from unittest.mock import Mock

import pytest
import requests

from labeler_dashboard.github import APIError, BudgetExceeded, GitHub
from labeler_dashboard.store import instant


def response(data=None, status=200, headers=None, next_page=False):
    result = Mock()
    result.status_code = status
    result.headers = headers or {}
    result.json.return_value = data
    result.links = {"next": {"url": "unused"}} if next_page else {}
    return result


def client(*responses):
    session = Mock()
    session.get.side_effect = responses
    return GitHub("not-a-real-secret", session=session, sleep=Mock())


def test_full_pagination():
    api = client(response([{"id": 1}], next_page=True), response([{"id": 2}]))
    assert list(api.pages("/repos/dotnet/runtime/issues/1/timeline")) == [[{"id": 1}], [{"id": 2}]]
    assert api.session.get.call_args_list[1].kwargs["params"]["page"] == 2
    assert api.session.get.call_args_list[0].kwargs["allow_redirects"] is False


def test_failed_second_page_is_not_truncated_success():
    api = client(
        response([{"id": 1}], next_page=True),
        response(status=503),
        response(status=503),
        response(status=503),
    )
    with pytest.raises(APIError):
        list(api.pages("/repos/dotnet/runtime/issues/1/timeline"))


def test_run_cap_splits_into_narrower_windows():
    api = client(
        response({"total_count": 1001, "workflow_runs": []}),
        response({"total_count": 1, "workflow_runs": [{"id": 1}]}),
        response({"total_count": 1, "workflow_runs": [{"id": 2}]}),
    )
    runs = api.runs_in_window(
        "dotnet/runtime", 123, instant("2026-11-11T00:00:00Z"), instant("2026-11-12T00:00:00Z")
    )
    assert [run["id"] for run in runs] == [1, 2]
    queries = [call.kwargs["params"]["created"] for call in api.session.get.call_args_list]
    assert queries[1] == "2026-11-11T00:00:00Z..2026-11-11T11:59:59Z"
    assert queries[2] == "2026-11-11T12:00:00Z..2026-11-11T23:59:59Z"


def test_run_pagination_and_inconsistent_count():
    api = client(
        response({"total_count": 101, "workflow_runs": [{"id": n} for n in range(100)]}),
        response({"total_count": 101, "workflow_runs": [{"id": 100}]}),
    )
    assert (
        len(
            api.runs_in_window(
                "dotnet/runtime",
                1,
                instant("2026-11-11T00:00:00Z"),
                instant("2026-11-12T00:00:00Z"),
            )
        )
        == 101
    )
    api = client(response({"total_count": 2, "workflow_runs": [{"id": 1}]}))
    with pytest.raises(APIError, match="pagination changed"):
        api.runs_in_window(
            "dotnet/runtime", 1, instant("2026-11-11T00:00:00Z"), instant("2026-11-12T00:00:00Z")
        )


def test_retry_after_and_rate_limit_reserve():
    api = client(response(status=429, headers={"Retry-After": "1"}), response({"ok": True}))
    assert api.get("/repos/dotnet/runtime") == {"ok": True}
    api.sleep.assert_called_once_with(1)
    api = client(response(status=403, headers={"X-RateLimit-Remaining": "0"}))
    with pytest.raises(BudgetExceeded):
        api.get("/repos/dotnet/runtime")


def test_auth_error_is_not_an_expired_log_or_rate_limit():
    api = client(response(status=403))
    with pytest.raises(APIError) as error:
        api.log("dotnet/runtime", 1)
    assert error.value.status == 403
    assert "not-a-real-secret" not in str(error.value)


def test_network_errors_do_not_leak_request_details():
    api = client(
        requests.ConnectionError("secret URL"),
        requests.ConnectionError("secret URL"),
        requests.ConnectionError("secret URL"),
    )
    with pytest.raises(APIError) as error:
        api.get("/repos/dotnet/runtime")
    assert "secret URL" not in str(error.value)


def test_budget_and_download_host_safety():
    api = client(response(status=302, headers={"Location": "https://evil.example/secret"}))
    with pytest.raises(APIError, match="Unexpected log download host"):
        api.log("dotnet/runtime", 1)
    api = client(response({}))
    api.limit = 0
    with pytest.raises(BudgetExceeded):
        api.get("/repos/dotnet/runtime")


def test_collection_deadline_stops_before_api_call_and_after_retry_wait(monkeypatch):
    clock = [0]
    monkeypatch.setattr("labeler_dashboard.github.time.monotonic", lambda: clock[0])
    api = client(response({}))
    clock[0] = 45 * 60
    with pytest.raises(BudgetExceeded, match="time limit"):
        api.get("/repos/dotnet/runtime")
    api.session.get.assert_not_called()
    clock[0] = 0
    api = client(response(status=429, headers={"Retry-After": "1"}), response({}))
    api.sleep.side_effect = lambda _: clock.__setitem__(0, 45 * 60)
    with pytest.raises(BudgetExceeded, match="time limit"):
        api.get("/repos/dotnet/runtime")
    assert api.session.get.call_count == 1


def test_collection_deadline_stops_inflight_log_download(monkeypatch):
    clock = [0]
    monkeypatch.setattr("labeler_dashboard.github.time.monotonic", lambda: clock[0])
    api = client(
        response(
            status=302,
            headers={
                "Location": "https://logs.blob.core.windows.net/example",
            },
        )
    )
    download = Mock(status_code=200)

    def chunks(*args):
        clock[0] = 45 * 60
        yield b"partial log"

    download.iter_content.side_effect = chunks
    context = Mock()
    context.__enter__ = Mock(return_value=download)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr("labeler_dashboard.github.requests.get", Mock(return_value=context))
    with pytest.raises(BudgetExceeded, match="time limit"):
        api.log("dotnet/runtime", 77)
    assert api.used == 2
    context.__exit__.assert_called_once()
