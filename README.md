# .NET labeler dashboard

Read-only production monitoring for the .NET issue labeler.

**Dashboard:** https://matouskozak.github.io/dotnet-labeler-dashboard/

## What it measures

Enabled streams are runtime issues, runtime pull requests, and ML.NET issues.
ML.NET pull requests are **not enabled**, not failures. Intake includes open,
closed, and merged items. Filters select repository, item type, and creation cohort.

The dashboard separates confirmed area applications, no-selection fallbacks,
application failures, and unknown evidence. It shows dashboard cohort items
currently lacking an area label (with or without a fallback marker or observed
run), human area changes, fallback resolutions, and bot
reapplications. It does **not** claim prediction accuracy, precision, or exact
scoring coverage.

Items whose automatic attempts only report supported existing-area skips are
excluded from the published item dataset, tables, item counts, and intake
denominators. A later skip does not hide an item with another automatic prediction,
application, fallback, failure, or unknown result. No-run items, unsupported
evidence, and manual/dry-run-only skips remain visible. This is a derived report
filter, not deletion of collected history. Configuration and operational run
counts cover all collected workflow runs separately from the dashboard item cohort.

Confirmation requires both a supported, explicit per-item predictor result and a
unique matching `github-actions[bot]` label mutation inside the predictor step.
Matching uses repository and item number, never titles or the workflow run actor.
Overlapping attempts cannot both claim one event. The shared bot identity alone
proves nothing. Unmatched applied results remain unconfirmed.
Non-dry manual attempts also compete for a mutation, but never count as an
automatic application. A successful application followed by a cleanup failure
retains both observations.

Human removal follow-up uses the first confirmed area application per item.
The 7/30-day denominator requires both elapsed time and a fully fetched timeline
through that deadline. Insufficient mature evidence is not zero. A human addition
without removal is not a correction. Human actors are not assumed to be maintainers.

The **Label changes** table shows factual before/after label sets, who changed
them, and when. It replays the ordered timeline independently of prediction logs,
so missing run evidence does not hide a known earlier label. It includes human
edits and bot reapplications. Empty label sets appear as **No area label**, and
the fallback remains explicit as `needs-area-label`.

For presentation only, two consecutive relevant edits by the same identified
human within five seconds are shown together when one adds a new label and the
other removes a previously present label (in either order). Different actors,
longer gaps, and additions without removals stay separate. All labels still
present are shown, so adding another area label is not called a replacement.
The displayed time is the last edit in the pair. Raw timeline events are retained
unchanged. This grouping never supplies model confirmation or accuracy evidence.

The daily plot counts items per UTC event day: confirmed automatic area
applications, supported automatic no-selection/fallback results, and human label
edits (`actor_type=User`). Each item counts once per day per line. Retries and
add/remove pairs do not inflate counts. The lines may overlap. Repository, type,
and creation-period filters select the items, and the plot shows activity within
that period. The 7/30-day selections use UTC calendar days, including today.
Dashed lines and hollow points are partial observed counts. A zero
is shown only when intake, run history, and timeline follow-up cover that day.
Missing history stays unknown, not zero.

Fixed 1h/24h intake denominators retain dashboard cohort items old enough for the
window, including no-run, failed, fallback, and unknown opportunities. Skip-only
pre-labeled items are excluded throughout. Incomplete evidence is reported
separately. Manual and dry-run attempts do not enter production metrics. Waiting
fallback items remain visible until resolved. A fallback proves no area selection,
not necessarily an exact below-threshold cause.
Unsupported or queued-only automatic attempts leave intake evidence incomplete,
even when another attempt has a known result or the workflow succeeded. Supported
item application failures remain known failures, not unknown predictions.
Coverage ignores manual/dry-run-only gaps. Details still lists all workflow runs.

## Run locally

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12 (uv can install it).

```text
uv sync --locked --python 3.12
uv run playwright install chromium
uv run pytest
uv run ruff check .
uv run labeler-dashboard refresh
uv run labeler-dashboard render
uv run python -m http.server 8000 --directory _site
```

Set `GH_TOKEN` in the process environment before refreshing. Never put a token in
configuration, command-line arguments, commits, or generated artifacts. Local
history defaults to `.state/state.json`. Rendering reads only persisted history.

## Collection and history

The first refresh fixes a 30-day creation-cohort start. REST all-state issue
pagination includes pull requests and stops at that intake boundary. Subsequent
refreshes query updated items with an overlap, retaining closed items and late
changes indefinitely. The bounded repository issue-event feed only prioritizes
reconciliation. Missing feed checkpoints are reported as gaps, never interpreted
as an unlimited archive. Full per-item timelines are authoritative.
Incremental pagination sorts newest updates first, so an already-read item moving
to the front cannot displace an unread quiet item out of subsequent pages.

An unavailable or redirected timeline is not assumed to be deleted. Only after
healthy source-level preflight, it gets two bounded retries (after one hour and
one day), then an explicit unavailable marker. A newly observed item update or
label event permits reconciliation again. Items and prior evidence are retained
in intake denominators. Missing timeline follow-up never matures just by waiting.
Redirects are not followed to other repositories or arbitrary hosts.

Only the configured prediction workflows are queried. Run-created windows are
split below GitHub's 1,000-result cap, with full pagination and bounded request
budgets. Recent work is collected before the resumable six-hour backfill windows.
Interrupted windows retain evidence without advancing their checkpoints.
Unfinished runs remain pending. A rotating audit of known run identities detects
reruns with old creation timestamps. Such reruns can remain undiscovered until
their audit turn, shown in operations.
Failed run lookups retain their errors and back off independently of successful
checks, so inaccessible runs do not block later audits. Coverage gaps use actual
attempt/job execution times where available. Missing execution times leave the
possible interval incomplete rather than assigning a rerun to its creation day.
Known unfinished attempts are audited before stale-parser history and routine
checks, including when a later attempt is already complete.

Every scheduled or manual refresh allows up to 4,000 requests and audits up to
100 old-run root IDs. The CLI uses the same request default (`--max-requests`
can lower it locally). Audits share the total request budget and never bypass
the 50-request GitHub quota reserve, rate-limit backoff, or retry checkpoints.
Collection pauses after 45 minutes and saves partial progress. The workflow has
a 60-minute timeout to leave time for persistence and deployment.

History is a compact schema-versioned JSON snapshot on the dedicated `data`
branch. This deliberately simple v1 layout has one atomic file replacement for
evidence **and** checkpoints, rather than a multi-file transaction or database.
Keys are repository + stable item ID, repository + event ID, and repository +
run ID + run attempt + item number. Run records also retain no-work batches and
unassigned technical failures. Retrying interrupted work is idempotent.
Nothing automatically deletes evidence. The snapshot will grow over time.

Only normalized public observations are retained. The parser is versioned and
supports the observed v2.2.0 and v2.3.0 action SHAs. Unknown action revisions or
result grammar remain unknown. Thresholds, action SHA, and cache hits are read
from actual logs. Main and release branches stay separate. Historical model
content identities are unavailable and are **not** invented from cache keys or
action versions.

Expired/deleted historical logs are retained as terminal evidence gaps, not
retried every hour. Recent missing logs, authorization errors, and rate limits
are distinct. Budget exhaustion defers work. API errors are recorded with
sanitized endpoint/status details, never response bodies or signed URLs.

## GitHub Actions and Pages

`refresh.yml` runs hourly at minute 17 and on manual dispatch. One concurrency
group serializes collection, normal non-force data pushes, and deployment.
It persists the data branch before rendering and deploying a Pages artifact.
Source commits do not contain monitoring updates. CI runs pytest and Ruff.

The actual refresh credential is preflighted against issue, timeline, run, job,
and recent log-download endpoints on both source repositories. The default is
the dashboard's `GITHUB_TOKEN`, **not** an assumption of cross-repository Actions
access. If preflight reports missing access, an administrator must configure
the `LABELER_READ_TOKEN` Actions secret with a read-only fine-grained PAT or
installation token granting **Actions, Issues, Pull requests, and Metadata read**
access to `dotnet/runtime` and `dotnet/machinelearning`. No source-write permission
is requested. Local credentials are never copied into Actions automatically.
The log probe checks up to three eligible older completed runs and two executed
jobs per run, with a five-minute completion buffer. Missing samples are reported
as unverified, not authorization failures. Metadata collection continues in that
case, and individual run failures remain explicit.

Changed or failed timelines are reconciled before unchanged background items.
The background request reservation alone is not reported as budget exhaustion
when no pending reconciliation remains. Rate limits and unfinished work still
report a pause.

For a new installation, initialize the `data` branch with a tested snapshot,
enable Pages with **GitHub Actions** as its source, then dispatch the workflow.
A failed collection can deploy the last persisted partial evidence with explicit
errors, but the workflow remains failed. Failed persistence prevents deployment.
The browser independently ages the stale-data banner after three hours, even if
no new build occurs. Backfill, source freshness, and timeline coverage are shown
separately.

### Browser caching

The generated HTML versions JS and CSS URLs by a hash of their contents.
New clients read `data-v2.json`. The public v2 contract is additive: new fields
do not change its schema number or remove existing fields. `data.json` remains
a v2 alias for previously deployed clients. The new client also understands v1
reports, showing legacy added/removed observations without inventing full label
states. The persisted history schema is unchanged.

A strict v1 script already running in a tab cannot be replaced by a static-site
deployment. Navigating to the newly versioned entry loads the corrected client.
Tests reproduce the old cached-script/new-data failure, then verify recovery
through a fresh entry in the same warm browser. They also cover the v2 alias,
legacy report input, additive fields, and chart filters.

## Scope and privacy

No upstream changes, webhooks, service, database server, LLM judging, or custom
notifications. Source API requests are read-only. Source PR code is never checked
out or executed. Public output contains minimal item metadata, label events, and
normalized prediction evidence. No issue bodies, titles, raw logs, credentials,
or model files are published. Static content uses text nodes, strict GitHub link
allowlists, and a self-only content security policy.
