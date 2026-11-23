from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import timedelta
from fnmatch import fnmatchcase

from .evidence import PARSER_VERSION, no_selection
from .store import (
    area_label,
    instant,
    pending_timeline,
    stamp,
    stream_key,
    timeline_coverage_through,
)


def production(attempt):
    return not attempt["manual"] and not attempt["dry_run"]


def supported_terminal_evidence(attempt):
    results = [row for row in attempt["observations"] if row["outcome"] != "queued"]
    return (
        attempt.get("parser_version", PARSER_VERSION) == PARSER_VERSION
        and bool(results)
        and all(
            row["action_version"]
            and row["outcome"]
            in {
                "area_applied",
                "fallback_applied",
                "fallback_present",
                "existing_area_skip",
                "fallback_removed",
                "no_selection",
                "application_failure",
            }
            for row in results
        )
    )


def automatic_run(run):
    configs = run.get("configs", [])
    return not run.get("manual", False) and not (
        configs and all(config.get("dry_run") is True for config in configs)
    )


def execution_bounds(run):
    start = run.get("started_at") or run.get("created_at")
    end = run.get("completed_at")
    if not end and (run.get("status") == "completed" or run.get("collected")):
        end = run.get("updated_at")
    return start or "", end or "~"


def execution_overlaps(run, lower, upper):
    start, end = execution_bounds(run)
    return start < upper and end >= lower


class ExecutionIntervals:
    """Merged closed execution intervals queried against half-open reporting windows."""

    def __init__(self, runs):
        self.starts = []
        self.ends = []
        for start, end in sorted(execution_bounds(run) for run in runs):
            if self.ends and start <= self.ends[-1]:
                self.ends[-1] = max(self.ends[-1], end)
            else:
                self.starts.append(start)
                self.ends.append(end)

    def overlaps(self, lower, upper):
        index = bisect_left(self.starts, upper) - 1
        return index >= 0 and self.ends[index] >= lower


def intake_run_unresolved(run):
    return automatic_run(run) and (
        not run["collected"]
        or run["evidence"] in {"unknown", "unavailable", "parser_upgrade_pending"}
        or (
            run["evidence"] == "technical_failure"
            and not run.get("has_terminal_item_evidence", False)
        )
    )


def prediction_ran(run):
    # Collection records a config or log gap for every prediction step that was not skipped.
    return bool(run.get("configs") or run.get("gaps"))


def daily_run_unresolved(run):
    return (
        automatic_run(run)
        and run.get("status") != "queued"
        and (
            not run["collected"]
            or run["evidence"] == "parser_upgrade_pending"
            or prediction_ran(run)
            and (
                run["evidence"] not in {"item_results", "no_work"}
                or any(not config["action_version"] for config in run.get("configs", []))
            )
        )
    )


def daily_execution(run):
    return {**run, "completed_at": run.get("completed_at") or run.get("updated_at")}


def attempt_overlaps(attempt, run, lower, upper):
    starts = [row["step_start"] for row in attempt["observations"] if row.get("step_start")]
    ends = [row["step_end"] for row in attempt["observations"] if row.get("step_end")]
    if starts and ends:
        return min(starts) < upper and max(ends) >= lower
    return execution_overlaps(run, lower, upper)


def only_existing_area_skips(attempts):
    if not attempts:
        return False
    for attempt in attempts:
        results = [row for row in attempt["observations"] if row["outcome"] != "queued"]
        if not results or any(
            row["outcome"] != "existing_area_skip"
            or not row["action_version"]
            or not area_label(row["label"] or "", attempt["repo"])
            for row in results
        ):
            return False
    return True


def reconstruct(state):
    events = defaultdict(list)
    attempts = defaultdict(list)
    for key, event in state["events"].items():
        events[event["item"]].append({**event, "key": key})
    for rows in events.values():
        rows.sort(key=lambda row: (row["at"], row["id"]))
    number_to_key = {(item["repo"], item["number"]): key for key, item in state["items"].items()}
    for key, attempt in state["attempts"].items():
        item_key = number_to_key.get((attempt["repo"], attempt["number"]))
        if item_key:
            attempts[item_key].append({**attempt, "key": key})
    candidates = {}
    claims = defaultdict(list)
    observations = {}
    automatic = set()
    for item_key, rows in attempts.items():
        through = state["items"][item_key].get("observed_through")
        for attempt in rows:
            if attempt["dry_run"]:
                continue
            for index, row in enumerate(attempt["observations"]):
                if row["outcome"] not in {"area_applied", "fallback_applied"}:
                    continue
                identity = (attempt["key"], index)
                observations[identity] = {**row, "run": attempt["run"], "item": item_key}
                if production(attempt):
                    automatic.add(identity)
                if (
                    not row["action_version"]
                    or not row["step_start"]
                    or not row["step_end"]
                    or not through
                    or through < row["at"]
                ):
                    continue
                upper = min(instant(row["step_end"]), instant(row["at"]) + timedelta(seconds=1))
                matching = [
                    event["key"]
                    for event in events[item_key]
                    if event["event"] == "labeled"
                    and event["label"] == row["label"]
                    and event["actor"] == "github-actions[bot]"
                    and event["actor_type"] == "Bot"
                    and instant(row["step_start"]) <= instant(event["at"]) <= upper
                ]
                candidates[identity] = matching
                for event_key in matching:
                    claims[event_key].append(identity)
    confirmed = {}
    for identity, matching in candidates.items():
        if identity in automatic and len(matching) == 1 and len(claims[matching[0]]) == 1:
            confirmed[matching[0]] = observations[identity]
    return events, attempts, confirmed


def removal_followup(application, events, now, days, through):
    if not application:
        return None
    deadline = instant(application["at"]) + timedelta(days=days)
    if now < deadline or not through or instant(through) < deadline:
        return None
    return any(
        event["event"] == "unlabeled"
        and event["actor_type"] == "User"
        and event["label"] == application["label"]
        and instant(application["at"]) <= instant(event["at"]) <= deadline
        for event in events
    )


def label_changes(item_key, events):
    rows = []
    labels = set()
    human_removed = set()
    index = 0
    while index < len(events):
        event = events[index]
        before = labels.copy()
        edits = [event]
        if index + 1 < len(events):
            following = events[index + 1]
            addition, removal = (
                (event, following) if event["event"] == "labeled" else (following, event)
            )
            # Presentation only: an adjacent add/remove pair, never attribution evidence.
            if (
                event["actor"]
                and event["actor"] == following["actor"]
                and event["actor_type"] == following["actor_type"] == "User"
                and 0 <= (instant(following["at"]) - instant(event["at"])).total_seconds() <= 5
                and addition["event"] == "labeled"
                and removal["event"] == "unlabeled"
                and removal["label"] in before
                and addition["label"] not in before
            ):
                edits.append(following)
        visible = event["actor_type"] == "User" or (
            event["actor_type"] == "Bot"
            and event["event"] == "labeled"
            and event["label"] in human_removed
        )
        for edit in edits:
            if edit["event"] == "labeled":
                labels.add(edit["label"])
            else:
                labels.discard(edit["label"])
                if edit["actor_type"] == "User":
                    human_removed.add(edit["label"])
        if visible and before != labels:
            rows.append(
                {
                    "item": item_key,
                    "at": edits[-1]["at"],
                    "actor": event["actor"],
                    "before": sorted(before),
                    "after": sorted(labels),
                }
            )
        index += len(edits)
    return rows


def intake_view(
    item, events, attempts, applications, progress, run_gaps, run_lookup, now, hours, through
):
    deadline = instant(item["created_at"]) + timedelta(hours=hours)
    if now < deadline:
        return None
    end = stamp(deadline)
    upper = stamp(deadline + timedelta(seconds=1))
    covered = progress.get("backfill_to", "") >= end or (
        progress.get("live_from", "~") <= item["created_at"] and progress.get("live_to", "") >= end
    )
    complete = bool(
        covered
        and (through or "") >= end
        and not run_gaps.overlaps(item["created_at"], upper)
        and not any(
            production(attempt)
            and attempt_overlaps(
                attempt, run_lookup.get(attempt["run"], {}), item["created_at"], upper
            )
            and not supported_terminal_evidence(attempt)
            for attempt in attempts
        )
    )
    applied = any(item["created_at"] <= row["at"] <= end for row in applications)
    observations = [
        row
        for attempt in attempts
        if production(attempt)
        for row in attempt["observations"]
        if item["created_at"] <= row["at"] <= end and row["action_version"]
    ]
    human_skip = False
    for observation in observations:
        if observation["outcome"] != "existing_area_skip":
            continue
        mutations = [
            event
            for event in events
            if event["label"] == observation["label"] and event["at"] <= observation["at"]
        ]
        if (
            mutations
            and mutations[-1]["event"] == "labeled"
            and mutations[-1]["actor_type"] == "User"
        ):
            human_skip = True
    outcomes = {row["outcome"] for row in observations}
    status = (
        "confirmed"
        if applied
        else "preexisting_human_skip"
        if human_skip
        else "technical_failure"
        if "application_failure" in outcomes
        else "fallback"
        if outcomes & {"fallback_applied", "fallback_present"}
        else "no_selection"
        if "no_selection" in outcomes
        else "no_confirmed_application"
    )
    return {"status": status, "evidence_complete": complete}


def daily_activity(events, attempts, confirmed):
    days = defaultdict(set)
    for event in events:
        day = event["at"][:10]
        if event["key"] in confirmed and area_label(event["label"], event["repo"]):
            days[day].add("applied")
        if event["actor_type"] == "User":
            days[day].add("human_changed")
    for attempt in attempts:
        if not production(attempt):
            continue
        for observation in attempt["observations"]:
            if no_selection(observation):
                days[observation["at"][:10]].add("no_selection")
    return [{"date": day, "series": sorted(series)} for day, series in sorted(days.items())]


def daily_coverage(state, stream, items, runs, now):
    if not stream["enabled"] or not state.get("cohort_start"):
        return []
    start = instant(state["cohort_start"])
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    progress = state["streams"].get(stream_key(stream["repo"], stream["kind"]), {})
    intake = state["repositories"].get(stream["repo"], {})
    timelines = [(item["created_at"], timeline_coverage_through(item, intake)) for item in items]
    daily_runs = [daily_execution(run) for run in runs]
    run_gaps = ExecutionIntervals(run for run in daily_runs if daily_run_unresolved(run))
    unresolved_attempts = []
    for attempt in state["attempts"].values():
        run = state["runs"].get(attempt["run"], {})
        if not production(attempt) or (run.get("repo"), run.get("kind")) != (
            stream["repo"],
            stream["kind"],
        ):
            continue
        if not supported_terminal_evidence(attempt):
            unresolved_attempts.append((attempt, daily_execution(run)))
    coverage = []
    while day <= now:
        end = day + timedelta(days=1)
        lower, upper = stamp(day), stamp(end)
        run_window_complete = (start <= day and progress.get("backfill_to", "") >= upper) or (
            progress.get("live_from", "~") <= lower and progress.get("live_to", "") >= upper
        )
        full_day = start <= day and end <= now
        history_complete = bool(
            full_day
            and intake.get("intake_complete")
            and (intake.get("updated_checkpoint") or "") >= upper
            and all((through or "") >= upper for created, through in timelines if created < upper)
        )
        complete = bool(
            history_complete
            and run_window_complete
            and not any(
                attempt_overlaps(attempt, run, lower, upper) for attempt, run in unresolved_attempts
            )
            and not run_gaps.overlaps(lower, upper)
        )
        history_reason = (
            "partial_day"
            if not full_day
            else "history_incomplete"
            if not history_complete
            else None
        )
        prediction_reason = (
            history_reason
            or ("history_incomplete" if not run_window_complete else None)
            or ("results_missing" if not complete else None)
        )
        coverage.append(
            {
                "date": lower[:10],
                "complete": complete,
                "series_complete": {
                    "applied": complete,
                    "no_selection": complete,
                    "human_changed": history_complete,
                },
                "partial_reason": {
                    "applied": prediction_reason,
                    "no_selection": prediction_reason,
                    "human_changed": history_reason,
                },
            }
        )
        day = end
    return coverage


def report(state, config, now):
    events, attempts, confirmed = reconstruct(state)
    result = {
        "schema": 2,
        "built_at": stamp(now),
        "data_as_of": state.get("data_as_of"),
        "last_refresh_attempt": state.get("last_refresh_attempt"),
        "cohort_start": state.get("cohort_start"),
        "budget_paused": state.get("budget_paused", False),
        "requests_used": state.get("requests_used"),
        "rate_remaining": state.get("rate_remaining"),
        "items": [],
        "changes": [],
        "streams": [],
        "configurations": [],
        "permissions": state["permissions"],
        "errors": sorted(state["errors"].values(), key=lambda row: row["last_seen"], reverse=True)[
            :30
        ],
    }
    runs_by_stream = defaultdict(list)
    terminal_runs, unresolved_attempt_runs = set(), set()
    stale_attempt_runs = {
        attempt["run"]
        for attempt in state["attempts"].values()
        if attempt.get("parser_version", PARSER_VERSION) != PARSER_VERSION
    }
    for attempt in state["attempts"].values():
        if production(attempt):
            target = (
                terminal_runs if supported_terminal_evidence(attempt) else unresolved_attempt_runs
            )
            target.add(attempt["run"])
    for key, run in state["runs"].items():
        runs_by_stream[stream_key(run["repo"], run["kind"])].append(
            {
                **run,
                "key": key,
                "has_terminal_item_evidence": key in terminal_runs,
            }
        )
    unresolved_by_stream = {
        stream_key(stream["repo"], stream["kind"]): [
            run
            for run in runs_by_stream[stream_key(stream["repo"], stream["kind"])]
            if intake_run_unresolved(run)
        ]
        for stream in config["streams"]
    }
    intake_gaps = {key: ExecutionIntervals(runs) for key, runs in unresolved_by_stream.items()}
    streams = {stream_key(s["repo"], s["kind"]): s for s in config["streams"]}
    for key, item in state["items"].items():
        item_events = events[key]
        item_attempts = attempts[key]
        production_attempts = [row for row in item_attempts if production(row)]
        key_stream = stream_key(item["repo"], item["kind"])
        base_branches = streams[key_stream].get("base_branches", [])
        if (
            base_branches
            and item.get("base_branch")
            and not any(fnmatchcase(item["base_branch"], branch) for branch in base_branches)
            and not production_attempts
        ):
            continue
        if only_existing_area_skips(production_attempts):
            continue
        observed = [
            observation
            for attempt in production_attempts
            for observation in attempt["observations"]
            if observation["outcome"] != "queued"
        ]
        observed.sort(key=lambda row: row["at"])
        applications = [
            {**event, "run": confirmed[event["key"]]["run"]}
            for event in item_events
            if event["key"] in confirmed and area_label(event["label"], item["repo"])
        ]
        primary = applications[0] if applications else None
        progress = state["streams"].get(key_stream, {})
        through = timeline_coverage_through(item, state["repositories"].get(item["repo"], {}))
        waiting_since = None
        for event in item_events:
            if event["label"] == "needs-area-label":
                waiting_since = event["at"] if event["event"] == "labeled" else None
        row = {
            "key": key,
            "repo": item["repo"],
            "kind": item["kind"],
            "number": item["number"],
            "url": item["url"],
            "state": item["state"],
            "created_at": item["created_at"],
            "labels": item["labels"],
            "observed_through": item.get("observed_through"),
            "timeline_coverage_through": through,
            "attempt_count": len(item_attempts),
            "production_attempt_count": len(production_attempts),
            "confirmed": bool(primary),
            "first_application": (
                {
                    "label": primary["label"],
                    "at": primary["at"],
                    "run_url": state["runs"][primary["run"]]["url"],
                }
                if primary
                else None
            ),
            "probable": any(observation["outcome"] == "area_applied" for observation in observed)
            and not primary,
            "fallback": any(
                observation["outcome"] in {"fallback_applied", "fallback_present"}
                and observation["action_version"]
                for observation in observed
            ),
            "no_selection": any(no_selection(observation) for observation in observed),
            "skip": any(
                observation["outcome"] == "existing_area_skip" and observation["action_version"]
                for observation in observed
            ),
            "technical_failure": any(
                observation["outcome"] == "application_failure" for observation in observed
            ),
            "unknown": bool(item.get("timeline_availability"))
            or bool(base_branches and not item.get("base_branch"))
            or not production_attempts
            or any(not supported_terminal_evidence(attempt) for attempt in production_attempts),
            "needs_area": not any(area_label(label, item["repo"]) for label in item["labels"]),
            "fallback_marker": "needs-area-label" in item["labels"],
            "timeline_unavailable": bool(item.get("timeline_availability")),
            "activity": daily_activity(item_events, item_attempts, confirmed),
            "waiting_since": waiting_since,
            "original_outcome": observed[0]["outcome"] if observed else "no_observed_result",
            "removal_7": removal_followup(primary, item_events, now, 7, through),
            "removal_30": removal_followup(primary, item_events, now, 30, through),
        }
        for hours in (1, 24):
            row[f"intake_{hours}"] = intake_view(
                item,
                item_events,
                item_attempts,
                applications,
                progress,
                intake_gaps[key_stream],
                state["runs"],
                now,
                hours,
                through,
            )
            if base_branches and not item.get("base_branch") and row[f"intake_{hours}"]:
                row[f"intake_{hours}"]["evidence_complete"] = False
        result["items"].append(row)
        result["changes"].extend(label_changes(key, item_events))
    for stream in config["streams"]:
        key = stream_key(stream["repo"], stream["kind"])
        progress = state["streams"].get(key, {})
        items = [
            row
            for row in result["items"]
            if row["repo"] == stream["repo"] and row["kind"] == stream["kind"]
        ]
        repo_progress = state["repositories"].get(stream["repo"], {})
        stream_runs = runs_by_stream[key]
        source_items = [state["items"][item["key"]] for item in items]
        pending_timelines = sum(pending_timeline(item) for item in source_items)
        collection_pending_runs = sum(
            run.get("parser_version", PARSER_VERSION) != PARSER_VERSION
            or run["key"] in stale_attempt_runs
            or run["evidence"] == "parser_upgrade_pending"
            or (
                not run["collected"]
                and not (
                    run["evidence"] == "pending"
                    and run.get("status")
                    in {"queued", "in_progress", "waiting", "pending", "requested"}
                )
            )
            for run in stream_runs
        )
        unresolved = {run["key"] for run in unresolved_by_stream[key]} | {
            run["key"] for run in stream_runs if run["key"] in unresolved_attempt_runs
        }
        production_runs = [run for run in stream_runs if automatic_run(run)]
        pending_results = {run["key"] for run in production_runs if run["evidence"] == "pending"}
        failed_results = {
            run["key"]
            for run in production_runs
            if run["evidence"] == "technical_failure"
            or run.get("conclusion")
            in {
                "failure",
                "timed_out",
                "cancelled",
                "action_required",
                "startup_failure",
                "stale",
            }
        }
        latest_action = max(
            (
                (run.get("created_at") or "", config)
                for run in production_runs
                for config in run.get("configs", [])
                if config["action_sha"]
            ),
            key=lambda pair: pair[0],
            default=(None, {}),
        )[1]
        result["streams"].append(
            {
                **stream,
                **progress,
                "labeler_version": latest_action.get("action_version"),
                "labeler_sha": latest_action.get("action_sha"),
                "intake_complete": repo_progress.get("intake_complete", False),
                "intake_as_of": repo_progress.get("updated_checkpoint"),
                "feed_gap": repo_progress.get("feed_gap", False),
                "items": len(items),
                "timelines": sum(bool(item["observed_through"]) for item in items),
                "pending_timelines": pending_timelines,
                "production_incomplete_runs": len(unresolved),
                "collection_pending_runs": collection_pending_runs,
                "collection_caught_up": bool(
                    stream["enabled"]
                    and repo_progress.get("intake_complete")
                    and not repo_progress.get("last_error")
                    and progress.get("live_to")
                    and (repo_progress.get("updated_checkpoint") or "") >= progress["live_to"]
                    and progress.get("backfill_to", "") >= progress["live_to"]
                    and not progress.get("last_error")
                    and not pending_timelines
                    and not collection_pending_runs
                ),
                "prediction_results": {
                    "missing": len(unresolved - pending_results - failed_results),
                    "pending": len(pending_results),
                    "failed_or_blocked": len(failed_results),
                },
                "daily_coverage": daily_coverage(state, stream, source_items, stream_runs, now),
                "run_attempts": len(stream_runs),
                "run_evidence": dict(Counter(run["evidence"] for run in stream_runs)),
                "failure_phases": dict(
                    Counter(phase for run in stream_runs for phase in run.get("failure_phases", []))
                ),
                "oldest_retry_check": min(
                    (run["last_checked_at"] for run in stream_runs), default=None
                ),
            }
        )
    configurations = Counter()
    for run in state["runs"].values():
        for observed_config in run.get("configs", []):
            configurations[
                (
                    run["repo"],
                    run["kind"],
                    run["branch"],
                    observed_config["threshold"],
                    observed_config["action_sha"],
                    observed_config["action_version"],
                    observed_config["cache_hit"],
                )
            ] += 1
    for values, count in configurations.items():
        repo, kind, branch, threshold, sha, version, cache = values
        result["configurations"].append(
            {
                "repo": repo,
                "kind": kind,
                "branch": branch,
                "threshold": threshold,
                "action_sha": sha,
                "action_version": version,
                "cache_hit": cache,
                "model_version": None,
                "run_attempts": count,
            }
        )
    return result
