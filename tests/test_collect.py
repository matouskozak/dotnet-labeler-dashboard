from copy import deepcopy
from unittest.mock import Mock

import pytest

from labeler_dashboard.collect import Collector
from labeler_dashboard.github import APIError, BudgetExceeded
from labeler_dashboard.store import empty_state, instant, load, save, stamp

CONFIG = {
    "schema": 1,
    "backfill_days": 30,
    "streams": [
        {"repo": "dotnet/runtime", "kind": "issue", "enabled": True, "workflow_id": 1},
        {"repo": "dotnet/runtime", "kind": "pr", "enabled": True, "workflow_id": 2},
        {"repo": "dotnet/machinelearning", "kind": "pr", "enabled": False},
    ],
}
NOW = instant("2026-11-19T12:00:00Z")


def collector():
    api = Mock()
    state = empty_state()
    result = Collector(api, state, CONFIG, NOW)
    result.initialize()
    return result


def source_item(number=1, **overrides):
    return {
        "id": number + 100,
        "number": number,
        "state": "closed",
        "created_at": "2026-11-11T00:00:00Z",
        "updated_at": "2026-11-11T14:04:00Z",
        "labels": [{"name": "area-X"}, {"name": "untriaged"}],
        "html_url": f"https://github.com/dotnet/runtime/issues/{number}",
        "body": "DO NOT STORE BODY",
        "title": "DO NOT STORE TITLE",
        **overrides,
    }


def event(identity=1, **overrides):
    return {
        "id": identity,
        "event": "labeled",
        "created_at": "2026-11-11T14:02:47Z",
        "label": {"name": "area-X"},
        "actor": {"login": "human", "type": "User"},
        **overrides,
    }


def source_run(attempt=1, **overrides):
    return {
        "id": 12,
        "run_attempt": attempt,
        "event": "issues",
        "status": "completed",
        "conclusion": "success",
        "head_branch": "main",
        "created_at": "2026-11-11T14:02:30Z",
        "updated_at": "2026-11-11T14:03:00Z",
        "html_url": "https://github.com/dotnet/runtime/actions/runs/12",
        **overrides,
    }


def jobs(skipped=False):
    return [
        [
            {
                "id": 77,
                "steps": [
                    {"name": "Determine pull requests to process", "conclusion": "success"},
                    {
                        "name": "Restore pulls model from cache",
                        "conclusion": "skipped" if skipped else "success",
                    },
                    {
                        "name": "Predict issue labels",
                        "conclusion": "skipped" if skipped else "success",
                        "started_at": "2026-11-11T14:02:40Z",
                        "completed_at": "2026-11-11T14:02:55Z",
                    },
                ],
            }
        ]
    ]


def test_all_state_intake_keeps_closed_items_and_excludes_disabled_stream():
    c = collector()
    c.api.pages.return_value = [[source_item()]]
    c.intake("dotnet/runtime")
    item = c.state["items"]["dotnet/runtime:101"]
    assert item["state"] == "closed"
    assert item["labels"] == ["area-X"]
    assert "body" not in item and "title" not in item
    assert c.api.pages.call_args.args[1]["state"] == "all"
    c.retain_item("dotnet/machinelearning", source_item(pull_request={"merged_at": None}))
    assert len(c.state["items"]) == 1


def test_merged_pull_and_late_closed_changes():
    c = collector()
    c.retain_item(
        "dotnet/runtime",
        source_item(
            pull_request={"merged_at": "2026-11-12T00:00:00Z"},
            html_url="https://github.com/dotnet/runtime/pull/1",
        ),
    )
    assert c.state["items"]["dotnet/runtime:101"]["state"] == "merged"
    c.state["cohort_start"] = "2026-11-15T00:00:00Z"
    c.retain_item("dotnet/runtime", source_item(labels=[]))
    assert not c.state["items"]["dotnet/runtime:101"]["labels"]


def test_partial_intake_does_not_advance_checkpoint():
    c = collector()

    def pages(*args):
        yield [source_item()]
        raise BudgetExceeded()

    c.api.pages.side_effect = pages
    with pytest.raises(BudgetExceeded):
        c.intake("dotnet/runtime")
    assert not c.state["repositories"]["dotnet/runtime"]["intake_complete"]
    assert len(c.state["items"]) == 1


def test_timeline_complete_only_after_all_pages_and_dedupes():
    c = collector()
    c.retain_item("dotnet/runtime", source_item())
    item = c.state["items"]["dotnet/runtime:101"]

    def partial(*args):
        yield [event()]
        raise APIError("/repos/dotnet/runtime/issues/1/timeline", 503)

    c.api.pages.side_effect = partial
    with pytest.raises(APIError):
        c.timeline("dotnet/runtime:101", item)
    assert "observed_through" not in item
    assert len(c.state["events"]) == 1
    c.api.pages.side_effect = None
    c.api.pages.return_value = [[event()], [event(2, label={"name": "untriaged"})]]
    c.timeline("dotnet/runtime:101", item)
    c.timeline("dotnet/runtime:101", item)
    assert len(c.state["events"]) == 1
    assert item["observed_through"] == "2026-11-19T12:00:00Z"


def test_event_feed_gap_never_claims_full_history():
    c = collector()
    c.state["repositories"]["dotnet/runtime"]["feed_head"] = 4
    c.api.get.return_value = [{"id": 8, "event": "labeled"}]
    c.event_feed("dotnet/runtime")
    assert c.state["repositories"]["dotnet/runtime"]["feed_gap"]
    assert c.state["errors"]


def test_multiple_items_multiple_attempts_and_manual():
    c = collector()
    c.api.pages.return_value = jobs()
    c.api.log.return_value = (
        "2026-11-11T14:02:47Z [Issue dotnet/runtime#1] Label 'area-X' applied.\n"
        "2026-11-11T14:02:48Z [Issue dotnet/runtime#2] Label 'area-Y' applied."
    )
    c.collect_run(CONFIG["streams"][0], source_run())
    c.collect_run(CONFIG["streams"][0], source_run(2, event="workflow_dispatch"))
    assert len(c.state["attempts"]) == 4
    assert len(c.state["runs"]) == 2
    assert c.state["attempts"]["dotnet/runtime:12:2:1"]["manual"]
    c.collect_run(CONFIG["streams"][0], source_run(2, event="workflow_dispatch"))
    assert len(c.state["attempts"]) == 4
    assert c.api.log.call_count == 2


def test_pending_run_does_not_complete_window():
    c = collector()
    c.api.runs_in_window.return_value = [source_run(status="in_progress")]
    before = deepcopy(c.state["streams"]["dotnet/runtime:issue"])
    c.run_window(CONFIG["streams"][0], "live_to", NOW)
    after = c.state["streams"]["dotnet/runtime:issue"]
    assert after["live_to"] == before["live_to"]
    assert not c.state["runs"]["dotnet/runtime:12:1"]["collected"]


def test_no_work_green_run_and_expired_logs_are_distinct():
    c = collector()
    c.api.pages.return_value = jobs(skipped=True)
    c.collect_run(CONFIG["streams"][0], source_run(event="schedule"))
    assert c.state["runs"]["dotnet/runtime:12:1"]["evidence"] == "no_work"
    c = collector()
    c.api.pages.return_value = jobs()
    c.api.log.return_value = "2026-11-11T14:02:47Z All done."
    c.collect_run(CONFIG["streams"][0], source_run())
    assert c.state["runs"]["dotnet/runtime:12:1"]["evidence"] == "unknown"
    c = collector()
    c.api.pages.return_value = jobs()
    c.api.log.side_effect = APIError("/repos/dotnet/runtime/actions/jobs/77/logs", 410)
    c.collect_run(CONFIG["streams"][0], source_run())
    c.collect_run(CONFIG["streams"][0], source_run())
    run = c.state["runs"]["dotnet/runtime:12:1"]
    assert run["gaps"] == ["logs_expired_or_deleted"]
    assert c.api.log.call_count == 1


def test_recent_404_and_permission_denial_are_retryable_not_expired():
    for status in (404, 403):
        c = collector()
        c.api.pages.return_value = jobs()
        c.api.log.side_effect = APIError("/repos/dotnet/runtime/actions/jobs/77/logs", status)
        assert not c.collect_run(
            CONFIG["streams"][0], source_run(updated_at="2026-11-19T11:59:00Z")
        )
        assert not c.state["runs"]["dotnet/runtime:12:1"]["collected"]


def test_atomic_checkpoint_and_evidence_save(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    state = empty_state()
    save(path, state)
    changed = deepcopy(state)
    changed["items"]["one"] = {"number": 1}
    monkeypatch.setattr(
        "labeler_dashboard.store.os.replace", Mock(side_effect=OSError("interrupted"))
    )
    with pytest.raises(OSError):
        save(path, changed)
    assert load(path) == state
    assert list(tmp_path.iterdir()) == [path]


def test_unsupported_schema_is_not_silently_reset(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"schema":999}', encoding="utf-8")
    with pytest.raises(ValueError):
        load(path)


def test_incremental_descending_pagination_keeps_quiet_item_when_read_item_moves():
    c = collector()
    c.state["repositories"]["dotnet/runtime"].update(
        intake_complete=True, updated_checkpoint="2026-11-10T00:00:00Z"
    )
    sources = [source_item(n, updated_at=f"2026-11-11T00:0{n}:00Z") for n in (1, 2, 3)]

    def pages(path, params):
        reverse = params["direction"] == "desc"
        ordered = sorted(sources, key=lambda source: source["updated_at"], reverse=reverse)
        yield deepcopy(ordered[:2])
        ordered[0]["updated_at"] = "2026-11-19T12:01:00Z"
        reordered = sorted(sources, key=lambda source: source["updated_at"], reverse=reverse)
        yield deepcopy(reordered[2:])

    c.api.pages.side_effect = pages
    c.intake("dotnet/runtime")
    assert {item["number"] for item in c.state["items"].values()} == {1, 2, 3}


def test_skipped_predictor_after_restore_is_not_proven_empty_batch():
    c = collector()
    steps = jobs(skipped=True)
    steps[0][0]["steps"][1]["conclusion"] = "success"
    c.api.pages.return_value = steps
    c.collect_run(CONFIG["streams"][0], source_run(event="schedule"))
    assert c.state["runs"]["dotnet/runtime:12:1"]["evidence"] == "unknown"


def test_explicit_cache_failure_is_not_an_empty_batch_or_item_failure():
    c = collector()
    steps = jobs(skipped=True)
    steps[0][0]["steps"][1]["conclusion"] = "failure"
    c.api.pages.return_value = steps
    c.collect_run(CONFIG["streams"][0], source_run(event="schedule", conclusion="failure"))
    run = c.state["runs"]["dotnet/runtime:12:1"]
    assert run["evidence"] == "technical_failure"
    assert run["failure_phases"] == ["cache_restore"]
    assert not c.state["attempts"]


def test_old_parser_records_are_reparsed_instead_of_skipped():
    c = collector()
    c.state["runs"]["dotnet/runtime:12:1"] = {
        "parser_version": 1,
        "collected": True,
        "evidence": "no_work",
    }
    c.initialize()
    assert c.state["runs"]["dotnet/runtime:12:1"]["evidence"] == "parser_upgrade_pending"
    sha = "160b6b1e1e8d36da09beb34ef1e4806d2c0520a3"
    c.api.pages.return_value = jobs()
    c.api.log.return_value = (
        f"2026-11-11T14:02:40Z Download action repository 'dotnet/issue-labeler@{sha}' (SHA:{sha})\n"
        "2026-11-11T14:02:41Z   threshold: 0.05\n"
        "2026-11-11T14:02:47Z [Issue dotnet/runtime#1] Label 'area-X' applied."
    )
    c.collect_run(CONFIG["streams"][0], source_run())
    run = c.state["runs"]["dotnet/runtime:12:1"]
    assert run["parser_version"] == 2
    assert run["configs"][0]["action_version"] == "v2.2.0"
    assert run["configs"][0]["threshold"] == 0.05


@pytest.mark.parametrize("status", [301, 404, 410])
def test_unavailable_item_has_bounded_retries_without_losing_evidence(status):
    c = collector()
    c.state["permissions"]["dotnet/runtime"] = {
        "checks": {"issues": {"ok": True}, "timeline": {"ok": True}}
    }
    c.retain_item("dotnet/runtime", source_item())
    key = "dotnet/runtime:101"
    item = c.state["items"][key]
    c.api.pages.side_effect = APIError("/repos/dotnet/runtime/issues/1/timeline", status)
    for at in (
        "2026-11-19T12:00:00Z",
        "2026-11-19T12:30:00Z",
        "2026-11-19T13:00:00Z",
        "2026-11-20T13:00:00Z",
        "2026-12-01T00:00:00Z",
    ):
        c.now, c.at = instant(at), at
        c.reconcile(key, item)
    assert c.api.pages.call_count == 3
    assert item["timeline_availability"]["status"] == "unavailable"
    assert "observed_through" not in item
    assert key in c.state["items"]
    assert not c.failed
    item["updated_at"] = stamp(c.now)
    c.api.pages.side_effect = None
    c.api.pages.return_value = [[event()]]
    c.reconcile(key, item)
    assert item["observed_through"] == stamp(c.now)
    assert "timeline_availability" not in item


def test_unhealthy_source_does_not_turn_missing_items_into_tombstones():
    c = collector()
    c.state["permissions"]["dotnet/runtime"] = {
        "checks": {"issues": {"ok": True}, "timeline": {"ok": False}}
    }
    c.retain_item("dotnet/runtime", source_item())
    item = c.state["items"]["dotnet/runtime:101"]
    c.reconcile("dotnet/runtime:101", item)
    assert c.api.pages.call_count == 0
    assert "timeline_availability" not in item


def test_preflight_log_authorization_failure_is_visible():
    c = collector()
    c.api.get.side_effect = [
        [source_item()],
        [],
        {"workflow_runs": [source_run()]},
        {"jobs": [{"id": 77}]},
    ]
    c.api.log.side_effect = APIError("/repos/dotnet/runtime/actions/jobs/77/logs", 403)
    checks = c.preflight("dotnet/runtime")
    assert checks["issues"]["ok"] and checks["timeline"]["ok"]
    assert checks["logs"]["status"] == 403
    assert c.failed and c.state["errors"]
