import re
import runpy
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests


@pytest.fixture
def artifact_wait(monkeypatch):
    module = runpy.run_path(".github/scripts/wait_for_pages_artifact.py")
    get, sleep = Mock(), Mock()
    monkeypatch.setattr(module["requests"], "get", get)
    monkeypatch.setattr(module["time"], "sleep", sleep)
    return module["wait_for_artifact"], get, sleep


def metadata(*rows, status=200):
    return Mock(status_code=status, json=Mock(return_value={"artifacts": list(rows)}))


def test_artifact_visibility_race_retries_empty_listing_until_exact_artifact_exists(artifact_wait):
    wait, get, sleep = artifact_wait
    get.side_effect = [
        metadata(),
        metadata({"id": 1, "name": "unrelated", "expired": False}),
        metadata({"id": 2, "name": "github-pages", "expired": False}),
    ]
    wait("matouskozak/dotnet-labeler-dashboard", "123", "test-token")
    assert get.call_count == 3
    assert sleep.call_args_list == [((10,),), ((10,),)]
    for call in get.call_args_list:
        assert call.args == (
            "https://api.github.com/repos/matouskozak/dotnet-labeler-dashboard/actions/runs/123/artifacts",
        )
        assert call.kwargs["params"] == {"name": "github-pages", "per_page": 100}
        assert call.kwargs["allow_redirects"] is False


def test_artifact_absence_has_bounded_wait_and_explicit_failure(artifact_wait):
    wait, get, sleep = artifact_wait
    get.return_value = metadata({"id": 2, "name": "github-pages", "expired": True})
    with pytest.raises(RuntimeError, match="after six checks"):
        wait("owner/repo", "123", "test-token")
    assert get.call_count == 6
    assert sleep.call_count == 5


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
def test_metadata_api_errors_fail_immediately_instead_of_polling(artifact_wait, status):
    wait, get, sleep = artifact_wait
    get.return_value = metadata(status=status)
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        wait("owner/repo", "123", "test-token")
    get.assert_called_once()
    sleep.assert_not_called()


def test_network_failure_does_not_expose_request_credentials(artifact_wait):
    wait, get, sleep = artifact_wait
    get.side_effect = requests.ConnectionError("sensitive request details")
    with pytest.raises(RuntimeError) as error:
        wait("owner/repo", "123", "test-token")
    assert "sensitive" not in str(error.value)
    sleep.assert_not_called()


def test_publication_only_skips_all_collection_and_history_writes_by_default_false_input():
    workflow = Path(".github/workflows/refresh.yml").read_text()
    assert re.search(
        r"publish_only:\n(?:[^\n]*\n)*?        type: boolean\n        default: false", workflow
    )
    steps = re.split(r"^      - ", workflow, flags=re.MULTILINE)[1:]
    for name in ("Collect with the actual workflow credential", "Persist evidence before building"):
        step = next(step for step in steps if step.startswith(f"name: {name}\n"))
        assert "if: ${{ inputs.publish_only != true }}" in step
    for name in (
        "Build from persisted snapshot",
        "Wait for Pages artifact metadata",
        "Deploy Pages",
    ):
        step = next(step for step in steps if step.startswith(f"name: {name}\n"))
        assert "\n        if:" not in step
    assert "ref: data\n          path: .state" in workflow
    assert (
        workflow.index("actions/upload-pages-artifact@")
        < workflow.index("name: Wait for Pages artifact metadata")
        < workflow.index("name: Deploy Pages")
    )
    assert "if: steps.collect.outcome == 'failure'" in workflow
    assert "  actions: read\n" in workflow
