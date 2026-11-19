from copy import deepcopy

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
