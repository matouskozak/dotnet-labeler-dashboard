from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_collect import CONFIG, NOW, collector, jobs, source_run

from labeler_dashboard.collect import PARSER_VERSION
from labeler_dashboard.github import APIError, BudgetExceeded
from labeler_dashboard.metrics import report
from labeler_dashboard.store import instant, load, save, stamp


def test_old_queued_window_persists_then_discovers_next_window_and_audits_completion(tmp_path):
    c = collector()
    stream = CONFIG["streams"][0]
    key = "dotnet/runtime:issue"
    first_end = instant("2026-11-12T00:00:00Z")
    second_end = instant("2026-11-12T06:00:00Z")
    c.state["streams"][key].update(
        backfill_to="2026-11-11T18:00:00Z",
        live_to=stamp(NOW),
    )
    c.state["repositories"]["dotnet/runtime"].update(
        intake_complete=True,
        updated_checkpoint=stamp(NOW),
    )
    c.state["permissions"]["dotnet/runtime"] = {
        "checks": {name: {"ok": True} for name in ("runs", "jobs", "logs")},
    }
    queued = source_run(
        status="queued",
        conclusion=None,
        created_at="2026-11-11T20:35:50Z",
        updated_at="2026-11-11T20:35:50Z",
        run_started_at="2026-11-11T20:35:50Z",
    )
    c.api.runs_in_window.side_effect = [
        [queued],
        [source_run(id=13, event="schedule", created_at="2026-11-12T01:00:00Z")],
    ]
    c.api.pages.return_value = jobs(skipped=True)
    c.run_window(stream, "backfill_to", first_end)
    path = tmp_path / "state.json"
    save(path, c.state)
    persisted = load(path)
    assert persisted["streams"][key]["backfill_to"] == stamp(first_end)
    assert persisted["runs"]["dotnet/runtime:12:1"]["evidence"] == "pending"
    assert not persisted["runs"]["dotnet/runtime:12:1"]["collected"]
    c.run_window(stream, "backfill_to", second_end)
    assert c.state["streams"][key]["backfill_to"] == stamp(second_end)
    assert c.state["runs"]["dotnet/runtime:13:1"]["evidence"] == "no_work"
    before_audit = report(c.state, CONFIG, NOW)
    coverage = next(s for s in before_audit["streams"] if s["kind"] == "issue")
    assert coverage["production_incomplete_runs"] == 1
    assert next(day for day in coverage["daily_coverage"] if day["date"] == "2026-11-11")[
        "complete"
    ]
    c.api.get.return_value = queued
    c.audit_runs()
    c.api.get.assert_called_once_with("/repos/dotnet/runtime/actions/runs/12")
    assert not c.state["runs"]["dotnet/runtime:12:1"]["collected"]
    assert "last_success_at" not in c.state["repositories"]["dotnet/runtime"]["run_audits"]["12"]
    c.now, c.at = instant("2026-11-19T13:00:00Z"), "2026-11-19T13:00:00Z"
    c.api.get.return_value = source_run()
    c.api.pages.return_value = jobs()
    c.api.log.return_value = "2026-11-11T14:02:47Z [Issue dotnet/runtime#1] Label 'area-X' applied."
    c.audit_runs()
    assert c.state["runs"]["dotnet/runtime:12:1"]["collected"]
    assert len(c.state["attempts"]) == 1
    assert c.state["streams"][key]["backfill_to"] == stamp(second_end)
    c.now, c.at = instant("2026-11-19T20:00:00Z"), "2026-11-19T20:00:00Z"
    c.api.get.side_effect = lambda path: source_run(id=int(path.rsplit("/", 1)[1]))
    c.audit_runs()
    assert len(c.state["attempts"]) == 1
    assert c.api.log.call_count == 1


@pytest.mark.parametrize("failure_stage", ["listing", "next_run", "jobs_page", "logs"])
@pytest.mark.parametrize("failure", ["network", "budget"])
def test_pending_record_does_not_waive_incomplete_listing_or_fetch(failure_stage, failure):
    c = collector()
    stream = CONFIG["streams"][0]
    key = "dotnet/runtime:issue"
    before = deepcopy(c.state["streams"][key])
    error = (
        APIError("/repos/dotnet/runtime/actions/runs", 503)
        if failure == "network"
        else BudgetExceeded()
    )
    c.api.runs_in_window.return_value = [source_run(status="queued"), source_run(id=13)]
    c.api.pages.return_value = jobs()
    c.api.log.return_value = ""
    if failure_stage == "listing":
        c.api.runs_in_window.side_effect = error
    elif failure_stage in {"next_run", "jobs_page"}:

        def partial(*args, **kwargs):
            if failure_stage == "jobs_page":
                yield jobs()[0]
            raise error

        c.api.pages.side_effect = partial
    else:
        c.api.log.side_effect = error
    if failure_stage == "logs" and failure == "network":
        c.run_window(stream, "backfill_to", NOW)
        assert c.failed
    else:
        with pytest.raises(type(error)):
            c.run_window(stream, "backfill_to", NOW)
    assert c.state["streams"][key]["backfill_to"] == before["backfill_to"]
    if failure_stage != "listing":
        assert c.state["runs"]["dotnet/runtime:12:1"]["evidence"] == "pending"
        assert not c.state["runs"]["dotnet/runtime:13:1"]["collected"]


def test_pending_discovery_checkpoint_and_evidence_replace_atomically(tmp_path, monkeypatch):
    c = collector()
    path = tmp_path / "state.json"
    save(path, c.state)
    original = load(path)
    c.api.runs_in_window.return_value = [source_run(status="queued")]
    c.run_window(CONFIG["streams"][0], "backfill_to", NOW)
    monkeypatch.setattr(
        "labeler_dashboard.store.os.replace", Mock(side_effect=OSError("interrupted"))
    )
    with pytest.raises(OSError):
        save(path, c.state)
    assert load(path) == original
    assert not load(path)["runs"]


def test_many_permanently_pending_roots_share_audit_capacity_with_parser_upgrades():
    c = collector()
    c.state["permissions"]["dotnet/runtime"] = {
        "checks": {name: {"ok": True} for name in ("runs", "jobs", "logs")},
    }
    for identity in range(1, 202):
        pending = identity <= 200
        c.state["runs"][f"dotnet/runtime:{identity}:1"] = {
            "repo": "dotnet/runtime",
            "kind": "issue",
            "id": identity,
            "attempt": 1,
            "last_checked_at": "2026-11-11T00:00:00Z",
            "collected": not pending,
            "evidence": "pending" if pending else "parser_upgrade_pending",
            "parser_version": PARSER_VERSION if pending else PARSER_VERSION - 1,
        }
    visited = []

    def get(path):
        identity = int(path.rsplit("/", 1)[1])
        visited.append(identity)
        return source_run(
            id=identity,
            status="queued" if identity <= 200 else "completed",
            event="schedule",
        )

    c.api.get.side_effect = get
    c.api.pages.return_value = jobs(skipped=True)
    c.audit_runs()
    assert len(visited) == 100
    assert visited[:2] == [1, 201]
    assert c.state["runs"]["dotnet/runtime:201:1"]["parser_version"] == PARSER_VERSION
    assert all(
        not c.state["runs"][f"dotnet/runtime:{identity}:1"]["collected"]
        for identity in visited
        if identity <= 200
    )
