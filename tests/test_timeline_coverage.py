from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_metrics import CONFIG, KEY, fixture

from labeler_dashboard.collect import Collector
from labeler_dashboard.github import APIError, BudgetExceeded
from labeler_dashboard.metrics import report
from labeler_dashboard.store import instant, timeline_coverage_through

FRIDAY = "2026-11-20T12:00:00Z"
MONDAY = "2026-11-23T03:46:02Z"
REPO = "dotnet/machinelearning"


def weekend_state():
    state = fixture()
    item = state["items"][KEY]
    item.update(
        observed_through=FRIDAY,
        updated_at="2026-11-15T14:03:01Z",
        timeline_source_updated="2026-11-15T14:03:01Z",
        created_at="2026-11-15T14:02:00Z",
    )
    for event in state["events"].values():
        event["at"] = event["at"].replace("2026-11-11", "2026-11-15")
    for row in state["attempts"]["a1"]["observations"]:
        for field in ("at", "step_start", "step_end"):
            row[field] = row[field].replace("2026-11-11", "2026-11-15")
    state["repositories"][REPO] = {
        "intake_complete": True,
        "updated_checkpoint": MONDAY,
        "last_success": MONDAY,
        "last_error": None,
    }
    state["streams"][f"{REPO}:issue"].update(backfill_to=MONDAY, live_to=MONDAY)
    return state


@pytest.mark.parametrize("status", ["open", "closed"])
def test_successful_unchanged_intake_extends_coverage_without_touching_raw_timeline(status):
    state = weekend_state()
    state["items"][KEY]["state"] = status
    before = deepcopy(state)
    result = report(state, CONFIG, instant(MONDAY))
    item = result["items"][0]
    assert item["observed_through"] == FRIDAY
    assert item["timeline_coverage_through"] == MONDAY
    assert item["removal_7"] is True
    days = {row["date"]: row for row in result["streams"][0]["daily_coverage"]}
    assert days["2026-11-22"]["complete"]
    assert all(days["2026-11-22"]["series_complete"].values())
    assert not any(days["2026-11-23"]["series_complete"].values())
    assert state == before


@pytest.mark.parametrize(
    "blocker",
    [
        "changed",
        "missing_timeline",
        "error",
        "reconcile",
        "unavailable",
        "intake_incomplete",
        "intake_error",
        "missing_revision",
        "unverified_checkpoint",
    ],
)
def test_unverified_or_changed_history_remains_partial(blocker):
    state = weekend_state()
    item, repo = state["items"][KEY], state["repositories"][REPO]
    if blocker == "changed":
        item["updated_at"] = "2026-11-22T13:00:00Z"
    elif blocker == "missing_timeline":
        item["observed_through"] = None
    elif blocker == "error":
        item["timeline_error"] = "Request failed"
    elif blocker == "reconcile":
        item["reconcile_requested"] = True
    elif blocker == "unavailable":
        item["timeline_availability"] = {"status": "unavailable"}
    elif blocker == "intake_incomplete":
        repo["intake_complete"] = False
    elif blocker == "intake_error":
        repo["last_error"] = "Page request failed"
    elif blocker == "missing_revision":
        item.pop("timeline_source_updated")
    else:
        repo.pop("last_success")
    result = report(state, CONFIG, instant(MONDAY))
    item = result["items"][0]
    assert item["timeline_coverage_through"] == state["items"][KEY]["observed_through"]
    assert item["removal_7"] is None
    day = next(row for row in result["streams"][0]["daily_coverage"] if row["date"] == "2026-11-22")
    assert not any(day["series_complete"].values())


def test_intake_windows_use_corroborated_coverage_for_known_unchanged_items():
    state = weekend_state()
    state["items"][KEY].update(
        created_at="2026-11-20T11:59:00Z",
        updated_at="2026-11-20T11:59:00Z",
        timeline_source_updated="2026-11-20T11:59:00Z",
        labels=[],
    )
    state["attempts"].clear()
    state["events"].clear()
    state["runs"].clear()
    result = report(state, CONFIG, instant(MONDAY))
    for hours in (1, 24):
        assert result["items"][0][f"intake_{hours}"] == {
            "status": "no_confirmed_application",
            "evidence_complete": True,
        }
    state["items"][KEY]["updated_at"] = "2026-11-22T12:00:00Z"
    result = report(state, CONFIG, instant(MONDAY))
    assert not result["items"][0]["intake_1"]["evidence_complete"]
    assert not result["items"][0]["intake_24"]["evidence_complete"]


@pytest.mark.parametrize("failure", ["page", "budget"])
def test_partial_incremental_scan_cannot_extend_timeline_coverage(failure):
    state = weekend_state()
    state["repositories"][REPO].update(updated_checkpoint=FRIDAY, last_success=FRIDAY)
    api = Mock()
    collector = Collector(api, state, {**CONFIG, "backfill_days": 30}, instant(MONDAY))

    def pages(*args):
        yield []
        if failure == "page":
            raise APIError(f"/repos/{REPO}/issues", 503)
        raise BudgetExceeded()

    api.pages.side_effect = pages
    with pytest.raises((APIError, BudgetExceeded)):
        collector.intake(REPO)
    assert timeline_coverage_through(state["items"][KEY], state["repositories"][REPO]) == FRIDAY
    api.pages.side_effect = None
    api.pages.return_value = [[]]
    collector.intake(REPO)
    assert timeline_coverage_through(state["items"][KEY], state["repositories"][REPO]) == MONDAY
    assert state["items"][KEY]["observed_through"] == FRIDAY


def test_real_prediction_gaps_remain_after_timeline_coverage_is_corroborated():
    state = weekend_state()
    state["cohort_start"] = "2026-10-20T12:00:00Z"
    state["runs"]["queued"] = {
        **state["runs"]["run1"],
        "status": "queued",
        "evidence": "pending",
        "collected": False,
        "created_at": "2026-11-11T20:35:50Z",
        "started_at": "2026-11-11T20:35:50Z",
        "completed_at": None,
        "updated_at": "2026-11-11T20:35:50Z",
    }
    days = {
        row["date"]: row
        for row in report(state, CONFIG, instant(MONDAY))["streams"][0]["daily_coverage"]
    }
    assert days["2026-11-22"]["series_complete"] == {
        "applied": False,
        "no_selection": False,
        "human_changed": True,
    }
    assert days["2026-11-22"]["partial_reason"]["applied"] == "results_missing"
    assert not any(days["2026-10-20"]["series_complete"].values())
    assert not any(days["2026-11-23"]["series_complete"].values())
