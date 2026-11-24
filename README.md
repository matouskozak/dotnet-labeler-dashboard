# .NET labeler dashboard

Read-only production monitoring for the .NET issue labeler.

**Dashboard:** https://matouskozak.github.io/dotnet-labeler-dashboard/

## What it measures

Enabled streams are runtime, Roslyn, Extensions, Aspire, and Git Extensions issues and
pull requests, plus ML.NET issues.
ML.NET pull requests are **not enabled**, not failures. Intake includes open,
closed, and merged items. Filters select repository, item type, and creation cohort.
The dashboard opens on runtime issues.

The dashboard separates confirmed area applications, explicit no-selection results,
application failures, and unknown evidence. It shows dashboard cohort items
currently lacking an area label (with or without a fallback marker or observed
run), human area changes, fallback resolutions, and bot
reapplications. Each coverage card shows the `dotnet/issue-labeler` version used by
the stream's latest automatic run (short SHA when the version is not recognized). It does **not** claim prediction accuracy, precision, or exact
scoring coverage.

Area-label recognition is case-insensitive, while original spelling is preserved
(for example, Roslyn's `Area-Compilers`). Git Extensions uses `area: `, recognized
only for `gitextensions/gitextensions`. Other repositories use `area-`.
Roslyn's supported v2.0.0 predictor does
not configure a fallback label. Its explicit abstentions count as **No label
chosen** in results and daily activity and remain in the needs-area table.
They are not treated as errors or unknown solely because a fallback is absent.
The logs do not always distinguish no scores from below-threshold predictions.

Git Extensions PR prediction targets `main` and `release/**`, not its default
`master` branch. The collector reads PR base branches for this scoped stream.
Known out-of-scope PRs without automatic predictor evidence are kept in history
but excluded from dashboard opportunities. Unknown bases remain visible as
unknown. Existing automatic evidence is retained if a PR later changes its base.
Manual runs remain outside production metrics. Its separate deterministic
Windows-error triage job is not counted as a prediction or model abstention.

Aspire PR prediction also targets `main` and `release/**`. Its automatic predictor
handles opened PRs, not reopened/closed/assignee events handled by another job.
The scheduled poll currently has a `dotnet` owner guard, so it does not poll
`microsoft/aspire`. A scheduled run with no selected items is not evidence of
prediction. Source workflow behavior is observed, not changed by this dashboard.

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
The 7/30-day denominator requires both elapsed time and verified timeline coverage
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
edits (`actor_type=User`) made after a confirmed automatic area or
`needs-area-label` application on the same item. Manual triage of items the
labeler never labeled is not counted. Each item counts once per day per line. Retries and
add/remove pairs do not inflate counts. The lines may overlap. Repository, type,
and creation-period filters select the items, and the plot shows activity within
that period. The 7/30-day selections use UTC calendar days, including today.
Dashed lines and hollow points are partial observed counts. Human-change coverage
requires complete intake and timelines for the tracked items on that UTC day.
A human edit not yet counted is partial while that item's labeler evidence is
unresolved or unmatched, or prediction coverage is incomplete between the item's
creation and the edit. Applied/no-selection coverage also requires discovered run
history and usable prediction evidence. Runs that never ran the prediction step
(still queued, awaiting approval, failed to start, or skipped prediction) remain
in workflow status but do not make daily prediction counts partial, because they
could not apply a label. Unresolved runs whose prediction step ran affect only
their recorded execution interval. Each line shows zero only when its own coverage is complete. Partial
first days, today, and missing history stay partial or unknown, not zero.

These are counts for the currently tracked cohort, not a promise that future
evidence will never change membership. Unknown/no-run items remain included.
A later supported skip-only result can exclude an item and its historical counts.
The public report keeps the conservative `complete` field for older clients and
adds `series_complete` and `partial_reason` for the separate chart lines.

Coverage cards distinguish collection from result availability. **Caught up**
means intake, discovery, parser reprocessing, and timeline reconciliation have
finished through the displayed refresh time. Missing prediction results, pending
source runs, and failed/blocked runs are listed separately. They do not imply
unfinished collection or invent a model outcome.

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

A completed, error-free intake scan can extend coverage for an already fetched
timeline when the item's `updated_at` still matches `timeline_source_updated`.
This applies to closed items too. Missing timelines, pending reconciliation,
unavailable items, and timeline or intake errors prevent that inference. Daily
coverage, intake windows, and removal follow-up use the corroborated coverage.
Raw `observed_through` remains the last complete timeline fetch. The report's
separate `timeline_coverage_through` never creates events or changes fetch times.

An unavailable or redirected timeline is not assumed to be deleted. Only after
healthy source-level preflight, it gets two bounded retries (after one hour and
one day), then an explicit unavailable marker. A newly observed item update or
label event permits reconciliation again. Items and prior evidence are retained
in intake denominators. Missing timeline follow-up never matures just by waiting.
Redirects are not followed to other repositories or arbitrary hosts.

Only the configured prediction workflows are queried. Run-created windows are
split below GitHub's 1,000-result cap, with full pagination and bounded request
budgets. Recent work is collected before the resumable six-hour backfill windows.
Discovery checkpoints advance only after the full window listing and run records
are persisted together. A recorded queued or other nonterminal source run does
not block discovery of later windows. It remains `collected=false`, with pending
evidence and incomplete production/day coverage, until a later audit completes it.
Listing, fetch, or budget interruptions still prevent unsafe checkpoint advancement.
A rotating audit of known run identities detects
reruns with old creation timestamps. Such reruns can remain undiscovered until
their audit turn, shown in operations.
Failed run lookups retain their errors and back off independently of successful
checks, so inaccessible runs do not block later audits. Coverage gaps use actual
attempt/job execution times where available. Missing execution times leave the
possible interval incomplete rather than assigning a rerun to its creation day.
Known unfinished attempts and stale-parser history alternate audit turns before
routine checks, including when a later attempt is already complete. Stuck source
jobs cannot consume every audit slot while parser upgrades remain.

Every scheduled or manual refresh allows up to 4,000 requests and audits up to
100 old-run root IDs. The CLI uses the same request default (`--max-requests`
can lower it locally). Audits share the total request budget and never bypass
the 50-request GitHub quota reserve, rate-limit backoff, or retry checkpoints.
Collection pauses after 45 minutes and saves partial progress. The workflow has
a 60-minute timeout to leave time for persistence and deployment.
After the bounded per-stream pass and old-run audit, unfinished backfills reuse
unspent requests in six-hour window turns. Caught-up streams do not hold their
unused share. A blocked or interrupted window waits for the next refresh rather
than repeatedly consuming the remaining budget.

History is a compact schema-versioned JSON snapshot on the dedicated `data`
branch. This deliberately simple v1 layout has one atomic file replacement for
evidence **and** checkpoints, rather than a multi-file transaction or database.
Keys are repository + stable item ID, repository + event ID, and repository +
run ID + run attempt + item number. Run records also retain no-work batches and
unassigned technical failures. Retrying interrupted work is idempotent.
Nothing automatically deletes evidence. The snapshot will grow over time.

Only normalized public observations are retained. The parser is versioned and
supports the observed v2.0.0, v2.2.0, and v2.3.0 action SHAs. Unknown action revisions or
result grammar remain unknown. Thresholds, action SHA, and cache hits are read
from actual logs. Main and release branches stay separate. Historical model
content identities are unavailable and are **not** invented from cache keys or
action versions.

Expired/deleted historical logs are retained as terminal evidence gaps, not
retried every hour. Recent missing logs, authorization errors, and rate limits
are distinct. Budget exhaustion defers work. API errors are recorded with
sanitized endpoint/status details, never response bodies or signed URLs.

## GitHub Actions and Pages

`refresh.yml` is scheduled hourly at minute 17 and supports manual dispatch.
GitHub may delay or skip scheduled starts, so collection is not guaranteed hourly.
One concurrency
group serializes collection, normal non-force data pushes, and deployment.
It persists the data branch before rendering and deploying a Pages artifact.
Source commits do not contain monitoring updates. CI runs pytest and Ruff.

For publication recovery, manually dispatch with `publish_only=true`. This reads
the latest remote data branch and renders/deploys it without source collection
or a data commit/push. The default is false, so scheduled and normal manual
refreshes are unchanged. After upload, deployment checks this run's Pages artifact
metadata up to six times, ten seconds apart. Only empty successful listings are
retried. API errors or a missing artifact after that bound fail visibly.

The actual refresh credential is preflighted against issue, timeline, run, job,
and recent log-download endpoints on every configured source repository. The default is
the dashboard's `GITHUB_TOKEN`, **not** an assumption of cross-repository Actions
access. If preflight reports missing access, an administrator must configure
the `LABELER_READ_TOKEN` Actions secret with a read-only fine-grained PAT or
installation token granting **Actions, Issues, Pull requests, and Metadata read**
access to the enabled source repositories in `config/repos.json`. No source-write permission
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

An open dashboard checks for new data every five minutes while visible and when
returning to the tab. It keeps repository/type/period selections, open details,
scroll position, and focus where the same control still exists. Unchanged reports
do not rerender the tables or chart. Requests do not overlap and time out after
20 seconds. Older or invalid reports are rejected. A failed refresh keeps the last
valid report visible, shows its data timestamp with a warning, and retries later.
These browser checks load already published data. They do not trigger collection
or guarantee that GitHub has run the hourly workflow.

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
