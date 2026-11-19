import json
from copy import deepcopy
from datetime import timedelta

import pytest

from labeler_dashboard.evidence import parse_log
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
        "started_at": "2026-11-11T14:02:20Z",
        "completed_at": "2026-11-11T14:03:00Z",
        "updated_at": "2026-11-11T14:03:00Z",
        "status": "completed",
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
    assert result["changes"] == [
        {
            "item": KEY,
            "at": "2026-11-11T14:03:01Z",
            "actor": "human",
            "before": ["area-Infrastructure"],
            "after": ["area-Trainers"],
        }
    ]
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


@pytest.mark.parametrize("dry_run", [False, True])
def test_manual_claim_competes_for_mutation_unless_dry_run(dry_run):
    state = fixture()
    state["attempts"]["manual"] = deepcopy(state["attempts"]["a1"])
    state["attempts"]["manual"].update(manual=True, dry_run=dry_run)
    item = summarize(state)["items"][0]
    assert item["confirmed"] is dry_run
    assert item["removal_7"] is (True if dry_run else None)
    activity = item["activity"][0]["series"]
    assert ("applied" in activity) is dry_run
    state["attempts"].pop("a1")
    item = summarize(state)["items"][0]
    assert not item["confirmed"]
    assert item["removal_7"] is None
    assert item["activity"] == [{"date": "2026-11-11", "series": ["human_changed"]}]


def test_cleanup_failure_does_not_erase_application_or_human_followup():
    state = fixture()
    parsed = parse_log(
        "2026-11-11T14:02:47Z [Issue dotnet/machinelearning#7901] "
        "Label 'area-Infrastructure' applied. "
        "Error occurred removing default label 'needs-area-label'",
        "dotnet/machinelearning",
    )
    existing = state["attempts"]["a1"]["observations"][0]
    state["attempts"]["a1"]["observations"] = [
        {**existing, **observation} for observation in parsed["observations"]
    ]
    item = summarize(state)["items"][0]
    assert item["confirmed"] and item["technical_failure"]
    assert item["removal_7"] is True
    assert item["activity"] == [{"date": "2026-11-11", "series": ["applied", "human_changed"]}]


@pytest.mark.parametrize("unknown", ["queued", "empty", "unsupported", "unknown_action"])
@pytest.mark.parametrize("known", ["existing_area_skip", "area_applied", "application_failure"])
def test_unknown_is_classified_per_automatic_attempt_not_flattened_results(unknown, known):
    state = fixture()
    state["attempts"]["a1"]["observations"][0]["outcome"] = known
    unresolved = deepcopy(state["attempts"]["a1"])
    if unknown == "empty":
        unresolved["observations"] = []
    elif unknown == "unknown_action":
        unresolved["observations"][0]["action_version"] = None
    else:
        unresolved["observations"][0]["outcome"] = unknown
    state["attempts"]["unresolved"] = unresolved
    result = summarize(state)
    assert result["items"][0]["unknown"]
    assert result["streams"][0]["production_incomplete_runs"] == 1
    for mode in ("manual", "dry_run"):
        state["attempts"]["unresolved"] = {**unresolved, mode: True}
        result = summarize(state)
        if known == "existing_area_skip":
            assert not result["items"]
        else:
            assert not result["items"][0]["unknown"]
        assert result["streams"][0]["production_incomplete_runs"] == 0


def test_known_item_failure_is_not_unknown_but_unassigned_failed_run_is_incomplete():
    state = fixture()
    state["runs"]["run1"]["evidence"] = "technical_failure"
    state["attempts"]["a1"]["observations"][0]["outcome"] = "application_failure"
    result = summarize(state)
    item = result["items"][0]
    assert item["technical_failure"] and not item["unknown"]
    assert item["intake_1"] == {"status": "technical_failure", "evidence_complete": True}
    assert result["streams"][0]["production_incomplete_runs"] == 0
    state["attempts"].clear()
    result = summarize(state)
    item = result["items"][0]
    assert item["unknown"] and not item["technical_failure"]
    assert not item["intake_1"]["evidence_complete"]
    assert result["streams"][0]["production_incomplete_runs"] == 1


@pytest.mark.parametrize("scenario", ["unsupported_action", "unsupported_result", "queued_only"])
def test_unknown_automatic_attempt_keeps_intake_denominator_but_not_completeness(scenario):
    state = fixture()
    observation = state["attempts"]["a1"]["observations"][0]
    if scenario == "unsupported_action":
        observation["action_version"] = None
    else:
        observation["outcome"] = "queued" if scenario == "queued_only" else "unsupported"
    item = summarize(state)["items"][0]
    assert item["unknown"]
    for hours in (1, 24):
        assert item[f"intake_{hours}"] is not None
        assert not item[f"intake_{hours}"]["evidence_complete"]


@pytest.mark.parametrize("mode", ["no_run", "manual", "dry_run"])
def test_complete_intake_keeps_no_run_opportunities_and_ignores_nonproduction_unknowns(mode):
    state = fixture()
    if mode == "no_run":
        state["attempts"].clear()
        state["runs"].clear()
    else:
        state["attempts"]["a1"][mode] = True
        state["attempts"]["a1"]["observations"][0]["outcome"] = "unsupported"
        state["runs"]["run1"]["evidence"] = "unknown"
        if mode == "manual":
            state["runs"]["run1"]["manual"] = True
        else:
            state["runs"]["run1"]["branch"] = "main"
            state["runs"]["run1"]["configs"] = [
                {
                    "dry_run": True,
                    "threshold": None,
                    "action_sha": None,
                    "action_version": None,
                    "cache_hit": None,
                }
            ]
    item = summarize(state)["items"][0]
    for hours in (1, 24):
        assert item[f"intake_{hours}"]["status"] == "no_confirmed_application"
        assert item[f"intake_{hours}"]["evidence_complete"]


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
    assert result["changes"][-1]["actor"] == "github-actions[bot]"
    assert result["changes"][-1]["before"] == ["area-Trainers"]
    assert result["changes"][-1]["after"] == ["area-Infrastructure", "area-Trainers"]


def test_maturity_needs_observed_through_not_only_wall_clock():
    state = fixture()
    state["items"][KEY]["observed_through"] = "2026-11-12T00:00:00Z"
    assert summarize(state, "2027-01-01T00:00:00Z")["items"][0]["removal_7"] is None
    state["items"][KEY]["observed_through"] = "2026-11-18T14:02:47Z"
    assert summarize(state, "2026-11-18T14:02:46Z")["items"][0]["removal_7"] is None
    assert summarize(state, "2026-11-18T14:02:47Z")["items"][0]["removal_7"] is True


def test_additional_area_labels_are_not_misreported_as_replacements():
    state = fixture()
    state["events"].pop("e2")
    result = summarize(state)
    assert result["items"][0]["removal_7"] is False
    assert result["changes"][0]["before"] == ["area-Infrastructure"]
    assert result["changes"][0]["after"] == ["area-Infrastructure", "area-Trainers"]
    state = fixture()
    state["events"]["e4"] = {**state["events"]["e3"], "id": 35805626722, "label": "area-Other"}
    changes = summarize(state)["changes"]
    assert len(changes) == 2
    assert changes[0]["after"] == changes[1]["before"] == ["area-Trainers"]
    assert changes[1]["after"] == ["area-Other", "area-Trainers"]


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
    assert len(result["changes"]) == 1
    assert result["changes"][0]["before"] == ["needs-area-label"]
    assert result["changes"][0]["after"] == ["area-Trainers"]


@pytest.mark.parametrize("add_first", [True, False])
def test_runtime_137854_three_second_edit_shows_original_label_without_run_evidence(add_first):
    state = fixture()
    state["attempts"].clear()
    state["runs"].clear()
    old_label = "area-NativeAOT-coreclr"
    new_label = "area-System.Diagnostics.Process"
    state["items"][KEY]["labels"] = [new_label]
    state["events"]["e1"].update(at="2026-11-18T17:52:56Z", label=old_label)
    state["events"]["e2"].update(
        at="2026-11-18T19:52:34Z" if add_first else "2026-11-18T19:52:31Z",
        label=old_label,
        actor="jkotas",
    )
    state["events"]["e3"].update(
        at="2026-11-18T19:52:31Z" if add_first else "2026-11-18T19:52:34Z",
        label=new_label,
        actor="jkotas",
    )
    before = deepcopy(state)
    result = summarize(state)
    assert result["changes"] == [
        {
            "item": KEY,
            "at": "2026-11-18T19:52:34Z",
            "actor": "jkotas",
            "before": [old_label],
            "after": [new_label],
        }
    ]
    assert result["items"][0]["unknown"]
    assert not result["items"][0]["confirmed"]
    assert result["items"][0]["first_application"] is None
    assert result["items"][0]["removal_7"] is None
    assert state == before


@pytest.mark.parametrize(
    ("delay", "actor", "expected_rows"),
    [(5, "human", 1), (6, "human", 2), (3600, "human", 2), (0, "someone-else", 2), (0, None, 2)],
)
def test_edit_grouping_does_not_merge_distant_or_different_actor_changes(
    delay, actor, expected_rows
):
    state = fixture()
    state["events"]["e3"]["at"] = (
        (instant(state["events"]["e2"]["at"]) + timedelta(seconds=delay))
        .isoformat()
        .replace("+00:00", "Z")
    )
    state["events"]["e3"]["actor"] = actor
    changes = summarize(state)["changes"]
    assert len(changes) == expected_rows
    if expected_rows == 2:
        assert changes[0]["before"] == ["area-Infrastructure"]
        assert changes[0]["after"] == changes[1]["before"] == []
        assert changes[1]["after"] == ["area-Trainers"]


def test_intervening_edit_prevents_grouping_and_initial_state_is_explicit():
    state = fixture()
    state["events"]["e1"].update(actor="first-human", actor_type="User")
    state["events"]["middle"] = {
        **state["events"]["e2"],
        "id": 35805626633,
        "event": "labeled",
        "label": "area-Other",
        "actor": "someone-else",
    }
    changes = summarize(state)["changes"]
    assert len(changes) == 4
    assert changes[0]["before"] == []
    assert changes[0]["after"] == ["area-Infrastructure"]
    assert changes[1]["after"] == changes[2]["before"] == []
    assert changes[2]["after"] == changes[3]["before"] == ["area-Other"]
    assert changes[3]["after"] == ["area-Other", "area-Trainers"]


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


@pytest.mark.parametrize("timeline_complete", [True, False])
def test_prelabeled_137902_skip_only_is_excluded_everywhere_without_deleting_history(
    tmp_path, timeline_complete
):
    state = fixture()
    key = "dotnet/runtime:6488648182"
    item = state["items"].pop(KEY)
    item.update(
        repo="dotnet/runtime",
        id=6488648182,
        number=137902,
        state="open",
        url="https://github.com/dotnet/runtime/issues/137902",
        created_at="2026-11-19T11:34:28Z",
        labels=["area-CodeGen-coreclr"],
    )
    if not timeline_complete:
        item.pop("observed_through")
    state["items"][key] = item
    state["events"] = {
        "e1": {
            **state["events"]["e1"],
            "repo": item["repo"],
            "item": key,
            "at": "2026-11-19T11:34:29Z",
            "label": "area-CodeGen-coreclr",
            "actor": "EgorBo",
            "actor_type": "User",
        }
    }
    run = state["runs"]["run1"]
    run.update(repo=item["repo"], created_at="2026-11-19T11:34:32Z")
    attempt = state["attempts"]["a1"]
    attempt.update(repo=item["repo"], number=item["number"])
    observation = attempt["observations"][0]
    observation.update(
        at="2026-11-19T11:34:48Z", outcome="existing_area_skip", label="area-CodeGen-coreclr"
    )
    attempt["observations"].insert(0, {**observation, "outcome": "queued", "label": None})
    state["attempts"]["a2"] = deepcopy(attempt)
    retained_key = "dotnet/runtime:999"
    state["items"][retained_key] = {
        **item,
        "id": 999,
        "number": 999,
        "url": "https://github.com/dotnet/runtime/issues/999",
        "labels": [],
        "observed_through": None,
    }
    config = {"streams": [{"repo": "dotnet/runtime", "kind": "issue", "enabled": True}]}
    before = deepcopy(state)
    render(state, config, instant("2026-11-21T12:00:00Z"), tmp_path)
    public = (tmp_path / "data.json").read_text(encoding="utf-8")
    result = json.loads(public)
    assert "137902" not in public
    assert [row["key"] for row in result["items"]] == [retained_key]
    assert result["changes"] == []
    assert result["streams"][0]["items"] == 1
    assert result["streams"][0]["timelines"] == 0
    assert result["streams"][0]["pending_timelines"] == 1
    assert result["streams"][0]["run_attempts"] == 1
    assert sum(row["intake_1"] is not None for row in result["items"]) == 1
    assert sum(row["intake_24"] is not None for row in result["items"]) == 1
    assert state == before


@pytest.mark.parametrize(
    "outcome",
    [
        "area_applied",
        "fallback_applied",
        "fallback_present",
        "no_selection",
        "application_failure",
        "unsupported",
        "unknown",
        "queued",
    ],
)
def test_automatic_prediction_or_unknown_attempt_followed_by_skip_stays_visible(outcome):
    state = fixture()
    state["attempts"]["a1"]["observations"][0]["outcome"] = outcome
    state["attempts"]["a2"] = deepcopy(state["attempts"]["a1"])
    state["attempts"]["a2"]["observations"][0].update(
        at="2026-11-12T14:02:47Z",
        outcome="existing_area_skip",
    )
    result = summarize(state)
    assert [row["key"] for row in result["items"]] == [KEY]
    assert result["items"][0]["production_attempt_count"] == 2
    assert result["streams"][0]["items"] == 1
    assert result["changes"]
    if outcome == "area_applied":
        assert result["items"][0]["confirmed"]
        assert result["items"][0]["removal_7"] is True
    # A supported earlier skip must not erase a later prediction either.
    state["attempts"]["a1"]["observations"][0]["outcome"] = "existing_area_skip"
    state["attempts"]["a2"]["observations"][0]["outcome"] = outcome
    assert summarize(state)["items"][0]["key"] == KEY


@pytest.mark.parametrize("excluded_mode", ["manual", "dry_run"])
@pytest.mark.parametrize("automatic_prediction", [False, True])
def test_manual_or_dry_run_skip_does_not_exclude_an_item(excluded_mode, automatic_prediction):
    state = fixture()
    skip = deepcopy(state["attempts"]["a1"])
    skip[excluded_mode] = True
    skip["observations"][0]["outcome"] = "existing_area_skip"
    if not automatic_prediction:
        state["attempts"].clear()
    state["attempts"]["skip"] = skip
    item = summarize(state)["items"][0]
    assert item["key"] == KEY
    assert item["production_attempt_count"] == int(automatic_prediction)
    assert item["confirmed"] == automatic_prediction


@pytest.mark.parametrize("scenario", ["no_run", "unsupported_skip", "empty_attempt"])
def test_absent_or_unsupported_outcomes_do_not_prove_a_prelabel_skip(scenario):
    state = fixture()
    state["attempts"]["a1"]["observations"][0]["outcome"] = "existing_area_skip"
    if scenario == "no_run":
        state["attempts"].clear()
    elif scenario == "unsupported_skip":
        state["attempts"]["a1"]["observations"][0]["action_version"] = None
    else:
        state["attempts"]["a2"] = {**state["attempts"]["a1"], "observations": []}
    result = summarize(state)
    assert result["items"][0]["key"] == KEY
    assert result["streams"][0]["items"] == 1
    assert result["changes"]


def test_exclusion_is_recomputed_when_new_prediction_evidence_arrives():
    state = fixture()
    prediction = deepcopy(state["attempts"]["a1"])
    state["attempts"]["a1"]["observations"][0]["outcome"] = "existing_area_skip"
    assert not summarize(state)["items"]
    state["attempts"]["a2"] = prediction
    assert summarize(state)["items"][0]["confirmed"]


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


def test_fallback_marker_remains_in_display_until_actually_removed():
    state = fixture()
    state["events"]["e1"]["label"] = "needs-area-label"
    state["events"].pop("e2")
    state["events"]["e4"] = {
        **state["events"]["e3"],
        "id": 10,
        "at": "2026-11-12T14:03:01Z",
        "label": "area-Other",
    }
    changes = summarize(state)["changes"]
    assert changes[0]["before"] == ["needs-area-label"]
    assert changes[0]["after"] == changes[1]["before"] == ["area-Trainers", "needs-area-label"]
    assert changes[1]["after"] == ["area-Other", "area-Trainers", "needs-area-label"]


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
