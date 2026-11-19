from collections import Counter, defaultdict
from datetime import timedelta

from .store import instant, stamp, stream_key


def production(attempt):
    return not attempt["manual"] and not attempt["dry_run"]


def only_existing_area_skips(attempts):
    if not attempts:
        return False
    for attempt in attempts:
        results = [row for row in attempt["observations"] if row["outcome"] != "queued"]
        if not results or any(
            row["outcome"] != "existing_area_skip"
            or not row["action_version"]
            or not (row["label"] or "").startswith("area-")
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
    for item_key, rows in attempts.items():
        through = state["items"][item_key].get("observed_through")
        for attempt in rows:
            if not production(attempt):
                continue
            for index, row in enumerate(attempt["observations"]):
                if row["outcome"] not in {"area_applied", "fallback_applied"}:
                    continue
                identity = (attempt["key"], index)
                observations[identity] = {**row, "run": attempt["run"], "item": item_key}
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
        if len(matching) == 1 and len(claims[matching[0]]) == 1:
            confirmed[matching[0]] = observations[identity]
    return events, attempts, confirmed


def removal_followup(item, application, events, now, days):
    if not application:
        return None
    deadline = instant(application["at"]) + timedelta(days=days)
    through = item.get("observed_through")
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


def intake_view(item, events, attempts, applications, progress, stream_runs, now, hours):
    deadline = instant(item["created_at"]) + timedelta(hours=hours)
    if now < deadline:
        return None
    end = stamp(deadline)
    covered = progress.get("backfill_to", "") >= end or (
        progress.get("live_from", "~") <= item["created_at"] and progress.get("live_to", "") >= end
    )
    complete = bool(
        covered
        and item.get("observed_through", "") >= end
        and not any(
            item["created_at"] <= run["created_at"] <= end
            and (not run["collected"] or run["evidence"] in {"unknown", "unavailable"})
            for run in stream_runs
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
        else "no_confirmed_application"
    )
    return {"status": status, "evidence_complete": complete}


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
    for run in state["runs"].values():
        runs_by_stream[stream_key(run["repo"], run["kind"])].append(run)
    for key, item in state["items"].items():
        item_events = events[key]
        item_attempts = attempts[key]
        production_attempts = [row for row in item_attempts if production(row)]
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
            if event["key"] in confirmed and event["label"].startswith("area-")
        ]
        primary = applications[0] if applications else None
        key_stream = stream_key(item["repo"], item["kind"])
        progress = state["streams"].get(key_stream, {})
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
            "skip": any(
                observation["outcome"] == "existing_area_skip" and observation["action_version"]
                for observation in observed
            ),
            "technical_failure": any(
                observation["outcome"] == "application_failure" for observation in observed
            ),
            "unknown": bool(item.get("timeline_availability"))
            or not observed
            or any(
                not observation["action_version"] or observation["outcome"] == "unsupported"
                for observation in observed
            ),
            "needs_area": not any(label.startswith("area-") for label in item["labels"]),
            "fallback_marker": "needs-area-label" in item["labels"],
            "timeline_unavailable": bool(item.get("timeline_availability")),
            "waiting_since": waiting_since,
            "original_outcome": observed[0]["outcome"] if observed else "no_observed_result",
            "removal_7": removal_followup(item, primary, item_events, now, 7),
            "removal_30": removal_followup(item, primary, item_events, now, 30),
        }
        for hours in (1, 24):
            row[f"intake_{hours}"] = intake_view(
                item,
                item_events,
                item_attempts,
                applications,
                progress,
                runs_by_stream[key_stream],
                now,
                hours,
            )
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
        result["streams"].append(
            {
                **stream,
                **progress,
                "intake_complete": repo_progress.get("intake_complete", False),
                "intake_as_of": repo_progress.get("updated_checkpoint"),
                "feed_gap": repo_progress.get("feed_gap", False),
                "items": len(items),
                "timelines": sum(bool(item["observed_through"]) for item in items),
                "pending_timelines": sum(
                    not item.get("observed_through")
                    or item.get("timeline_source_updated") != item.get("updated_at")
                    or bool(item.get("timeline_error"))
                    or item.get("reconcile_requested", False)
                    for item in source_items
                ),
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
