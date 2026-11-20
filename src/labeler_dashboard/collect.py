from collections import defaultdict
from datetime import timedelta

from .evidence import PARSER_VERSION, PRODUCTION_EVENTS, outcome, parse_log, prediction_step
from .github import APIError, BudgetExceeded
from .store import (
    instant,
    pending_timeline,
    record_error,
    relevant_label,
    safe_github_url,
    stamp,
    stream_key,
)


class Collector:
    def __init__(self, client, state: dict, config: dict, now):
        self.api = client
        self.state = state
        self.config = config
        self.now = now
        self.at = stamp(now)
        self.enabled = [stream for stream in config["streams"] if stream["enabled"]]
        self.repos = list(dict.fromkeys(stream["repo"] for stream in self.enabled))
        self.failed = False

    def error(self, stage: str, error: Exception):
        if isinstance(error, APIError):
            self.failed = True
        record_error(self.state, stage, str(error), self.at)
        print(f"{stage}: {error}", flush=True)

    def initialize(self):
        self.state.setdefault(
            "cohort_start", stamp(self.now - timedelta(days=self.config["backfill_days"]))
        )
        for repo in self.repos:
            self.state["repositories"].setdefault(
                repo, {"intake_complete": False, "updated_checkpoint": None, "feed_head": None}
            )
        for stream in self.enabled:
            key = stream_key(stream["repo"], stream["kind"])
            self.state["streams"].setdefault(
                key,
                {
                    "backfill_to": self.state["cohort_start"],
                    "live_from": stamp(self.now - timedelta(hours=6)),
                    "live_to": stamp(self.now - timedelta(hours=6)),
                    "last_success": None,
                    "last_error": None,
                },
            )
        for run in self.state["runs"].values():
            if run.get("parser_version") != PARSER_VERSION and run["evidence"] != "unavailable":
                run["evidence"] = "parser_upgrade_pending"

    def preflight(self, repo):
        checks = {}
        self.state["permissions"][repo] = {"checked_at": self.at, "checks": checks}
        operations = (
            (
                "issues",
                lambda: self.api.get(f"/repos/{repo}/issues", {"state": "all", "per_page": 1}),
            ),
            (
                "timeline",
                lambda: self.api.get(
                    f"/repos/{repo}/issues/{latest_item[0]['number']}/timeline", {"per_page": 1}
                ),
            ),
            (
                "runs",
                lambda: self.api.get(
                    f"/repos/{repo}/actions/workflows/{workflow}/runs",
                    {"status": "completed", "per_page": 5},
                ),
            ),
        )
        latest_item = []
        runs = []
        workflow = next(s["workflow_id"] for s in self.enabled if s["repo"] == repo)
        for name, operation in operations:
            if name == "timeline" and not latest_item:
                checks[name] = {
                    "ok": False,
                    "state": "unverified",
                    "reason": "No item sample to verify access",
                }
                continue
            try:
                data = operation()
                checks[name] = {"ok": True}
                if name == "issues":
                    latest_item = data
                elif name == "runs":
                    runs = data["workflow_runs"]
            except APIError as error:
                checks[name] = {
                    "ok": False,
                    "state": "denied" if error.status in {401, 403} else "failed",
                    "status": error.status,
                    "reason": str(error),
                }
                self.error(f"{repo}/preflight/{name}", error)
        checks["jobs"] = {
            "ok": False,
            "state": "unverified",
            "reason": "No eligible job sample to verify access",
        }
        checks["logs"] = {
            "ok": False,
            "state": "unverified",
            "reason": "No readable log sample found",
        }
        eligible = [
            run
            for run in runs
            if run.get("conclusion") not in {"skipped", "cancelled"}
            and instant(run["updated_at"]) <= self.now - timedelta(minutes=5)
        ]
        for run in eligible[:3]:
            endpoint = "jobs"
            try:
                jobs = self.api.get(
                    f"/repos/{repo}/actions/runs/{run['id']}/jobs", {"per_page": 100}
                )["jobs"]
                checks["jobs"] = {"ok": True}
                executed = [
                    job
                    for job in jobs
                    if job.get("status") == "completed"
                    and job.get("conclusion") not in {"skipped", "cancelled"}
                    and any(
                        step.get("conclusion") not in {None, "skipped"}
                        for step in job.get("steps", [])
                    )
                ]
                endpoint = "logs"
                for job in executed[:2]:
                    try:
                        self.api.log(repo, job["id"])
                    except APIError as error:
                        if error.status in {404, 410}:
                            continue
                        raise
                    checks["logs"] = {"ok": True}
                    return checks
            except APIError as error:
                if error.status in {404, 410}:
                    continue
                checks[endpoint] = {
                    "ok": False,
                    "state": "denied" if error.status in {401, 403} else "failed",
                    "status": error.status,
                    "reason": str(error),
                }
                self.error(f"{repo}/preflight/{endpoint}", error)
                break
        return checks

    def can_collect_runs(self, repo):
        checks = self.state["permissions"].get(repo, {}).get("checks", {})
        return checks.get("runs", {}).get("ok") and all(
            checks.get(name, {}).get("ok") or checks.get(name, {}).get("state") == "unverified"
            for name in ("jobs", "logs")
        )

    def retain_item(self, repo, source):
        kind = "pr" if "pull_request" in source else "issue"
        if not any(s["repo"] == repo and s["kind"] == kind for s in self.enabled):
            return
        key = f"{repo}:{source['id']}"
        if source["created_at"] < self.state["cohort_start"] and key not in self.state["items"]:
            return
        item = self.state["items"].get(key, {})
        state = source["state"]
        if kind == "pr" and state == "closed":
            pr = source["pull_request"]
            if "merged_at" not in pr:
                pr = self.api.get(f"/repos/{repo}/pulls/{source['number']}")
            state = "merged" if pr.get("merged_at") else "closed"
        item.update(
            id=source["id"],
            repo=repo,
            kind=kind,
            number=source["number"],
            url=safe_github_url(source.get("html_url"), repo, "pull" if kind == "pr" else "issues"),
            state=state,
            created_at=source["created_at"],
            updated_at=source["updated_at"],
            labels=sorted(
                label["name"] for label in source["labels"] if relevant_label(label["name"])
            ),
            observed_at=self.at,
        )
        self.state["items"][key] = item

    def intake(self, repo):
        progress = self.state["repositories"][repo]
        initial = not progress["intake_complete"]
        since = (
            self.state["cohort_start"]
            if initial
            else stamp(instant(progress["updated_checkpoint"]) - timedelta(minutes=10))
        )
        params = {
            "state": "all",
            "since": since,
            "sort": "created" if initial else "updated",
            "direction": "desc",
        }
        for page in self.api.pages(f"/repos/{repo}/issues", params):
            for source in page:
                self.retain_item(repo, source)
            if initial and page and page[-1]["created_at"] < self.state["cohort_start"]:
                break
        progress.update(
            intake_complete=True, updated_checkpoint=self.at, last_success=self.at, last_error=None
        )

    def event_feed(self, repo):
        progress = self.state["repositories"][repo]
        previous = progress.get("feed_head")
        found = previous is None
        head = None
        for page in range(1, 4):
            rows = self.api.get(f"/repos/{repo}/issues/events", {"per_page": 100, "page": page})
            if head is None and rows:
                head = rows[0]["id"]
            for event in rows:
                if event["id"] == previous:
                    found = True
                    break
                item_id = event.get("issue", {}).get("id")
                item = self.state["items"].get(f"{repo}:{item_id}")
                if item and event.get("event") in {"labeled", "unlabeled"}:
                    item["reconcile_requested"] = True
            if found and previous is not None:
                break
            if len(rows) < 100:
                break
        progress["feed_checked_at"] = self.at
        progress["feed_gap"] = not found
        if not found:
            self.error(
                f"{repo}/event-feed",
                RuntimeError(
                    "Checkpoint not in bounded event feed. Full timelines remain authoritative."
                ),
            )
        # The feed is only a prioritization hint, never a completeness checkpoint.
        if head is not None:
            progress["feed_head"] = head

    def timeline(self, key, item):
        for rows in self.api.pages(f"/repos/{item['repo']}/issues/{item['number']}/timeline"):
            for source in rows:
                label = (source.get("label") or {}).get("name", "")
                if source.get("event") not in {"labeled", "unlabeled"} or not relevant_label(label):
                    continue
                if not source.get("id") or not source.get("created_at"):
                    raise APIError(
                        f"/repos/{item['repo']}/issues/{item['number']}/timeline",
                        200,
                        "Label event missing identity or timestamp",
                    )
                actor = source.get("actor") or {}
                event_key = f"{item['repo']}:{source['id']}"
                self.state["events"][event_key] = {
                    "id": source["id"],
                    "item": key,
                    "repo": item["repo"],
                    "at": source["created_at"],
                    "event": source["event"],
                    "label": label,
                    "actor": actor.get("login"),
                    "actor_type": actor.get("type"),
                }
        item.update(
            observed_through=self.at,
            timeline_source_updated=item["updated_at"],
            timeline_error=None,
            reconcile_requested=False,
        )
        item.pop("timeline_availability", None)

    def reconcile(self, key, item):
        checks = self.state["permissions"].get(item["repo"], {}).get("checks", {})
        if not all(checks.get(name, {}).get("ok") for name in ("issues", "timeline")):
            return
        availability = item.get("timeline_availability", {})
        changed = availability.get("source_updated") != item["updated_at"] or item.get(
            "reconcile_requested", False
        )
        if availability and not changed:
            retry_at = availability.get("retry_at")
            if retry_at is None or instant(retry_at) > self.now:
                return
        try:
            self.timeline(key, item)
        except APIError as error:
            item["timeline_error"] = str(error)
            if error.status not in {301, 404, 410}:
                raise
            failures = (availability.get("failures", 0) if not changed else 0) + 1
            delay = {1: timedelta(hours=1), 2: timedelta(days=1)}.get(failures)
            item["timeline_availability"] = {
                "status": "unavailable" if failures >= 3 else "retrying",
                "reason": "redirect_not_followed"
                if error.status == 301
                else "missing_or_inaccessible",
                "failures": failures,
                "source_updated": item["updated_at"],
                "checked_at": self.at,
                "retry_at": stamp(self.now + delay) if delay else None,
            }
            item["reconcile_requested"] = False
            # Healthy source preflight makes this a per-item evidence gap, not deletion proof.
            self.error(f"{item['repo']}/timeline/{item['number']}", RuntimeError(str(error)))

    def collect_run(self, stream, source):
        repo = stream["repo"]
        run_id = source["id"]
        run_number = source.get("run_attempt", 1)
        complete = True
        for attempt in range(1, run_number + 1):
            run_key = f"{repo}:{run_id}:{attempt}"
            existing = self.state["runs"].get(run_key)
            if (
                existing
                and existing.get("collected")
                and existing.get("parser_version") == PARSER_VERSION
            ):
                existing["last_checked_at"] = self.at
                continue
            raw = (
                source
                if attempt == run_number
                else self.api.get(f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}")
            )
            run = {
                "repo": repo,
                "kind": stream["kind"],
                "id": run_id,
                "attempt": attempt,
                "created_at": raw["created_at"],
                "updated_at": raw["updated_at"],
                "started_at": raw.get("run_started_at"),
                "completed_at": None,
                "status": raw["status"],
                "branch": raw.get("head_branch"),
                "event": raw["event"],
                "conclusion": raw.get("conclusion"),
                "url": safe_github_url(raw.get("html_url"), repo, "actions/runs"),
                "manual": raw["event"] not in PRODUCTION_EVENTS,
                "collected": False,
                "parser_version": PARSER_VERSION,
                "last_checked_at": self.at,
                "evidence": "pending",
                "gaps": [],
            }
            self.state["runs"][run_key] = run
            if raw["status"] != "completed":
                complete = False
                continue
            jobs = []
            for page in self.api.pages(
                f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs", field="jobs"
            ):
                jobs.extend(page)
            starts = [job["started_at"] for job in jobs if job.get("started_at")]
            ends = [job["completed_at"] for job in jobs if job.get("completed_at")]
            if starts and not run["started_at"]:
                run["started_at"] = min(starts)
            if jobs and len(ends) == len(jobs):
                run["completed_at"] = max(ends)
            by_item = defaultdict(list)
            configs = []
            failure_phases = set()
            for job in jobs:
                for step in job.get("steps", []):
                    if step["conclusion"] == "failure":
                        failure_phases.add(
                            "cache_restore"
                            if step["name"]
                            in {"Restore issues model from cache", "Restore pulls model from cache"}
                            else "prediction"
                            if prediction_step(job) == step
                            else "workflow"
                        )
            run["failure_phases"] = sorted(failure_phases)
            explicit_failure = bool(failure_phases)
            no_work = False
            for job in jobs:
                step = prediction_step(job)
                if step is None:
                    continue
                if step["conclusion"] == "skipped":
                    no_work = no_work or (
                        raw["event"] == "schedule"
                        and any(
                            s["name"] == "Restore pulls model from cache"
                            and s["conclusion"] == "skipped"
                            for s in job.get("steps", [])
                        )
                        and any(
                            s["name"] == "Determine pull requests to process"
                            and s["conclusion"] == "success"
                            for s in job.get("steps", [])
                        )
                    )
                    continue
                try:
                    parsed = parse_log(self.api.log(repo, job["id"]), repo)
                except APIError as error:
                    if error.status in {404, 410}:
                        if self.now - instant(raw["updated_at"]) < timedelta(days=1):
                            complete = False
                            run["gaps"].append("logs_not_yet_available")
                        else:
                            run["gaps"].append("logs_expired_or_deleted")
                    else:
                        run["gaps"].append("logs_unavailable")
                        complete = False
                        self.error(f"{repo}/run/{run_id}/{attempt}", error)
                    continue
                config = {
                    k: parsed[k]
                    for k in (
                        "action_sha",
                        "action_version",
                        "model_version",
                        "threshold",
                        "cache_hit",
                        "dry_run",
                    )
                }
                configs.append(config)
                explicit_failure = explicit_failure or parsed["explicit_failure"]
                no_work = no_work or parsed["explicit_no_work"]
                for observation in parsed["observations"]:
                    if observation["kind"] != stream["kind"]:
                        continue
                    observation.update(
                        step_start=step.get("started_at"),
                        step_end=step.get("completed_at"),
                        **config,
                    )
                    by_item[observation["number"]].append(observation)
            run["configs"] = configs
            run["evidence"] = (
                "unavailable"
                if run["gaps"]
                else "technical_failure"
                if explicit_failure or raw.get("conclusion") == "failure"
                else "item_results"
                if by_item
                else "no_work"
                if no_work
                else "unknown"
            )
            run["collected"] = not any(gap != "logs_expired_or_deleted" for gap in run["gaps"])
            for number, observations in by_item.items():
                attempt_key = f"{run_key}:{number}"
                self.state["attempts"][attempt_key] = {
                    "repo": repo,
                    "kind": stream["kind"],
                    "number": number,
                    "run": run_key,
                    "manual": run["manual"],
                    "dry_run": any(
                        row["dry_run"] is True or row["outcome"] == "dry_run"
                        for row in observations
                    ),
                    "outcome": outcome(observations),
                    "observations": observations,
                    "parser_version": PARSER_VERSION,
                }
            complete = complete and run["collected"]
        return complete

    def audit_runs(self):
        # A failed lookup rotates independently of the last successful evidence fetch.
        roots = {}
        urgency = {}
        for run in self.state["runs"].values():
            identity = (run["repo"], run["id"])
            priority = (
                0
                if not run.get("collected")
                else 1
                if run.get("parser_version") != PARSER_VERSION
                else 2
            )
            urgency[identity] = min(urgency.get(identity, 2), priority)
            if identity not in roots or run["attempt"] > roots[identity]["attempt"]:
                roots[identity] = run
        due = []
        for run in roots.values():
            audit = (
                self.state["repositories"][run["repo"]]
                .setdefault("run_audits", {})
                .get(str(run["id"]), {})
            )
            if audit.get("retry_at") and instant(audit["retry_at"]) > self.now:
                continue
            last_attempt = audit.get("last_attempted_at", run["last_checked_at"])
            priority = urgency[run["repo"], run["id"]]
            if priority == 2 and (self.now - instant(last_attempt) < timedelta(hours=6)):
                continue
            if self.can_collect_runs(run["repo"]):
                due.append((run, audit, last_attempt, priority))
        due.sort(key=lambda row: (row[3], row[2]))
        for run, previous, _, _ in due[:100]:
            audits = self.state["repositories"][run["repo"]]["run_audits"]
            audit = {**previous}
            stream = next(
                s for s in self.enabled if s["repo"] == run["repo"] and s["kind"] == run["kind"]
            )
            try:
                source = self.api.get(f"/repos/{run['repo']}/actions/runs/{run['id']}")
                audit["last_attempted_at"] = self.at
                audits[str(run["id"])] = audit
                if self.collect_run(stream, source):
                    audit.update(last_success_at=self.at, failures=0, retry_at=None)
                    audit.pop("last_error", None)
            except APIError as error:
                failures = previous.get("failures", 0) + 1
                audit.update(
                    last_attempted_at=self.at,
                    failures=failures,
                    retry_at=stamp(
                        self.now + timedelta(hours=min(168, 6 * 2 ** min(failures - 1, 5)))
                    ),
                    last_error=str(error),
                )
                audits[str(run["id"])] = audit
                self.error(f"{run['repo']}/rerun-audit", error)
            except BudgetExceeded:
                self.state["budget_paused"] = True
                break

    def run_window(self, stream, field, end):
        progress = self.state["streams"][stream_key(stream["repo"], stream["kind"])]
        checkpoint = instant(progress[field])
        start = checkpoint
        if field == "live_to":
            start = max(instant(self.state["cohort_start"]), start - timedelta(minutes=10))
        if start >= end:
            return
        runs = self.api.runs_in_window(stream["repo"], stream["workflow_id"], start, end)
        completed = True
        for run in sorted(runs, key=lambda row: row["id"]):
            completed = self.collect_run(stream, run) and completed
        if completed:
            progress[field] = stamp(end)
            progress["last_error"] = None
            if field == "live_to":
                progress["last_success"] = self.at
        else:
            progress["last_error"] = "Pending runs or unavailable evidence. Window will be retried."

    def backfill_spare_budget(self, end):
        active = [
            stream
            for stream in self.enabled
            if self.can_collect_runs(stream["repo"])
            and not self.state["streams"][stream_key(stream["repo"], stream["kind"])]["last_error"]
            and instant(
                self.state["streams"][stream_key(stream["repo"], stream["kind"])]["backfill_to"]
            )
            < end
        ]
        while active and self.api.used < self.api.budget:
            share = max(1, (self.api.budget - self.api.used) // len(active))
            continuing = []
            for stream in active:
                progress = self.state["streams"][stream_key(stream["repo"], stream["kind"])]
                before = progress["backfill_to"]
                self.api.limit = min(self.api.budget, self.api.used + share)
                try:
                    self.run_window(
                        stream, "backfill_to", min(instant(before) + timedelta(hours=6), end)
                    )
                except APIError as error:
                    progress["last_error"] = str(error)
                    self.error(stream_key(stream["repo"], stream["kind"]), error)
                except BudgetExceeded as error:
                    self.state["budget_paused"] = True
                    if error.rate_limited or self.api.used < self.api.limit:
                        return
                else:
                    if progress["backfill_to"] != before and instant(progress["backfill_to"]) < end:
                        continuing.append(stream)
                if self.api.used >= self.api.budget:
                    if any(
                        instant(
                            self.state["streams"][stream_key(s["repo"], s["kind"])]["backfill_to"]
                        )
                        < end
                        for s in active
                    ):
                        self.state["budget_paused"] = True
                    return
            active = continuing

    def refresh(self):
        self.initialize()
        self.state["last_refresh_attempt"] = self.at
        self.state["budget_paused"] = False
        for repo in self.repos:
            try:
                self.preflight(repo)
                self.intake(repo)
                self.event_feed(repo)
            except APIError as error:
                self.state["repositories"][repo]["last_error"] = str(error)
                self.error(f"{repo}/intake", error)
            except BudgetExceeded:
                self.state["budget_paused"] = True
                break

        # Reserve requests for run streams even during the first timeline backfill.
        self.api.limit = min(self.api.budget, self.api.used + self.api.budget * 35 // 100)
        items = sorted(
            self.state["items"].items(),
            key=lambda row: (
                not pending_timeline(row[1]),
                row[1].get("observed_through") or "",
                row[0],
            ),
        )
        for index, (key, item) in enumerate(items):
            if (
                item.get("observed_through") == self.at
                and item.get("timeline_source_updated") == item["updated_at"]
            ):
                continue
            used_before = self.api.used
            try:
                self.reconcile(key, item)
            except APIError as error:
                item["timeline_error"] = str(error)
                self.error(f"{item['repo']}/timeline/{item['number']}", error)
            except BudgetExceeded as error:
                reservation_only = (
                    not error.rate_limited
                    and self.api.limit < self.api.budget
                    and self.api.used >= self.api.limit
                    and self.api.used < self.api.budget
                )
                if self.api.used > used_before:
                    item["reconcile_requested"] = True
                if not reservation_only or any(pending_timeline(row) for _, row in items[index:]):
                    self.state["budget_paused"] = True
                break

        self.api.limit = self.api.budget
        available = max(0, self.api.budget - self.api.used - 15)
        share = available // max(1, len(self.enabled))
        end = self.now - timedelta(minutes=5)
        for stream in self.enabled:
            key = stream_key(stream["repo"], stream["kind"])
            progress = self.state["streams"][key]
            if not self.can_collect_runs(stream["repo"]):
                progress["last_error"] = (
                    "Source access failed. See operations for endpoint details."
                )
                continue
            self.api.limit = min(self.api.budget - 15, self.api.used + share)
            try:
                self.run_window(stream, "live_to", end)
                while instant(progress["backfill_to"]) < end:
                    before = progress["backfill_to"]
                    window_end = min(instant(before) + timedelta(hours=6), end)
                    self.run_window(stream, "backfill_to", window_end)
                    if before == progress["backfill_to"]:
                        break
            except APIError as error:
                progress["last_error"] = str(error)
                self.error(key, error)
            except BudgetExceeded:
                self.state["budget_paused"] = True

        # Reruns keep created_at. Rotate a separate audit across known run identities.
        self.api.limit = self.api.budget
        self.audit_runs()
        # Audits get their turn before unfinished streams reuse any unspent shares.
        self.backfill_spare_budget(end)
        self.api.limit = self.api.budget
        checkpoints = [
            self.state["repositories"][repo].get("updated_checkpoint") for repo in self.repos
        ] + [
            progress.get("last_success") and progress["live_to"]
            for progress in self.state["streams"].values()
        ]
        if checkpoints and all(checkpoints):
            self.state["data_as_of"] = min(checkpoints)
        self.state["requests_used"] = self.api.used
        self.state["rate_remaining"] = self.api.remaining
