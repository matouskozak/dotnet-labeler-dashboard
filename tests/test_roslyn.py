import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_metrics import CONFIG as EXISTING_CONFIG
from test_metrics import fixture

from labeler_dashboard.collect import Collector
from labeler_dashboard.evidence import PARSER_VERSION, parse_log
from labeler_dashboard.metrics import report
from labeler_dashboard.store import area_label, instant

SHA = "46125e85e6a568dc712f358c39f35317366f5eed"
NOW = instant("2026-11-20T12:00:00Z")
ROSLYN_STREAMS = [
    {"repo": "dotnet/roslyn", "kind": "issue", "enabled": True, "workflow_id": 160813423},
    {"repo": "dotnet/roslyn", "kind": "pr", "enabled": True, "workflow_id": 160813424},
]


def roslyn_log(number, message, kind="Issue"):
    return (
        f"2026-11-19T23:57:03Z Download action repository 'dotnet/issue-labeler@{SHA}' (SHA:{SHA})\n"
        f"2026-11-19T23:57:09Z ##[group]Run dotnet/issue-labeler/predict@{SHA}\n"
        "2026-11-19T23:57:09Z   threshold: 0.4\n"
        "2026-11-19T23:57:09Z ##[endgroup]\n"
        f"2026-11-19T23:57:14.9300174Z [{kind} dotnet/roslyn#{number}] {message}"
    )


def roslyn_fixture():
    state = fixture()
    config = {
        "schema": 1,
        "backfill_days": 30,
        "streams": deepcopy(EXISTING_CONFIG["streams"]) + deepcopy(ROSLYN_STREAMS),
    }
    api = Mock()
    collector = Collector(api, state, config, NOW)
    collector.initialize()
    cases = [
        (86547, "Label 'Area-Compilers' applied.", "issue", ["Area-IDE"]),
        (86548, "No prediction made. No applicable label found. No action taken.", "issue", []),
        (
            86557,
            "No prediction needed. Applicable label 'Area-Compilers' already exists.",
            "pr",
            ["Area-Compilers"],
        ),
    ]
    for number, message, kind, labels in cases:
        source = {
            "id": number,
            "number": number,
            "state": "open",
            "created_at": "2026-11-19T23:56:59Z",
            "updated_at": "2026-11-20T00:02:00Z",
            "labels": [{"name": value} for value in [*labels, "vs-insertion"]],
            "html_url": f"https://github.com/dotnet/roslyn/{'pull' if kind == 'pr' else 'issues'}/{number}",
        }
        if kind == "pr":
            source["pull_request"] = {}
        collector.retain_item("dotnet/roslyn", source)
        key = f"dotnet/roslyn:{number}"
        timeline = []
        if number == 86547:
            for identity, at, event, label, actor, actor_type in [
                (
                    36714079755,
                    "2026-11-19T23:57:14Z",
                    "labeled",
                    "Area-Compilers",
                    "github-actions[bot]",
                    "Bot",
                ),
                (
                    36714079756,
                    "2026-11-20T00:01:00Z",
                    "unlabeled",
                    "Area-Compilers",
                    "human",
                    "User",
                ),
                (36714079757, "2026-11-20T00:01:02Z", "labeled", "Area-IDE", "human", "User"),
            ]:
                timeline.append(
                    {
                        "id": identity,
                        "created_at": at,
                        "event": event,
                        "label": {"name": label},
                        "actor": {"login": actor, "type": actor_type},
                    }
                )
        timeline.append(
            {
                "id": number,
                "created_at": "2026-11-20T00:02:00Z",
                "event": "labeled",
                "label": {"name": "vs-insertion"},
                "actor": {"login": "bot", "type": "Bot"},
            }
        )
        api.pages.return_value = [timeline]
        collector.timeline(key, state["items"][key])
        api.pages.return_value = [
            [
                {
                    "id": number,
                    "steps": [
                        {
                            "name": "Predict pull labels"
                            if kind == "pr"
                            else "Predict issue labels",
                            "conclusion": "success",
                            "started_at": "2026-11-19T23:57:09Z",
                            "completed_at": "2026-11-19T23:57:15Z",
                        }
                    ],
                }
            ]
        ]
        api.log.return_value = roslyn_log(
            number, message, "Pull Request" if kind == "pr" else "Issue"
        )
        collector.collect_run(
            next(s for s in ROSLYN_STREAMS if s["kind"] == kind),
            {
                "id": number,
                "run_attempt": 1,
                "created_at": "2026-11-19T23:57:01Z",
                "updated_at": "2026-11-19T23:57:16Z",
                "head_branch": "main",
                "event": "pull_request_target" if kind == "pr" else "issues",
                "status": "completed",
                "conclusion": "success",
                "html_url": f"https://github.com/dotnet/roslyn/actions/runs/{number}",
            },
        )
    return state, config


@pytest.mark.parametrize(
    "message,outcome,label",
    [
        ("Label 'Area-Compilers' applied.", "area_applied", "Area-Compilers"),
        (
            "No prediction needed. Applicable label 'Area-Compilers' already exists.",
            "existing_area_skip",
            "Area-Compilers",
        ),
        ("No prediction made. No applicable label found. No action taken.", "no_selection", None),
        (
            "No prediction was made. The prediction engine did not return any possible predictions.",
            "no_selection",
            None,
        ),
        ("Label 'untriaged' applied.", "unsupported", None),
        ("Label 'needs-area-label' applied.", "unsupported", None),
        ("Label 'area-' applied.", "unsupported", None),
    ],
)
def test_v2_roslyn_result_grammar_is_supported_without_inventing_area_labels(
    message, outcome, label
):
    parsed = parse_log(roslyn_log(86547, message), "dotnet/roslyn")
    assert parsed["action_version"] == "v2.0.0"
    assert parsed["action_sha"] == SHA
    assert parsed["threshold"] == 0.4
    assert [(row["outcome"], row["label"]) for row in parsed["observations"]] == [(outcome, label)]


@pytest.mark.parametrize("label", ["area-Compilers", "Area-Compilers", "AREA-Compilers"])
def test_area_recognition_is_case_insensitive_but_not_substring_based(label):
    assert area_label(label)
    assert not area_label("not-" + label)
    assert not area_label("area")
    assert not area_label("area-")


def test_roslyn_collection_keeps_case_and_feeds_confirmation_changes_daily_and_skip_exclusion():
    state, config = roslyn_fixture()
    output = report(state, config, instant("2026-11-27T12:00:00Z"))
    items = {item["number"]: item for item in output["items"] if item["repo"] == "dotnet/roslyn"}
    assert set(items) == {86547, 86548}
    applied = items[86547]
    assert applied["confirmed"] and not applied["unknown"] and not applied["needs_area"]
    assert applied["first_application"]["label"] == "Area-Compilers"
    assert applied["removal_7"] is None  # Fetch time, not wall-clock, controls maturity.
    assert applied["activity"] == [
        {"date": "2026-11-19", "series": ["applied"]},
        {"date": "2026-11-20", "series": ["human_changed"]},
    ]
    assert next(change for change in output["changes"] if change["item"] == applied["key"]) == {
        "item": applied["key"],
        "before": ["Area-Compilers"],
        "after": ["Area-IDE"],
        "human_after_application": True,
        "actor": "human",
        "at": "2026-11-20T00:01:02Z",
    }
    state["items"][applied["key"]]["observed_through"] = "2026-11-27T12:00:00Z"
    assert next(
        i
        for i in report(state, config, instant("2026-11-27T12:00:00Z"))["items"]
        if i["key"] == applied["key"]
    )["removal_7"]
    abstained = items[86548]
    assert abstained["no_selection"] and abstained["needs_area"]
    assert not abstained["unknown"] and not abstained["technical_failure"]
    assert not abstained["fallback"] and not abstained["fallback_marker"]
    assert abstained["intake_1"]["status"] == "no_selection"
    assert abstained["activity"] == [{"date": "2026-11-19", "series": ["no_selection"]}]
    assert all(event["label"] != "vs-insertion" for event in state["events"].values())
    assert state["items"]["dotnet/roslyn:86557"]["labels"] == ["Area-Compilers"]


def test_adding_roslyn_preserves_existing_history_checkpoints_and_report_counts():
    state = fixture()
    state["cohort_start"] = "2026-10-20T12:00:00Z"
    state["runs"]["run1"]["parser_version"] = PARSER_VERSION
    before = deepcopy(state)
    existing = report(state, EXISTING_CONFIG, NOW)
    config = json.loads(Path("config/repos.json").read_text())
    collector = Collector(Mock(), state, config, NOW)
    collector.initialize()
    for key in ("cohort_start", "items", "events", "attempts", "runs"):
        assert state[key] == before[key]
    for key, progress in before["streams"].items():
        assert state["streams"][key] == progress
    for stream in ROSLYN_STREAMS:
        assert stream in config["streams"]
        progress = state["streams"][f"dotnet/roslyn:{stream['kind']}"]
        assert progress["backfill_to"] == before["cohort_start"]
        assert progress["last_success"] is None
    output = report(state, config, NOW)
    assert output["items"] == existing["items"]
    assert output["changes"] == existing["changes"]
    assert output["configurations"] == existing["configurations"]
    for stream in existing["streams"]:
        actual = next(
            s
            for s in output["streams"]
            if (s["repo"], s["kind"]) == (stream["repo"], stream["kind"])
        )
        for field in ("items", "timelines", "pending_timelines", "run_attempts", "run_evidence"):
            assert actual[field] == stream[field]
