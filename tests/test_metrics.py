from copy import deepcopy
from datetime import timedelta

from labeler_dashboard.metrics import reconstruct, report
from labeler_dashboard.render import render
from labeler_dashboard.store import empty_state, instant

KEY = "dotnet/machinelearning:7901"
AT = "2026-11-11T14:02:47Z"
CONFIG = {
    "streams": [
        {"repo": "dotnet/machinelearning", "kind": "issue", "enabled": True},
        {"repo": "dotnet/machinelearning", "kind": "pr", "enabled": False},
    ]
}


def fixture():
    state = empty_state()
    state["cohort_start"] = "2026-10-20T00:00:00Z"
    state["items"][KEY] = {
        "repo": "dotnet/machinelearning",
        "id": 7901,
        "number": 7901,
        "kind": "issue",
        "url": "https://github.com/dotnet/machinelearning/issues/7901",
        "created_at": "2026-11-11T14:02:00Z",
        "state": "closed",
        "labels": ["area-Trainers"],
        "observed_through": "2026-11-19T12:00:00Z",
    }
    state["events"]["e1"] = {
        "id": 35805604133,
        "item": KEY,
        "repo": "dotnet/machinelearning",
        "at": AT,
        "event": "labeled",
        "label": "area-Infrastructure",
        "actor": "github-actions[bot]",
        "actor_type": "Bot",
    }
    state["events"]["e2"] = {
        "id": 35805626632,
        "item": KEY,
        "repo": "dotnet/machinelearning",
        "at": "2026-11-11T14:03:01Z",
        "event": "unlabeled",
        "label": "area-Infrastructure",
        "actor": "human",
        "actor_type": "User",
    }
    state["events"]["e3"] = {
        **state["events"]["e2"],
        "id": 35805626721,
        "event": "labeled",
        "label": "area-Trainers",
    }
    state["runs"]["run1"] = {
        "repo": "dotnet/machinelearning",
        "kind": "issue",
        "url": "https://github.com/dotnet/machinelearning/actions/runs/43034653858",
        "created_at": "2026-11-11T14:02:20Z",
        "last_checked_at": AT,
        "collected": True,
        "evidence": "item_results",
        "configs": [],
    }
    state["attempts"]["a1"] = {
        "repo": "dotnet/machinelearning",
        "number": 7901,
        "run": "run1",
        "manual": False,
        "dry_run": False,
        "observations": [
            {
                "at": AT,
                "outcome": "area_applied",
                "label": "area-Infrastructure",
                "step_start": "2026-11-11T14:02:40Z",
                "step_end": "2026-11-11T14:02:50Z",
                "action_version": "v2.3.0",
            }
        ],
    }
    state["streams"]["dotnet/machinelearning:issue"] = {
        "backfill_to": "2026-11-19T12:00:00Z",
        "live_to": "2026-11-19T12:00:00Z",
    }
    return state


def summarize(state, now="2026-11-19T12:00:00Z"):
    return report(state, CONFIG, instant(now))


def test_confirmed_application_removal_and_unambiguous_replacement():
    result = summarize(fixture())
    item = result["items"][0]
    assert item["confirmed"]
    assert item["removal_7"] is True
    assert item["removal_30"] is None
    assert [row["category"] for row in result["changes"]] == [
        "human_area_removed",
        "human_area_added",
    ]
    assert result["changes"][0]["elapsed_seconds"] == 14
    assert result["changes"][0]["replacement"] == "area-Infrastructure -> area-Trainers"
    assert result["streams"][1]["enabled"] is False


def test_shared_bot_without_explicit_supported_outcome_is_not_confirmation():
    for scenario in ("missing", "unsupported", "manual", "dry_run", "incomplete_timeline"):
        state = fixture()
        if scenario == "missing":
            state["attempts"].clear()
        elif scenario == "unsupported":
            state["attempts"]["a1"]["observations"][0]["action_version"] = None
        elif scenario == "incomplete_timeline":
            state["items"][KEY].pop("observed_through")
        else:
            state["attempts"]["a1"][scenario] = True
        assert not summarize(state)["items"][0]["confirmed"]


def test_one_mutation_cannot_confirm_overlapping_attempts():
    state = fixture()
    state["attempts"]["a2"] = deepcopy(state["attempts"]["a1"])
    assert not reconstruct(state)[2]
    assert summarize(state)["items"][0]["probable"]


def test_repeated_application_is_not_another_followup_sample_and_is_reapplication():
    state = fixture()
    state["events"]["e4"] = {
        **state["events"]["e1"],
        "id": 9,
        "at": "2026-11-12T14:02:47Z",
    }
    state["attempts"]["a2"] = deepcopy(state["attempts"]["a1"])
    row = state["attempts"]["a2"]["observations"][0]
    row.update(
        at="2026-11-12T14:02:47Z",
        step_start="2026-11-12T14:02:40Z",
        step_end="2026-11-12T14:02:50Z",
    )
    result = summarize(state)
    assert len(result["items"]) == 1
    assert result["items"][0]["first_application"]["at"] == AT
    assert result["changes"][-1]["category"] == "bot_reapplication"
    assert result["changes"][-1]["attribution"] == "confirmed"


def test_maturity_needs_observed_through_not_only_wall_clock():
    state = fixture()
    state["items"][KEY]["observed_through"] = "2026-11-12T00:00:00Z"
    assert summarize(state, "2027-01-01T00:00:00Z")["items"][0]["removal_7"] is None
    state["items"][KEY]["observed_through"] = "2026-11-18T14:02:47Z"
    assert summarize(state, "2026-11-18T14:02:46Z")["items"][0]["removal_7"] is None
    assert summarize(state, "2026-11-18T14:02:47Z")["items"][0]["removal_7"] is True


def test_human_addition_is_not_removal_and_ambiguous_pairs_are_not_invented():
    state = fixture()
    state["events"].pop("e2")
    result = summarize(state)
    assert result["items"][0]["removal_7"] is False
    assert result["changes"][0]["replacement"] is None
    state = fixture()
    state["events"]["e4"] = {**state["events"]["e3"], "id": 10, "label": "area-Other"}
    assert all(row["replacement"] is None for row in summarize(state)["changes"])


def test_fallback_applied_and_removed_between_scans_is_still_visible():
    state = fixture()
    state["events"]["e1"]["label"] = "needs-area-label"
    state["events"]["e2"]["label"] = "needs-area-label"
    state["attempts"]["a1"]["observations"][0].update(
        outcome="fallback_applied", label="needs-area-label"
    )
    result = summarize(state)
    assert result["items"][0]["fallback"]
    assert not result["items"][0]["needs_area"]
    assert result["items"][0]["removal_7"] is None
    assert result["changes"][-1]["category"] == "fallback_to_human_area"
    assert result["changes"][-1]["original_label"] == "needs-area-label"
    assert result["changes"][-1]["elapsed_seconds"] == 14
    assert result["changes"][-1]["run_url"].endswith("/43034653858")


def test_unresolved_fallback_and_no_run_intake_remain_in_denominator():
    state = fixture()
    state["attempts"].clear()
    state["events"].clear()
    state["items"][KEY]["labels"] = ["needs-area-label"]
    result = summarize(state)
    item = result["items"][0]
    assert item["needs_area"] and item["unknown"]
    assert item["intake_1"]["status"] == "no_confirmed_application"
    assert item["intake_24"]["status"] == "no_confirmed_application"


def test_preexisting_human_skip_is_separate():
    state = fixture()
    state["events"]["e1"].update(actor="human", actor_type="User")
    state["attempts"]["a1"]["observations"][0]["outcome"] = "existing_area_skip"
    result = summarize(state)
    assert not result["items"][0]["confirmed"]
    assert result["items"][0]["intake_1"]["status"] == "preexisting_human_skip"


def test_future_deadline_does_not_enter_intake_denominator():
    state = fixture()
    result = report(
        state, CONFIG, instant(state["items"][KEY]["created_at"]) + timedelta(minutes=59)
    )
    assert result["items"][0]["intake_1"] is None


def test_missing_area_without_fallback_or_run_is_not_hidden():
    state = fixture()
    state["items"][KEY]["labels"] = []
    state["events"].clear()
    state["attempts"].clear()
    item = summarize(state)["items"][0]
    assert item["needs_area"]
    assert not item["fallback_marker"]
    assert item["unknown"]
    assert item["original_outcome"] == "no_observed_result"


def test_unavailable_timeline_keeps_unknown_intake_and_blocks_unobserved_followup():
    state = fixture()
    state["items"][KEY]["timeline_availability"] = {"status": "unavailable"}
    state["items"][KEY]["observed_through"] = "2026-11-12T00:00:00Z"
    item = summarize(state)["items"][0]
    assert item["unknown"] and item["timeline_unavailable"]
    assert item["intake_24"] is not None
    assert item["removal_7"] is None


def test_only_first_human_area_addition_resolves_a_waiting_fallback():
    state = fixture()
    state["events"]["e1"]["label"] = "needs-area-label"
    state["events"].pop("e2")
    state["events"]["e4"] = {
        **state["events"]["e3"],
        "id": 10,
        "at": "2026-11-12T14:03:01Z",
        "label": "area-Other",
    }
    categories = [row["category"] for row in summarize(state)["changes"]]
    assert categories == ["fallback_to_human_area", "human_area_added"]


def test_render_does_not_embed_untrusted_text_or_raw_payloads(tmp_path):
    state = fixture()
    state["items"][KEY]["labels"] = ["area-</script><img src=x onerror=alert(1)>"]
    state["items"][KEY]["body"] = "PRIVATE BODY"
    state["runs"]["run1"]["raw_log"] = "PRIVATE LOG"
    render(state, CONFIG, instant("2026-11-19T12:00:00Z"), tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    public = (tmp_path / "data.json").read_text(encoding="utf-8")
    script = (tmp_path / "app.js").read_text(encoding="utf-8")
    assert "<img src=x" not in html
    assert "PRIVATE BODY" not in public and "PRIVATE LOG" not in public
    assert "innerHTML" not in script
    assert "textContent" in script
    assert "setInterval(freshness, 60000)" in script
