from copy import deepcopy

import pytest
from test_metrics import CONFIG, KEY, fixture, summarize

from labeler_dashboard.metrics import daily_coverage, reconstruct, report
from labeler_dashboard.store import instant


def test_daily_counts_deduplicate_retries_and_human_edit_pairs():
    state = fixture()
    # A later result must match a different mutation, not steal the first confirmation.
    state["events"]["e4"] = {
        **state["events"]["e1"],
        "id": 35805626722,
        "at": "2026-11-11T15:00:00Z",
    }
    state["attempts"]["a2"] = deepcopy(state["attempts"]["a1"])
    state["attempts"]["a2"]["observations"][0].update(
        at="2026-11-11T15:00:00Z",
        step_start="2026-11-11T14:59:50Z",
        step_end="2026-11-11T15:00:05Z",
    )
    activity = summarize(state)["items"][0]["activity"]
    assert activity == [{"date": "2026-11-11", "series": ["applied", "human_changed"]}]


def test_no_selection_is_supported_automatic_evidence_not_a_skip_or_guess():
    state = fixture()
    state["events"].clear()
    source = state["attempts"].pop("a1")
    for index, (outcome, version, manual, dry_run) in enumerate(
        [
            ("fallback_applied", "v2.3.0", False, False),
            ("fallback_present", "v2.3.0", False, False),
            ("no_selection", "v2.3.0", False, False),
            ("no_selection", None, False, False),
            ("fallback_applied", "v2.3.0", True, False),
            ("fallback_applied", "v2.3.0", False, True),
            ("existing_area_skip", "v2.3.0", False, False),
            ("unsupported", "v2.3.0", False, False),
        ]
    ):
        attempt = deepcopy(source)
        attempt.update(manual=manual, dry_run=dry_run)
        attempt["observations"][0].update(
            outcome=outcome,
            action_version=version,
            at=f"2026-11-{11 if index < 3 else 12}T12:00:00Z",
        )
        state["attempts"][str(index)] = attempt
    assert summarize(state)["items"][0]["activity"] == [
        {"date": "2026-11-11", "series": ["no_selection"]}
    ]


def test_unknown_application_is_not_confirmed_and_skip_only_is_excluded():
    state = fixture()
    state["attempts"].clear()
    assert summarize(state)["items"][0]["activity"] == [
        {"date": "2026-11-11", "series": ["human_changed"]}
    ]
    state = fixture()
    state["attempts"]["a1"]["observations"][0]["outcome"] = "existing_area_skip"
    assert summarize(state)["items"] == []


def test_daily_events_use_utc_dates_not_grouped_edit_end_date():
    state = fixture()
    state["events"]["e2"]["at"] = "2026-11-11T23:59:59Z"
    state["events"]["e3"]["at"] = "2026-11-12T00:00:01Z"
    item = summarize(state)["items"][0]
    assert item["activity"] == [
        {"date": "2026-11-11", "series": ["applied", "human_changed"]},
        {"date": "2026-11-12", "series": ["human_changed"]},
    ]


def test_human_series_uses_actor_type_not_login_or_visible_change_rows():
    state = fixture()
    state["events"]["e3"]["actor"] = "human-with-bot-suffix[bot]"
    state["events"]["reapply"] = {
        **state["events"]["e1"],
        "id": 35805626722,
        "at": "2026-11-12T12:00:00Z",
        "actor": "automation-without-bot-suffix",
    }
    result = summarize(state)
    assert any(row["actor"] == "automation-without-bot-suffix" for row in result["changes"])
    assert result["items"][0]["activity"] == [
        {"date": "2026-11-11", "series": ["applied", "human_changed"]},
    ]


def test_daily_coverage_distinguishes_complete_zero_from_missing_history():
    state = fixture()
    state["cohort_start"] = "2026-11-10T00:00:00Z"
    state["repositories"]["dotnet/machinelearning"] = {
        "intake_complete": True,
        "updated_checkpoint": "2026-11-19T12:00:00Z",
    }
    stream = CONFIG["streams"][0]
    progress = state["streams"]["dotnet/machinelearning:issue"]
    progress["backfill_to"] = "2026-11-12T00:00:00Z"
    rows = daily_coverage(
        state,
        stream,
        list(state["items"].values()),
        list(state["runs"].values()),
        instant("2026-11-19T12:00:00Z"),
    )
    covered = {row["date"]: row["complete"] for row in rows}
    assert covered["2026-11-10"]  # Full day with no observed events.
    assert covered["2026-11-11"]
    assert not covered["2026-11-12"]
    assert not covered["2026-11-19"]  # Today's partial counts are not a completed day.
    state["attempts"]["a1"]["observations"][0]["outcome"] = "unsupported"
    result = report(state, CONFIG, instant("2026-11-19T12:00:00Z"))
    assert not next(r for r in result["streams"][0]["daily_coverage"] if r["date"] == "2026-11-11")[
        "complete"
    ]
    state["attempts"]["a1"]["observations"][0]["outcome"] = "area_applied"
    state["items"][KEY]["observed_through"] = "2026-11-11T23:59:59Z"
    result = report(state, CONFIG, instant("2026-11-19T12:00:00Z"))
    assert not next(r for r in result["streams"][0]["daily_coverage"] if r["date"] == "2026-11-11")[
        "complete"
    ]
    state["items"][KEY]["observed_through"] = "2026-11-19T12:00:00Z"
    state["runs"]["run1"]["evidence"] = "unavailable"
    result = report(state, CONFIG, instant("2026-11-19T12:00:00Z"))
    assert not next(r for r in result["streams"][0]["daily_coverage"] if r["date"] == "2026-11-11")[
        "complete"
    ]
    assert result["streams"][1]["daily_coverage"] == []


def test_plot_observations_do_not_change_model_confirmation():
    state = fixture()
    before = deepcopy(state)
    confirmed = reconstruct(state)[2]
    summarize(state)
    assert state == before
    assert reconstruct(state)[2] == confirmed


@pytest.mark.parametrize("evidence", ["pending", "unavailable", "unknown"])
def test_unresolved_rerun_affects_execution_day_not_original_created_day(evidence):
    state = fixture()
    state["cohort_start"] = "2026-11-10T00:00:00Z"
    state["repositories"]["dotnet/machinelearning"] = {
        "intake_complete": True,
        "updated_checkpoint": "2026-11-19T12:00:00Z",
    }
    state["runs"]["rerun"] = {
        **state["runs"]["run1"],
        "attempt": 2,
        "evidence": evidence,
        "started_at": "2026-11-18T12:00:00Z",
        "updated_at": "2026-11-18T12:05:00Z",
        "completed_at": None if evidence == "pending" else "2026-11-18T12:05:00Z",
        "status": "in_progress" if evidence == "pending" else "completed",
        "collected": evidence != "pending",
    }
    rows = summarize(state)["streams"][0]["daily_coverage"]
    days = {row["date"]: row["complete"] for row in rows}
    assert days["2026-11-11"]
    assert days["2026-11-17"]
    assert not days["2026-11-18"]
    # Older snapshots lack attempt timing. Unknown execution stays conservative.
    state["runs"]["rerun"]["started_at"] = None
    state["runs"]["rerun"]["completed_at"] = None
    days = {r["date"]: r["complete"] for r in summarize(state)["streams"][0]["daily_coverage"]}
    assert not days["2026-11-11"]
    assert not days["2026-11-18"]
    assert days["2026-11-10"]


def test_rerun_with_no_item_observations_also_leaves_new_item_intake_incomplete():
    state = fixture()
    state["items"][KEY]["created_at"] = "2026-11-18T12:00:00Z"
    state["events"].clear()
    state["attempts"].clear()
    state["runs"]["run1"].update(
        attempt=2,
        evidence="unavailable",
        started_at="2026-11-18T12:01:00Z",
        completed_at="2026-11-18T12:05:00Z",
        updated_at="2026-11-18T12:05:00Z",
    )
    item = summarize(state)["items"][0]
    assert item["intake_1"] == {
        "status": "no_confirmed_application",
        "evidence_complete": False,
    }


def test_unresolved_attempt_spanning_midnight_keeps_both_execution_days_partial():
    state = fixture()
    state["cohort_start"] = "2026-11-10T00:00:00Z"
    state["repositories"]["dotnet/machinelearning"] = {
        "intake_complete": True,
        "updated_checkpoint": "2026-11-19T12:00:00Z",
    }
    state["runs"]["run1"].update(
        evidence="unknown",
        started_at="2026-11-17T23:59:50Z",
        completed_at="2026-11-18T00:01:00Z",
        updated_at="2026-11-18T00:01:00Z",
    )
    state["attempts"].clear()
    days = {r["date"]: r["complete"] for r in summarize(state)["streams"][0]["daily_coverage"]}
    assert days["2026-11-16"]
    assert not days["2026-11-17"]
    assert not days["2026-11-18"]


def covered_state():
    state = fixture()
    state["cohort_start"] = "2026-11-10T00:00:00Z"
    state["repositories"]["dotnet/machinelearning"] = {
        "intake_complete": True,
        "updated_checkpoint": "2026-11-19T12:00:00Z",
    }
    return state


@pytest.mark.parametrize("evidence", ["unknown", "pending", "unavailable", "technical_failure"])
def test_missing_prediction_evidence_does_not_make_collected_human_changes_partial(evidence):
    state = covered_state()
    state["runs"]["gap"] = {
        **state["runs"]["run1"],
        "evidence": evidence,
        "collected": evidence != "pending",
        "status": "queued" if evidence == "pending" else "completed",
        "completed_at": None if evidence == "pending" else state["runs"]["run1"]["completed_at"],
    }
    result = summarize(state)
    day = next(row for row in result["streams"][0]["daily_coverage"] if row["date"] == "2026-11-11")
    assert day["series_complete"] == {
        "applied": False,
        "no_selection": False,
        "human_changed": True,
    }
    assert not day["complete"]  # Cached clients remain conservative.
    assert day["partial_reason"] == {
        "applied": "results_missing",
        "no_selection": "results_missing",
        "human_changed": None,
    }
    assert result["items"][0]["activity"] == summarize(covered_state())["items"][0]["activity"]
    assert result["changes"] == summarize(covered_state())["changes"]
    assert result["streams"][0]["collection_caught_up"]


@pytest.mark.parametrize("missing", ["intake", "timeline", "run_history"])
def test_series_coverage_tracks_its_own_missing_history(missing):
    state = covered_state()
    if missing == "intake":
        state["repositories"]["dotnet/machinelearning"]["intake_complete"] = False
    elif missing == "timeline":
        state["items"][KEY]["observed_through"] = "2026-11-11T23:59:59Z"
    else:
        state["streams"]["dotnet/machinelearning:issue"]["backfill_to"] = "2026-11-11T23:59:59Z"
    day = next(
        row
        for row in summarize(state)["streams"][0]["daily_coverage"]
        if row["date"] == "2026-11-11"
    )
    assert day["series_complete"]["human_changed"] is (missing == "run_history")
    assert not day["series_complete"]["applied"]
    assert not day["series_complete"]["no_selection"]
    assert day["partial_reason"]["applied"] == "history_incomplete"
    assert day["partial_reason"]["human_changed"] == (
        None if missing == "run_history" else "history_incomplete"
    )


def test_initial_partial_day_and_current_day_never_become_complete_for_any_series():
    state = covered_state()
    state["cohort_start"] = "2026-11-10T12:00:00Z"
    state["repositories"]["dotnet/machinelearning"]["updated_checkpoint"] = "2026-11-21T00:00:00Z"
    state["items"][KEY]["observed_through"] = "2026-11-21T00:00:00Z"
    state["streams"]["dotnet/machinelearning:issue"].update(
        backfill_to="2026-11-21T00:00:00Z",
        live_from="2026-11-10T00:00:00Z",
        live_to="2026-11-21T00:00:00Z",
    )
    days = {row["date"]: row for row in summarize(state)["streams"][0]["daily_coverage"]}
    for boundary in ("2026-11-10", "2026-11-19"):
        assert not any(days[boundary]["series_complete"].values())
        assert not days[boundary]["complete"]
        assert set(days[boundary]["partial_reason"].values()) == {"partial_day"}
    assert all(days["2026-11-11"]["series_complete"].values())


@pytest.mark.parametrize("mode", ["manual", "dry_run"])
def test_nonproduction_unknowns_do_not_lower_any_daily_series_coverage(mode):
    state = covered_state()
    run = {**state["runs"]["run1"], "evidence": "unknown", "manual": mode == "manual"}
    if mode == "dry_run":
        run.update(
            branch="main",
            configs=[
                {
                    "dry_run": True,
                    "action_version": None,
                    "action_sha": None,
                    "threshold": None,
                    "cache_hit": None,
                }
            ],
        )
    state["runs"]["nonproduction"] = run
    result = summarize(state)
    day = next(row for row in result["streams"][0]["daily_coverage"] if row["date"] == "2026-11-11")
    assert day["complete"]
    assert all(day["series_complete"].values())
    assert result["streams"][1]["daily_coverage"] == []
    assert not result["streams"][1]["collection_caught_up"]


def test_human_coverage_is_for_tracked_cohort_without_overriding_skip_exclusion():
    state = covered_state()
    state["attempts"].clear()
    unknown = summarize(state)
    assert len(unknown["items"]) == 1
    assert unknown["items"][0]["unknown"]
    day = next(
        row for row in unknown["streams"][0]["daily_coverage"] if row["date"] == "2026-11-11"
    )
    assert day["series_complete"]["human_changed"]
    state["attempts"] = deepcopy(fixture()["attempts"])
    state["attempts"]["a1"]["observations"][0]["outcome"] = "existing_area_skip"
    excluded = summarize(state)
    assert not excluded["items"] and not excluded["changes"]
    assert excluded["streams"][0]["items"] == 0
