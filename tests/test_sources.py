import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from labeler_dashboard.collect import Collector
from labeler_dashboard.evidence import parse_log
from labeler_dashboard.metrics import report
from labeler_dashboard.store import area_label, empty_state, instant, relevant_label

NOW = instant("2026-11-21T07:00:00Z")
ACTION_SHA = "46125e85e6a568dc712f358c39f35317366f5eed"
ASPIRE_ACTION_SHA = "160b6b1e1e8d36da09beb34ef1e4806d2c0520a3"


def config():
    return json.loads(Path("config/repos.json").read_text())


def item_source(repo, number, kind="issue", labels=()):
    item = {
        "id": number,
        "number": number,
        "state": "open",
        "created_at": "2026-11-20T18:42:30Z",
        "updated_at": "2026-11-20T19:00:02Z",
        "html_url": f"https://github.com/{repo}/{'pull' if kind == 'pr' else 'issues'}/{number}",
        "labels": [{"name": label} for label in labels],
    }
    if kind == "pr":
        item["pull_request"] = {}
    return item


def prediction_log(repo, number, message, kind="issue"):
    action_sha = ASPIRE_ACTION_SHA if repo == "microsoft/aspire" else ACTION_SHA
    return (
        f"2026-11-20T18:42:45Z Download action repository 'dotnet/issue-labeler@{action_sha}' (SHA:{action_sha})\n"
        f"2026-11-20T18:42:49Z ##[group]Run dotnet/issue-labeler/predict@{action_sha}\n"
        "2026-11-20T18:42:49Z   threshold: 0.4\n"
        "2026-11-20T18:42:49Z ##[endgroup]\n"
        f"2026-11-20T18:42:52.6732822Z [{'Pull Request' if kind == 'pr' else 'Issue'} {repo}#{number}] {message}"
    )


def source_fixture():
    state, api = empty_state(), Mock()
    settings = config()
    collector = Collector(api, state, settings, NOW)
    collector.initialize()
    for repo, number, kind, label, outcome in [
        ("gitextensions/gitextensions", 13466, "issue", "area: worktree", "applied"),
        ("gitextensions/gitextensions", 13467, "pr", "area: worktree", "skip"),
        ("gitextensions/gitextensions", 13468, "issue", "needs-area-label", "fallback"),
        ("dotnet/extensions", 7971, "issue", "needs-area-label", "fallback"),
        ("dotnet/extensions", 7972, "pr", "area-ai", "applied"),
        ("microsoft/aspire", 22511, "issue", "needs-area-label", "fallback"),
        ("microsoft/aspire", 900001, "pr", "area-test", "applied"),
    ]:
        api.get.return_value = {"base": {"ref": "release/4.0"}, "merged_at": None}
        collector.retain_item(repo, item_source(repo, number, kind, [label]))
        key = f"{repo}:{number}"
        events = [
            {
                "id": number * 10,
                "event": "labeled",
                "created_at": "2026-11-20T18:42:51Z",
                "label": {"name": label},
                "actor": {"login": "github-actions[bot]", "type": "Bot"},
            }
        ]
        if repo == "gitextensions/gitextensions" and outcome == "applied":
            events.extend(
                [
                    {
                        "id": number * 10 + 1,
                        "event": "unlabeled",
                        "created_at": "2026-11-20T19:00:00Z",
                        "label": {"name": label},
                        "actor": {"login": "human", "type": "User"},
                    },
                    {
                        "id": number * 10 + 2,
                        "event": "labeled",
                        "created_at": "2026-11-20T19:00:02Z",
                        "label": {"name": "area: merge"},
                        "actor": {"login": "human", "type": "User"},
                    },
                ]
            )
            state["items"][key]["labels"] = ["area: merge"]
        api.pages.return_value = [events]
        collector.timeline(key, state["items"][key])
        api.pages.return_value = [
            [
                {
                    "id": number,
                    "name": "predict",
                    "steps": [
                        {
                            "name": "Predict issue labels"
                            if kind == "issue"
                            else "Predict pull labels",
                            "conclusion": "success",
                            "started_at": "2026-11-20T18:42:49Z",
                            "completed_at": "2026-11-20T18:42:53Z",
                        }
                    ],
                }
            ]
        ]
        message = (
            f"Label '{label}' applied."
            if outcome == "applied"
            else f"No prediction needed. Applicable label '{label}' already exists."
            if outcome == "skip"
            else "No prediction made. Default label 'needs-area-label' applied."
        )
        api.log.return_value = prediction_log(repo, number, message, kind)
        collector.collect_run(
            next(s for s in settings["streams"] if (s["repo"], s["kind"]) == (repo, kind)),
            {
                "id": number,
                "run_attempt": 1,
                "status": "completed",
                "conclusion": "success",
                "created_at": "2026-11-20T18:42:45Z",
                "updated_at": "2026-11-20T18:42:53Z",
                "event": "issues" if kind == "issue" else "pull_request_target",
                "head_branch": "main",
                "html_url": f"https://github.com/{repo}/actions/runs/{number}",
            },
        )
    return state, settings


@pytest.mark.parametrize(
    "repo,label,expected",
    [
        ("gitextensions/gitextensions", "area: worktree", True),
        ("gitextensions/gitextensions", "Area: Worktree", True),
        ("gitextensions/gitextensions", "area-worktree", False),
        ("gitextensions/gitextensions", "area: ", False),
        ("gitextensions/gitextensions", "area:worktree", False),
        ("dotnet/extensions", "area-ai", True),
        ("dotnet/extensions", "area: worktree", False),
        ("dotnet/runtime", "area: worktree", False),
        ("dotnet/roslyn", "Area-Compilers", True),
        ("microsoft/aspire", "area-test", True),
        ("microsoft/aspire", "area: test", False),
        ("other/gitextensions", "area: worktree", False),
    ],
)
def test_area_taxonomy_is_repository_specific(repo, label, expected):
    assert area_label(label, repo) is expected
    parsed = parse_log(prediction_log(repo, 1, f"Label '{label}' applied."), repo)
    assert parsed["observations"][0]["outcome"] == ("area_applied" if expected else "unsupported")
    assert relevant_label("needs-area-label", repo)


def test_colon_prefix_flows_through_collection_confirmation_history_daily_and_skip_exclusion():
    state, settings = source_fixture()
    output = report(state, settings, NOW)
    items = {(i["repo"], i["number"]): i for i in output["items"]}
    applied = items["gitextensions/gitextensions", 13466]
    assert applied["confirmed"] and not applied["unknown"] and not applied["needs_area"]
    assert applied["first_application"]["label"] == "area: worktree"
    assert applied["activity"] == [{"date": "2026-11-20", "series": ["applied", "human_changed"]}]
    assert next(c for c in output["changes"] if c["item"] == applied["key"]) == {
        "item": applied["key"],
        "actor": "human",
        "at": "2026-11-20T19:00:02Z",
        "before": ["area: worktree"],
        "after": ["area: merge"],
        "human_after_application": True,
    }
    assert ("gitextensions/gitextensions", 13467) not in items
    state["items"][applied["key"]]["observed_through"] = "2026-11-28T07:00:00Z"
    matured = report(state, settings, instant("2026-11-28T07:00:00Z"))
    assert next(i for i in matured["items"] if i["key"] == applied["key"])["removal_7"]
    for repo, number in [
        ("gitextensions/gitextensions", 13468),
        ("dotnet/extensions", 7971),
        ("microsoft/aspire", 22511),
    ]:
        item = items[repo, number]
        assert item["no_selection"] and item["fallback"] and item["needs_area"]
        assert not item["unknown"]
        assert item["activity"] == [{"date": "2026-11-20", "series": ["no_selection"]}]
    assert items["dotnet/extensions", 7972]["confirmed"]
    assert items["microsoft/aspire", 900001]["confirmed"]
    assert all(
        c["action_version"] == ("v2.2.0" if c["repo"] == "microsoft/aspire" else "v2.0.0")
        and c["threshold"] == 0.4
        for c in output["configurations"]
    )
    for stream in output["streams"]:
        if stream["enabled"] and stream["run_attempts"]:
            assert stream["labeler_version"] == (
                "v2.2.0" if stream["repo"] == "microsoft/aspire" else "v2.0.0"
            )


def test_deterministic_triage_and_skipped_predictor_do_not_invent_model_evidence():
    settings, state, api = config(), empty_state(), Mock()
    collector = Collector(api, state, settings, NOW)
    collector.initialize()
    repo = "gitextensions/gitextensions"
    source = item_source(repo, 13469, labels=["status: workaround available"])
    source["state"] = "closed"
    collector.retain_item(repo, source)
    api.pages.return_value = [
        [
            {
                "id": 1,
                "name": "triage-known-windows-update-error",
                "conclusion": "success",
                "steps": [{"name": "Triage known Windows update error", "conclusion": "success"}],
            },
            {
                "id": 2,
                "name": "predict-issue-label",
                "conclusion": "skipped",
                "steps": [{"name": "Predict issue labels", "conclusion": "skipped"}],
            },
        ]
    ]
    collector.collect_run(
        next(s for s in settings["streams"] if s["repo"] == repo and s["kind"] == "issue"),
        {
            "id": 1,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-11-20T18:42:45Z",
            "updated_at": "2026-11-20T18:42:53Z",
            "event": "issues",
            "head_branch": "master",
            "html_url": f"https://github.com/{repo}/actions/runs/1",
        },
    )
    api.log.assert_not_called()
    assert not state["attempts"]
    assert state["runs"][f"{repo}:1:1"]["evidence"] == "unknown"
    item = report(state, settings, NOW)["items"][0]
    assert item["state"] == "closed" and item["labels"] == []
    assert item["unknown"] and not item["confirmed"] and not item["no_selection"]
    assert item["activity"] == []


@pytest.mark.parametrize(
    "branch,included",
    [("main", True), ("release/4.0", True), ("master", False), ("dev", False), (None, True)],
)
@pytest.mark.parametrize("repo", ["gitextensions/gitextensions", "microsoft/aspire"])
def test_scoped_pr_bases_exclude_known_ineligible_opportunities_but_keep_unknowns(
    repo, branch, included
):
    state, api, settings = empty_state(), Mock(), config()
    c = Collector(api, state, settings, NOW)
    c.initialize()
    api.get.return_value = {"base": {"ref": branch}}
    c.retain_item(repo, item_source(repo, 1, "pr"))
    api.get.assert_called_once_with(f"/repos/{repo}/pulls/1")
    assert state["items"][f"{repo}:1"]["base_branch"] == branch
    output = report(state, settings, NOW)
    assert bool(output["items"]) is included
    if included and branch is None:
        assert output["items"][0]["unknown"]
        assert not output["items"][0]["intake_1"]["evidence_complete"]
    api.get.reset_mock()
    c.retain_item("dotnet/extensions", item_source("dotnet/extensions", 2, "pr"))
    api.get.assert_not_called()
    assert any(i["repo"] == "dotnet/extensions" for i in report(state, settings, NOW)["items"])


@pytest.mark.parametrize("event", ["pull_request_target", "schedule"])
def test_aspire_assignee_job_or_disabled_poll_is_not_prediction_evidence(event):
    state, api, settings = empty_state(), Mock(), config()
    collector = Collector(api, state, settings, NOW)
    collector.initialize()
    repo = "microsoft/aspire"
    api.get.return_value = {"base": {"ref": "main"}}
    collector.retain_item(repo, item_source(repo, 900002, "pr", ["needs-assignee"]))
    predictor_steps = [
        {
            "name": "Predict pull labels",
            "conclusion": "skipped",
        }
    ]
    if event == "schedule":
        predictor_steps.extend(
            [
                {"name": "Determine pull requests to process", "conclusion": "success"},
                {"name": "Restore pulls model from cache", "conclusion": "skipped"},
            ]
        )
    api.pages.return_value = [
        [
            {
                "id": 1,
                "name": "update-copilot-assignee-label",
                "conclusion": "success",
                "steps": [{"name": "Update assignee label", "conclusion": "success"}],
            },
            {"id": 2, "name": "poll-pull-requests", "conclusion": "skipped", "steps": []},
            {"id": 3, "name": "predict-pull-label", "steps": predictor_steps},
        ]
    ]
    collector.collect_run(
        next(s for s in settings["streams"] if (s["repo"], s["kind"]) == (repo, "pr")),
        {
            "id": 900002,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-11-20T18:42:45Z",
            "updated_at": "2026-11-20T18:42:53Z",
            "event": event,
            "head_branch": "main",
            "html_url": f"https://github.com/{repo}/actions/runs/900002",
        },
    )
    api.log.assert_not_called()
    assert not state["attempts"]
    assert state["runs"][f"{repo}:900002:1"]["evidence"] == (
        "no_work" if event == "schedule" else "unknown"
    )
    item = report(state, settings, NOW)["items"][0]
    assert item["labels"] == [] and item["unknown"]
    assert not item["confirmed"] and not item["no_selection"]
    assert item["activity"] == []


def test_automatic_evidence_survives_retargeting_but_manual_only_outside_scope_is_not_counted():
    state, settings = source_fixture()
    item = state["items"]["gitextensions/gitextensions:13466"]
    item.update(kind="pr", base_branch="master")
    run = state["runs"]["gitextensions/gitextensions:13466:1"]
    run["kind"] = "pr"
    attempt = state["attempts"]["gitextensions/gitextensions:13466:1:13466"]
    attempt["kind"] = "pr"
    assert any(
        i["key"] == "gitextensions/gitextensions:13466"
        for i in report(state, settings, NOW)["items"]
    )
    attempt["manual"] = True
    assert not any(
        i["key"] == "gitextensions/gitextensions:13466"
        for i in report(state, settings, NOW)["items"]
    )
    assert "gitextensions/gitextensions:13466" in state["items"]


def test_new_sources_initialize_without_resetting_existing_five_streams():
    settings = config()
    original_settings = {
        **settings,
        "streams": [
            s
            for s in settings["streams"]
            if s["repo"] in {"dotnet/runtime", "dotnet/machinelearning", "dotnet/roslyn"}
        ],
    }
    state = empty_state()
    c = Collector(Mock(), state, original_settings, NOW)
    c.initialize()
    for index, stream in enumerate(c.enabled):
        c.retain_item(stream["repo"], item_source(stream["repo"], index + 1, stream["kind"]))
    before = deepcopy(state)
    previous = report(state, original_settings, NOW)
    Collector(Mock(), state, settings, NOW).initialize()
    for key in ("cohort_start", "items", "events", "attempts", "runs"):
        assert state[key] == before[key]
    for key, value in before["streams"].items():
        assert state["streams"][key] == value
    after = report(state, settings, NOW)
    assert after["items"] == previous["items"]
    assert after["changes"] == previous["changes"]
    for stream in previous["streams"]:
        assert (
            next(
                s
                for s in after["streams"]
                if (s["repo"], s["kind"]) == (stream["repo"], stream["kind"])
            )
            == stream
        )
