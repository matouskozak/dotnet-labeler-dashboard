from copy import deepcopy
from datetime import timedelta

import pytest
from test_metrics import CONFIG, KEY, fixture

from labeler_dashboard import metrics
from labeler_dashboard.store import instant, stamp


def unresolved(start, end, **overrides):
    return {
        "started_at": start,
        "completed_at": end,
        "created_at": "2026-11-01T00:00:00Z",
        "status": "in_progress",
        "collected": False,
        "evidence": "pending",
        **overrides,
    }


@pytest.mark.parametrize(
    "offset,expected", [(-1, False), (0, True), (1800, True), (3600, True), (3601, False)]
)
@pytest.mark.parametrize("evidence", ["pending", "unknown", "unavailable"])
def test_index_preserves_inclusive_intake_deadline(offset, expected, evidence):
    start = instant("2026-11-11T12:00:00Z")
    at = stamp(start + timedelta(seconds=offset))
    run = unresolved(at, at, collected=evidence != "pending", evidence=evidence)
    gaps = metrics.ExecutionIntervals([run])
    assert gaps.overlaps(stamp(start), stamp(start + timedelta(hours=1, seconds=1))) is expected
    assert gaps.overlaps(stamp(start), stamp(start + timedelta(hours=1, seconds=1))) == (
        metrics.execution_overlaps(run, stamp(start), stamp(start + timedelta(hours=1, seconds=1)))
    )


def test_merged_intervals_match_linear_scan_with_unknown_and_open_execution_bounds():
    runs = [
        unresolved("2026-11-11T12:00:00Z", "2026-11-11T13:00:00Z"),
        unresolved("2026-11-11T12:30:00Z", "2026-11-11T14:00:00Z"),
        unresolved("2026-11-11T14:00:00Z", "2026-11-11T14:30:00Z"),
        unresolved("2026-11-18T12:00:00Z", None),
        unresolved(None, None, status="completed", updated_at="2026-11-03T00:00:00Z"),
    ]
    for selected in (runs, runs[:3], runs[3:], [], [unresolved(None, None, created_at=None)]):
        index = metrics.ExecutionIntervals(selected)
        for day in range(1, 21):
            for hour in (0, 12, 13, 14, 23):
                lower = instant(f"2026-11-{day:02d}T{hour:02d}:00:00Z")
                upper = lower + timedelta(hours=1)
                assert index.overlaps(stamp(lower), stamp(upper)) == any(
                    metrics.execution_overlaps(run, stamp(lower), stamp(upper)) for run in selected
                )
    index = metrics.ExecutionIntervals(runs[:3])
    assert index.starts == ["2026-11-11T12:00:00Z"]
    assert index.ends == ["2026-11-11T14:30:00Z"]
    assert not index.overlaps("2026-11-11T11:00:00Z", "2026-11-11T12:00:00Z")
    assert index.overlaps("2026-11-11T14:30:00Z", "2026-11-11T15:00:00Z")


@pytest.mark.parametrize(
    "kind", ["pending", "unknown", "unavailable", "manual", "dry_run", "known", "failure"]
)
def test_index_filters_preserve_different_intake_and_daily_criteria(kind):
    run = unresolved("2026-11-18T12:00:00Z", "2026-11-18T12:05:00Z")
    if kind in {"unknown", "unavailable"}:
        run.update(collected=True, evidence=kind)
    elif kind == "manual":
        run["manual"] = True
    elif kind == "dry_run":
        run["configs"] = [{"dry_run": True}]
    elif kind == "known":
        run.update(collected=True, evidence="item_results")
    elif kind == "failure":
        run.update(collected=True, evidence="technical_failure", has_terminal_item_evidence=True)
    lower, upper = "2026-11-18T00:00:00Z", "2026-11-19T00:00:00Z"
    intake = metrics.ExecutionIntervals(r for r in [run] if metrics.intake_run_unresolved(r))
    daily = metrics.ExecutionIntervals(r for r in [run] if metrics.daily_run_unresolved(r))
    assert intake.overlaps(lower, upper) is (kind in {"pending", "unknown", "unavailable"})
    assert daily.overlaps(lower, upper) is (
        kind in {"pending", "unknown", "unavailable", "failure"}
    )


def synthetic_state(item_count, run_count):
    state = fixture()
    item = state["items"][KEY]
    run = state["runs"]["run1"]
    state["items"] = {
        f"item:{index}": {**item, "id": index, "number": index} for index in range(item_count)
    }
    state["runs"] = {f"run:{index}": dict(run) for index in range(run_count)}
    state["events"].clear()
    state["attempts"].clear()
    state["cohort_start"] = "2026-11-01T00:00:00Z"
    state["repositories"]["dotnet/machinelearning"] = {
        "intake_complete": True,
        "updated_checkpoint": "2026-11-19T12:00:00Z",
    }
    return state


def test_complete_run_evaluation_count_is_linear_not_per_item(monkeypatch):
    original = metrics.automatic_run
    calls = 0

    def counted(run):
        nonlocal calls
        calls += 1
        return original(run)

    monkeypatch.setattr(metrics, "automatic_run", counted)
    for item_count in (10, 500):
        calls = 0
        result = metrics.report(
            synthetic_state(item_count, 1000), CONFIG, instant("2026-11-19T12:00:00Z")
        )
        assert calls == 2000  # One intake and one daily classification per run.
        assert len(result["items"]) == item_count
        assert all(item["intake_1"]["evidence_complete"] for item in result["items"])


def test_indexed_report_matches_reference_scan(monkeypatch):
    state = synthetic_state(12, 10)
    reference = deepcopy(state["runs"]["run:0"])
    for index, kind in enumerate(("unknown", "pending", "unavailable")):
        state["runs"][f"gap:{index}"] = {
            **reference,
            **unresolved(
                f"2026-11-11T{13 + index:02d}:00:00Z",
                f"2026-11-11T{14 + index:02d}:00:00Z",
                evidence=kind,
            ),
        }
    expected = metrics.report(state, CONFIG, instant("2026-11-19T12:00:00Z"))

    class LinearIntervals:
        def __init__(self, runs):
            self.runs = list(runs)

        def overlaps(self, lower, upper):
            return any(metrics.execution_overlaps(run, lower, upper) for run in self.runs)

    monkeypatch.setattr(metrics, "ExecutionIntervals", LinearIntervals)
    assert metrics.report(state, CONFIG, instant("2026-11-19T12:00:00Z")) == expected
