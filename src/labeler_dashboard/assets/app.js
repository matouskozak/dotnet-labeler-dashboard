"use strict";
let data;
let renderRoot;
let refreshError = null;
let refreshInFlight = false;
let lastPayload;
const $ = id => renderRoot ? renderRoot.querySelector(`#${id}`) : document.getElementById(id);
const node = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = String(text);
  if (className) element.className = className;
  return element;
};
const date = (value, seconds = false) => value ? new Date(value).toISOString().replace("T", " ").slice(0, seconds ? 19 : 16) + " UTC" : "Unknown";
const label = value => value.replaceAll("_", " ");
const labelState = values => Array.isArray(values) ? (values.length ? values.join(", ") : "No area label") : "Unknown";
const series = [
  ["applied", "Labels applied", "#006c5b"],
  ["no_selection", "No label chosen", "#986000"],
  ["human_changed", "Human label changes", "#346cba"],
];
const sourceRepos = new Set([
  "dotnet/runtime", "dotnet/machinelearning", "dotnet/roslyn", "dotnet/extensions",
  "microsoft/aspire", "gitextensions/gitextensions",
]);
const age = seconds => {
  if (seconds == null) return "Unknown";
  if (seconds < 60) return `${Math.max(0, Math.floor(seconds))}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
};
function safeLink(text, url) {
  const match = /^https:\/\/github\.com\/([^/]+\/[^/]+)\/(?:issues|pull|actions\/runs)\/\d+$/.exec(url || "");
  if (!match || !sourceRepos.has(match[1])) return node("span", text);
  const link = node("a", text);
  link.href = url;
  link.rel = "noopener noreferrer";
  return link;
}
function itemCell(item) {
  const cell = node("td");
  cell.append(safeLink(`#${item.number}`, item.url), node("span", `${item.repo} / ${item.kind === "pr" ? "PR" : "issue"}`, "secondary"));
  return cell;
}
function emptyTable(id, columns, message) {
  const tr = node("tr");
  const td = node("td", message, "empty");
  td.colSpan = columns;
  tr.append(td);
  $(id).append(tr);
}
function periodStart() {
  const end = data.built_at.slice(0, 10);
  const historyStart = data.cohort_start ? data.cohort_start.slice(0, 10) : end;
  if ($("cohort").value === "all") return historyStart;
  const first = new Date(`${end}T00:00:00Z`);
  first.setUTCDate(first.getUTCDate() - Number($("cohort").value) + 1);
  return [historyStart, first.toISOString().slice(0, 10)].sort().at(-1);
}
function selected(item) {
  if ($("repo").value && item.repo !== $("repo").value) return false;
  if ($("kind").value && item.kind !== $("kind").value) return false;
  return $("cohort").value === "all" || item.created_at.slice(0, 10) >= periodStart();
}
function dailyRows(items) {
  const streams = data.streams.filter(stream => stream.enabled &&
    (!$("repo").value || stream.repo === $("repo").value) &&
    (!$("kind").value || stream.kind === $("kind").value));
  if (!streams.length || !data.cohort_start) return [];
  const end = data.built_at.slice(0, 10);
  const start = periodStart();
  const counts = new Map();
  for (const item of items) {
    const seen = new Set();
    for (const activity of item.activity || []) {
      if (activity.date < start || activity.date > end) continue;
      if (!counts.has(activity.date)) counts.set(activity.date, {});
      for (const name of activity.series) {
        const key = `${activity.date}:${name}`;
        if (seen.has(key)) continue;
        seen.add(key);
        const day = counts.get(activity.date);
        day[name] = (day[name] || 0) + 1;
      }
    }
  }
  const rows = [];
  for (let cursor = new Date(`${start}T00:00:00Z`); cursor.toISOString().slice(0, 10) <= end; cursor.setUTCDate(cursor.getUTCDate() + 1)) {
    const day = cursor.toISOString().slice(0, 10);
    const coverage = streams.map(stream => (stream.daily_coverage || []).find(row => row.date === day));
    const seriesComplete = {}, partialReason = {};
    for (const [name] of series) {
      seriesComplete[name] = coverage.every(row => row?.series_complete?.[name] ?? row?.complete ?? false);
      const reasons = coverage.filter(row => !(row?.series_complete?.[name] ?? row?.complete ?? false))
        .map(row => row?.partial_reason?.[name] || "history_incomplete");
      partialReason[name] = ["partial_day", "history_incomplete", "results_missing"].find(reason => reasons.includes(reason));
    }
    rows.push({date: day, counts: counts.get(day) || {}, complete: coverage.every(row => row?.complete),
      series_complete: seriesComplete, partial_reason: partialReason});
  }
  return rows;
}
function renderTrend(items) {
  if (!$("trend")) return;
  const rows = dailyRows(items);
  $("trend").replaceChildren();
  $("trend-values").replaceChildren();
  $("trend-legend").replaceChildren(...series.map(([name, text]) => node("span", text, name)));
  if (!rows.length || data.items.some(item => !Array.isArray(item.activity))) {
    $("trend").append(node("p", "No daily data for this selection.", "caption"));
    return;
  }
  const svgNode = (tag, attributes, text) => {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
    if (text !== undefined) element.textContent = text;
    return element;
  };
  const svg = svgNode("svg", {viewBox: "0 0 960 260", role: "img", "aria-label": "Daily item counts", "aria-describedby": "trend-note"});
  svg.append(svgNode("title", {}, "Daily item counts"), svgNode("desc", {}, "Each line counts an item once per day. Hollow points and dashed lines are partial observations. Missing counts are unknown."));
  const maximum = Math.max(1, ...rows.flatMap(row => series.map(([name]) => row.counts[name] || 0)));
  const x = index => 45 + index * 895 / Math.max(1, rows.length - 1);
  const y = value => 220 - value * 200 / maximum;
  for (const value of [...new Set([0, Math.ceil(maximum / 2), maximum])]) {
    svg.append(svgNode("line", {x1: 45, y1: y(value), x2: 940, y2: y(value), stroke: "#dce4e8"}));
    svg.append(svgNode("text", {x: 35, y: y(value) + 4, "text-anchor": "end", class: "axis"}, value));
  }
  const head = node("tr");
  head.append(node("th", "Date"), ...series.map(([, text]) => node("th", text)));
  $("trend-values").append(head);
  rows.forEach((row, index) => {
    if (index === 0 || index === rows.length - 1 || index % Math.ceil(rows.length / 6) === 0) {
      svg.append(svgNode("text", {x: x(index), y: 246, "text-anchor": "middle", class: "axis"}, row.date.slice(5)));
    }
    const tr = node("tr");
    tr.append(node("th", row.date));
    for (const [name, text, color] of series) {
      const count = row.counts[name] || 0;
      const complete = row.series_complete[name];
      const reason = {partial_day: "partial day", history_incomplete: "history not collected", results_missing: "prediction results missing"}[row.partial_reason[name]];
      const visible = complete || count > 0;
      tr.append(node("td", complete ? count : count > 0 ? `${count} (partial: ${reason})` : "Unknown"));
      if (!visible) continue;
      const previous = rows[index - 1];
      if (previous && (previous.series_complete[name] || previous.counts[name] > 0)) {
        svg.append(svgNode("line", {x1: x(index - 1), y1: y(previous.counts[name] || 0), x2: x(index), y2: y(count),
          stroke: color, "stroke-width": 2, "stroke-dasharray": complete && previous.series_complete[name] ? "none" : "5 4"}));
      }
      const description = `${row.date}: ${count} ${text.toLowerCase()}${complete ? "" : ` (partial: ${reason})`}`;
      const point = svgNode("circle", {cx: x(index), cy: y(count), r: 4, fill: complete ? color : "white",
        stroke: color, "stroke-width": 2, tabindex: 0, "aria-label": description});
      point.append(svgNode("title", {}, description));
      svg.append(point);
    }
    $("trend-values").append(tr);
  });
  $("trend").append(svg);
}
function changeLabels(change) {
  if (Array.isArray(change.before) && Array.isArray(change.after)) {
    return [labelState(change.before), labelState(change.after)];
  }
  // Legacy reports have observations, not complete before/after label sets.
  if (change.replacement && change.replacement.includes(" -> ")) {
    const [before, after] = change.replacement.split(" -> ");
    return [`Removed ${before}`, `Added ${after}`];
  }
  if (["human_area_removed", "human_fallback_removed"].includes(change.category)) {
    return [`Removed ${change.label}`, "Unknown"];
  }
  return ["Unknown", change.label ? `Added ${change.label}` : "Unknown"];
}
function incompleteProduction(stream) {
  return stream.production_incomplete_runs ?? (
    ["unknown", "unavailable", "pending", "parser_upgrade_pending"].some(key => stream.run_evidence[key]) ? 1 : 0
  );
}
function collectionCaughtUp(stream) {
  return stream.collection_caught_up ?? (stream.intake_complete && stream.timelines === stream.items &&
    !stream.pending_timelines && stream.backfill_to >= stream.live_to && !stream.last_error &&
    !incompleteProduction(stream));
}
function freshness() {
  const banner = $("freshness");
  if (refreshError) {
    banner.className = `notice ${data ? "warning" : "failure"}`;
    banner.textContent = data
      ? `Refresh failed: ${refreshError}. Showing data through ${date(data.data_as_of)}. Will retry.`
      : `Could not load data: ${refreshError}. Will retry.`;
    return;
  }
  if (!data) return;
  const elapsed = data.data_as_of ? (Date.now() - new Date(data.data_as_of)) / 1000 : null;
  const checks = Object.values(data.permissions).flatMap(repo => Object.values(repo.checks));
  const denied = checks.some(check => !check.ok && check.state !== "unverified");
  const unverified = checks.some(check => check.state === "unverified");
  const stale = elapsed == null || elapsed > 3 * 3600;
  banner.className = "notice" + (denied ? " failure" : stale || unverified ? " warning" : "");
  const collecting = data.streams.some(stream => stream.enabled && !collectionCaughtUp(stream));
  banner.textContent = denied
    ? "Source access failed. Results are incomplete. See Details."
    : unverified
      ? "Some source access checks are unverified. See Details."
    : stale
      ? `Data is stale. Last discovery refresh: ${date(data.data_as_of)}.`
      : `Updated ${date(data.data_as_of)}. ${collecting ? "Collection is incomplete." : "Collection caught up."}${data.errors.length ? " Recorded errors in Details." : ""}`;
}
function render() {
  const items = data.items.filter(selected);
  const itemIndex = new Map(items.map(item => [item.key, item]));
  const built = new Date(data.built_at);
  $("cohort-note").textContent = `${items.length} items`;
  renderTrend(items);
  $("streams").replaceChildren();
  for (const stream of data.streams) {
    const card = node("article", undefined, "stream");
    card.append(node("h3", `${stream.repo} / ${stream.kind === "pr" ? "Pull requests" : "Issues"}`));
    const complete = stream.enabled && collectionCaughtUp(stream);
    card.append(node("span", !stream.enabled ? "NOT ENABLED" : complete ? "CAUGHT UP" : "COLLECTING", `badge ${!stream.enabled ? "disabled" : complete ? "" : "pending"}`));
    if (!stream.enabled) {
      card.append(node("p", "Not monitored."));
    } else {
      const cohortItems = items.filter(item => item.repo === stream.repo && item.kind === stream.kind);
      card.append(node("p", `${cohortItems.length} selected / ${stream.items} collected items`),
        node("p", `${stream.timelines} timelines / ${stream.pending_timelines} pending`));
      if (stream.labeler_sha) card.append(node("p", `issue-labeler ${stream.labeler_version || stream.labeler_sha.slice(0, 7)}`));
      if (stream.base_branches) card.append(node("p", `PR bases: ${stream.base_branches.join(", ")} only`));
      const results = stream.prediction_results;
      if (results) {
        if (results.missing) card.append(node("p", `Prediction results missing for ${results.missing} ${results.missing === 1 ? "run" : "runs"}`));
        if (results.pending) card.append(node("p", `${results.pending} ${results.pending === 1 ? "run pending" : "runs pending"}`));
        if (results.failed_or_blocked) card.append(node("p", `${results.failed_or_blocked} failed or blocked ${results.failed_or_blocked === 1 ? "run" : "runs"}`));
      } else if (incompleteProduction(stream)) {
        card.append(node("p", `${incompleteProduction(stream)} unresolved runs`));
      }
    }
    $("streams").append(card);
  }
  $("metrics").replaceChildren();
  const metrics = [
    ["confirmed", "Confirmed labels", true],
    ["probable", "Unconfirmed applications"],
    ["no_selection", "No label chosen"],
    ["technical_failure", "Application failures"],
    ["unknown", "Unknown outcomes"],
    ["needs_area", "No area label"],
  ];
  for (const [field, title, primary] of metrics) {
    const card = node("div", undefined, `metric${primary ? " primary" : ""}`);
    card.append(node("div", items.filter(item => field === "no_selection" ? (item.no_selection ?? item.fallback) : item[field]).length, "value"), node("div", title, "label"));
    $("metrics").append(card);
  }
  $("followup").replaceChildren();
  for (const days of [7, 30]) {
    const eligible = items.filter(item => item[`removal_${days}`] !== null);
    const removed = eligible.filter(item => item[`removal_${days}`]).length;
    const row = node("div", undefined, "stat-line");
    row.append(node("span", `${days}-day human area removal`),
      node("strong", eligible.length ? `${removed} / ${eligible.length} (${(removed / eligible.length * 100).toFixed(1)}%)` : "Not enough follow-up"));
    $("followup").append(row);
  }
  $("intake").replaceChildren();
  for (const hours of [1, 24]) {
    const eligible = items.map(item => item[`intake_${hours}`]).filter(Boolean);
    const confirmed = eligible.filter(row => row.status === "confirmed").length;
    const incomplete = eligible.filter(row => !row.evidence_complete).length;
    const row = node("div", undefined, "stat-line");
    row.append(node("span", `${hours}h from creation`), node("strong", eligible.length ? `${confirmed} labeled / ${eligible.length} eligible items` : "No eligible items"));
    $("intake").append(row, node("p", `${incomplete} with incomplete history.`, "caption"));
  }
  const waiting = items.filter(item => item.needs_area).sort((a, b) => a.created_at.localeCompare(b.created_at));
  $("waiting").replaceChildren();
  $("waiting-count").textContent = `${waiting.length} waiting`;
  for (const item of waiting) {
    const tr = node("tr");
    tr.append(itemCell(item), node("td", `${item.state}${item.timeline_unavailable ? " / timeline unavailable" : ""}`), node("td", age((built - new Date(item.created_at)) / 1000)), node("td", item.fallback_marker ? "Present" : "Absent"),
      node("td", date(item.waiting_since)), node("td", label(item.original_outcome)));
    $("waiting").append(tr);
  }
  if (!waiting.length) emptyTable("waiting", 6, "None recorded in this period.");
  const allChanges = $("change-scope").value === "all";
  const changes = data.changes.filter(change => itemIndex.has(change.item) &&
    (allChanges || change.human_after_application === true)).sort((a, b) => b.at.localeCompare(a.at));
  $("changes").replaceChildren();
  $("changes-count").textContent = `${changes.length} ${changes.length === 1 ? "edit" : "edits"}`;
  for (const change of changes) {
    const item = itemIndex.get(change.item);
    const tr = node("tr");
    const [before, after] = changeLabels(change);
    tr.append(itemCell(item), node("td", before), node("td", after),
      node("td", change.actor || "Unknown"), node("td", date(change.at, true)));
    $("changes").append(tr);
  }
  if (!changes.length) emptyTable("changes", 5, "None recorded in this period.");
  $("configuration").replaceChildren();
  for (const config of data.configurations) {
    const tr = node("tr");
    tr.append(node("td", `${config.repo} / ${config.kind} / ${config.branch || "unknown branch"}`),
      node("td", config.threshold ?? "Unknown"),
      node("td", `${config.action_version || "Unsupported / unknown"} (${config.action_sha || "SHA unknown"})`),
      node("td", config.cache_hit === null ? "Unknown" : config.cache_hit ? "Yes" : "No"),
      node("td", "Unknown"), node("td", config.run_attempts));
    $("configuration").append(tr);
  }
  if (!data.configurations.length) emptyTable("configuration", 6, "No configuration data.");
  $("permissions").replaceChildren();
  for (const [repo, permission] of Object.entries(data.permissions)) {
    for (const [endpoint, check] of Object.entries(permission.checks)) {
      $("permissions").append(node("div", `${repo} / ${endpoint}: ${check.ok ? "OK" : check.reason} (${date(permission.checked_at)})`, "ops-row"));
    }
  }
  $("operations").replaceChildren(
    node("div", `Build: ${date(data.built_at)}. Refresh attempted: ${date(data.last_refresh_attempt)}. Requests used: ${data.requests_used ?? "unknown"}. Remaining API quota: ${data.rate_remaining ?? "unknown"}. Budget pause: ${data.budget_paused ? "yes (resumable)" : "no"}.`, "ops-row")
  );
  for (const stream of data.streams.filter(stream => stream.enabled)) {
    $("operations").append(node("div", `${stream.repo} / ${stream.kind}: runs discovered through ${date(stream.backfill_to)}. Last live refresh ${date(stream.last_success)}.${stream.last_error ? " " + stream.last_error : ""}${stream.feed_gap ? " Event-feed gap recorded." : ""}`, "ops-row"));
    $("operations").append(node("div", `${stream.repo} / ${stream.kind}: ${stream.run_attempts} collected workflow run attempts (all items, independent of dashboard cohort). ${Object.entries(stream.run_evidence).map(([name, count]) => `${label(name)} ${count}`).join(", ") || "No runs observed"}. Oldest retry audit: ${date(stream.oldest_retry_check)}.`, "ops-row"));
    if (Object.keys(stream.failure_phases).length) $("operations").append(node("div", `Explicit failed steps: ${Object.entries(stream.failure_phases).map(([name, count]) => `${label(name)} ${count}`).join(", ")}. These are run-level failures, not inferred per-item outcomes.`, "ops-row"));
  }
  $("operations").append(node("p", "History collection resumes each refresh. Old reruns are checked in rotation. Expired logs remain gaps.", "caption"));
  $("errors").replaceChildren();
  for (const error of data.errors) {
    $("errors").append(node("div", `${date(error.last_seen)} / ${error.stage}: ${error.message} (${error.count} occurrences)`, "ops-row"));
  }
  if (!data.errors.length) $("errors").append(node("p", "None recorded.", "caption"));
  freshness();
}
function validateReport(value) {
  const object = v => v !== null && typeof v === "object" && !Array.isArray(v);
  const text = v => typeof v === "string";
  const boolean = v => typeof v === "boolean";
  const count = v => Number.isInteger(v) && v >= 0;
  const timestamp = v => text(v) && Number.isFinite(Date.parse(v));
  const day = v => timestamp(v) && /^\d{4}-\d\d-\d\d$/.test(v);
  const nullable = (v, check) => v == null || check(v);
  const array = (v, check) => Array.isArray(v) && v.every(check);
  const counts = v => object(v) && Object.values(v).every(count);
  const intake = v => v === null || (object(v) && text(v.status) && boolean(v.evidence_complete));
  const optional = (v, key, check) => !(key in v) || check(v[key]);
  if (!object(value) || ![1, 2].includes(value.schema)) throw new Error("Unsupported data format");
  const valid = timestamp(value.built_at) && "data_as_of" in value && nullable(value.data_as_of, timestamp) &&
    nullable(value.cohort_start, timestamp) && nullable(value.last_refresh_attempt, timestamp) &&
    array(value.items, item => object(item) && text(item.key) && text(item.repo) &&
      ["issue", "pr"].includes(item.kind) && count(item.number) && text(item.state) &&
      nullable(item.url, text) && timestamp(item.created_at) && array(item.labels, text) &&
      ["confirmed", "probable", "fallback", "technical_failure", "unknown", "needs_area", "fallback_marker"]
        .every(key => boolean(item[key])) &&
      optional(item, "no_selection", boolean) && text(item.original_outcome) &&
      nullable(item.waiting_since, timestamp) && nullable(item.observed_through, timestamp) &&
      ["removal_7", "removal_30"].every(key => item[key] === null || boolean(item[key])) &&
      intake(item.intake_1) && intake(item.intake_24) &&
      optional(item, "activity", rows => array(rows, row =>
        object(row) && day(row.date) && array(row.series, text)))) &&
    array(value.changes, change => object(change) && text(change.item) && timestamp(change.at) &&
      nullable(change.actor, text) && optional(change, "human_after_application", boolean) && (
        (array(change.before, text) && array(change.after, text)) ||
        (value.schema === 1 && text(change.category) && nullable(change.label, text) &&
          nullable(change.replacement, text)))) &&
    array(value.streams, stream => object(stream) && text(stream.repo) &&
      ["issue", "pr"].includes(stream.kind) && boolean(stream.enabled) &&
      boolean(stream.intake_complete) &&
      ["items", "timelines", "pending_timelines", "run_attempts"].every(key => count(stream[key])) &&
      counts(stream.run_evidence) && counts(stream.failure_phases) &&
      ["backfill_to", "live_to", "intake_as_of"].every(key => nullable(stream[key], timestamp)) &&
      nullable(stream.last_success, timestamp) && nullable(stream.oldest_retry_check, timestamp) &&
      nullable(stream.last_error, text) &&
      optional(stream, "collection_caught_up", boolean) &&
      optional(stream, "production_incomplete_runs", count) &&
      optional(stream, "base_branches", rows => array(rows, text)) &&
      optional(stream, "labeler_version", v => nullable(v, text)) &&
      optional(stream, "labeler_sha", v => nullable(v, text)) &&
      optional(stream, "prediction_results", counts) &&
      optional(stream, "daily_coverage", rows => array(rows, row => object(row) &&
        day(row.date) && boolean(row.complete) &&
        optional(row, "series_complete", v => object(v) && Object.values(v).every(boolean)) &&
        optional(row, "partial_reason", v => object(v) && Object.values(v).every(r => nullable(r, text)))))) &&
    array(value.configurations, config => object(config) && text(config.repo) && text(config.kind) &&
      count(config.run_attempts) && nullable(config.branch, text) &&
      nullable(config.action_version, text) && nullable(config.action_sha, text) &&
      nullable(config.threshold, v => typeof v === "number" && Number.isFinite(v)) &&
      nullable(config.cache_hit, boolean)) &&
    array(value.errors, error => object(error) && text(error.stage) && text(error.message) &&
      timestamp(error.last_seen) && count(error.count)) &&
    object(value.permissions) && Object.values(value.permissions).every(permission =>
      object(permission) && timestamp(permission.checked_at) && object(permission.checks) &&
      Object.values(permission.checks).every(check => object(check) && boolean(check.ok) &&
        nullable(check.reason, text)));
  if (!valid) throw new Error("Invalid dashboard data");
  const keys = new Set(value.items.map(item => item.key));
  if (keys.size !== value.items.length || value.changes.some(change => !keys.has(change.item))) {
    throw new Error("Invalid dashboard item references");
  }
}
function applyReport(value) {
  const current = document.querySelector("main");
  const next = current.cloneNode(true);
  const filters = Object.fromEntries(["repo", "kind", "cohort"].map(id => [id, $(id).value]));
  const scroll = [window.scrollX, window.scrollY];
  const focused = document.activeElement;
  const focusSelector = !current.contains(focused) ? null
    : focused.id ? `#${CSS.escape(focused.id)}`
    : focused.matches("summary") ? "summary"
    : focused.matches("a[href]") ? `a[href="${CSS.escape(focused.getAttribute("href"))}"]`
    : focused.hasAttribute("aria-label") ? `[aria-label="${CSS.escape(focused.getAttribute("aria-label"))}"]`
    : null;
  const previous = data;
  try {
    // Render off-document so an invalid update cannot mix old and new report sections.
    renderRoot = next;
    data = value;
    const repos = [...new Set(value.streams.map(stream => stream.repo))];
    if (filters.repo && !repos.includes(filters.repo)) repos.push(filters.repo);
    $("repo").replaceChildren(new Option("All repositories", ""),
      ...repos.map(repo => new Option(repo, repo)));
    for (const [id, selection] of Object.entries(filters)) $(id).value = selection;
    render();
  } catch (error) {
    data = previous;
    throw error;
  } finally {
    renderRoot = undefined;
  }
  current.replaceWith(next);
  if (focusSelector) next.querySelector(focusSelector)?.focus({preventScroll: true});
  window.scrollTo(...scroll);
}
async function refreshData() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch("data-v2.json", {cache: "no-store", signal: controller.signal});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.text();
    if (controller.signal.aborted) throw new Error("Request timed out");
    if (payload !== lastPayload) {
      let value;
      try {
        value = JSON.parse(payload);
      } catch {
        throw new Error("Invalid dashboard JSON");
      }
      validateReport(value);
      if (data && (Date.parse(value.built_at) < Date.parse(data.built_at) ||
        (data.data_as_of && value.data_as_of && Date.parse(value.data_as_of) < Date.parse(data.data_as_of)))) {
        throw new Error("Server returned an older snapshot");
      }
      refreshError = null;
      applyReport(value);
      lastPayload = payload;
    }
    refreshError = null;
  } catch (error) {
    refreshError = controller.signal.aborted ? "Request timed out" : error.message;
  } finally {
    clearTimeout(timeout);
    refreshInFlight = false;
    freshness();
  }
}
document.addEventListener("change", event => {
  if (data && ["repo", "kind", "cohort", "change-scope"].includes(event.target.id)) render();
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshData();
});
setInterval(() => {
  if (!document.hidden) refreshData();
}, 5 * 60000);
setInterval(freshness, 60000);
refreshData();
