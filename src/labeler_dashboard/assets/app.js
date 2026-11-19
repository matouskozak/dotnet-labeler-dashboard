"use strict";
let data;
const $ = id => document.getElementById(id);
const node = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = String(text);
  if (className) element.className = className;
  return element;
};
const date = value => value ? new Date(value).toISOString().replace("T", " ").slice(0, 16) + " UTC" : "Unknown";
const label = value => value.replaceAll("_", " ");
const age = seconds => {
  if (seconds == null) return "Unknown";
  if (seconds < 60) return `${Math.max(0, Math.floor(seconds))}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
};
function safeLink(text, url) {
  if (!/^https:\/\/github\.com\/dotnet\/(?:runtime|machinelearning)\/(?:issues|pull|actions\/runs)\/\d+$/.test(url || "")) return node("span", text);
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
function selected(item) {
  if ($("repo").value && item.repo !== $("repo").value) return false;
  if ($("kind").value && item.kind !== $("kind").value) return false;
  return $("cohort").value === "all" ||
    new Date(item.created_at) >= new Date(new Date(data.built_at) - Number($("cohort").value) * 86400000);
}
function freshness() {
  const banner = $("freshness");
  const elapsed = data.data_as_of ? (Date.now() - new Date(data.data_as_of)) / 1000 : null;
  const denied = Object.values(data.permissions).some(repo => Object.values(repo.checks).some(check => !check.ok));
  const stale = elapsed == null || elapsed > 3 * 3600;
  banner.className = "notice" + (denied ? " failure" : stale ? " warning" : "");
  banner.textContent = denied
    ? "Source-read preflight incomplete. Results are partial. See Configuration & operations for the exact endpoint and credential requirement."
    : stale
      ? `Stale or incomplete refresh. Last complete live scan: ${date(data.data_as_of)}. This warning ages in your browser even if builds stop.`
      : `Live scan through ${date(data.data_as_of)} (${age(elapsed)} ago). Historical backfill and timeline completeness are shown separately below.`;
}
function render() {
  const items = data.items.filter(selected);
  const itemIndex = new Map(items.map(item => [item.key, item]));
  const built = new Date(data.built_at);
  $("cohort-note").textContent = `${items.length} dashboard items. Intake began ${date(data.cohort_start)}.`;
  $("streams").replaceChildren();
  for (const stream of data.streams) {
    const card = node("article", undefined, "stream");
    card.append(node("h3", `${stream.repo.replace("dotnet/", "")} / ${stream.kind === "pr" ? "Pull requests" : "Issues"}`));
    const complete = stream.enabled && stream.intake_complete && stream.timelines === stream.items &&
      !stream.pending_timelines && stream.backfill_to >= stream.live_to && !stream.last_error &&
      !["unknown", "unavailable", "pending", "parser_upgrade_pending"].some(key => stream.run_evidence[key]);
    card.append(node("span", !stream.enabled ? "NOT ENABLED" : complete ? "COLLECTED" : "PARTIAL / BACKFILL", `badge ${!stream.enabled ? "disabled" : complete ? "" : "pending"}`));
    if (!stream.enabled) {
      card.append(node("p", "Excluded by design. Not a failure."));
    } else {
      const cohortItems = items.filter(item => item.repo === stream.repo && item.kind === stream.kind);
      card.append(node("p", `${stream.items} dashboard items / ${stream.timelines} fully fetched timelines`),
        node("p", `${stream.pending_timelines} timelines pending reconciliation`),
        node("p", `Selected cohort: ${cohortItems.filter(item => item.confirmed).length} confirmed areas / ${cohortItems.filter(item => item.fallback).length} observed fallbacks`),
        node("p", `Run backfill through ${date(stream.backfill_to)}`),
        node("p", `Last live success ${date(stream.last_success)}`));
      if (stream.last_error) card.append(node("p", stream.last_error));
      if (stream.feed_gap) card.append(node("p", "Event-feed gap detected. Complete timelines are authoritative."));
    }
    $("streams").append(card);
  }
  $("metrics").replaceChildren();
  const metrics = [
    ["confirmed", "Confirmed area applications", true],
    ["probable", "Applied result, unconfirmed mutation"],
    ["fallback", "Observed no-selection fallbacks"],
    ["technical_failure", "Explicit item application failures"],
    ["unknown", "Items with unknown evidence"],
    ["needs_area", "Currently waiting for an area"],
  ];
  for (const [field, title, primary] of metrics) {
    const card = node("div", undefined, `metric${primary ? " primary" : ""}`);
    card.append(node("div", items.filter(item => item[field]).length, "value"), node("div", title, "label"));
    $("metrics").append(card);
  }
  const attempts = node("div", undefined, "metric");
  attempts.append(node("div", items.reduce((sum, item) => sum + item.production_attempt_count, 0), "value"), node("div", "Production attempts (not samples)", "label"));
  $("metrics").append(attempts);
  $("followup").replaceChildren();
  for (const days of [7, 30]) {
    const eligible = items.filter(item => item[`removal_${days}`] !== null);
    const removed = eligible.filter(item => item[`removal_${days}`]).length;
    const row = node("div", undefined, "stat-line");
    row.append(node("span", `${days}-day human area removal`),
      node("strong", eligible.length ? `${removed} / ${eligible.length} (${(removed / eligible.length * 100).toFixed(1)}%)` : "Insufficient mature evidence"));
    $("followup").append(row);
  }
  $("intake").replaceChildren();
  for (const hours of [1, 24]) {
    const eligible = items.map(item => item[`intake_${hours}`]).filter(Boolean);
    const confirmed = eligible.filter(row => row.status === "confirmed").length;
    const incomplete = eligible.filter(row => !row.evidence_complete).length;
    const row = node("div", undefined, "stat-line");
    row.append(node("span", `${hours}h from creation`), node("strong", eligible.length ? `${confirmed} confirmed / ${eligible.length} mature intakes` : "No mature intake yet"));
    $("intake").append(row, node("p", `${incomplete} have incomplete evidence. Skip-only pre-labeled items are excluded from the dashboard cohort.`, "caption"));
  }
  const waiting = items.filter(item => item.needs_area).sort((a, b) => a.created_at.localeCompare(b.created_at));
  $("waiting").replaceChildren();
  $("waiting-count").textContent = `${waiting.length} waiting`;
  for (const item of waiting) {
    const tr = node("tr");
    tr.append(itemCell(item), node("td", `${item.state}${item.timeline_unavailable ? " / timeline unavailable" : ""}`), node("td", age((built - new Date(item.created_at)) / 1000)), node("td", item.fallback_marker ? "Present" : "Absent"),
      node("td", date(item.waiting_since)), node("td", label(item.original_outcome)), node("td", date(item.observed_through)));
    $("waiting").append(tr);
  }
  if (!waiting.length) emptyTable("waiting", 7, "No waiting items observed in this cohort. Incomplete intake is not proof of none.");
  const changes = data.changes.filter(change => itemIndex.has(change.item)).sort((a, b) => b.at.localeCompare(a.at));
  $("changes").replaceChildren();
  $("changes-count").textContent = `${changes.length} events`;
  for (const change of changes) {
    const item = itemIndex.get(change.item);
    const cell = itemCell(item);
    cell.append(node("span", date(change.at), "secondary"));
    const evidence = node("td", `${change.attribution}: event ${change.event_id}`);
    evidence.append(node("span", change.actor || "Unknown actor", "secondary"));
    if (change.run_url) evidence.append(safeLink("Application run", change.run_url));
    const tr = node("tr");
    tr.append(cell, node("td", label(change.category)), node("td", change.original_label || "Not confirmed"),
      node("td", change.replacement || change.label), node("td", age(change.elapsed_seconds)), evidence);
    $("changes").append(tr);
  }
  if (!changes.length) emptyTable("changes", 6, "No relevant changes observed. See timeline coverage before interpreting this as zero.");
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
  if (!data.configurations.length) emptyTable("configuration", 6, "No configuration evidence collected yet.");
  $("permissions").replaceChildren();
  for (const [repo, permission] of Object.entries(data.permissions)) {
    for (const [endpoint, check] of Object.entries(permission.checks)) {
      $("permissions").append(node("div", `${repo} / ${endpoint}: ${check.ok ? "verified read" : check.reason} (${date(permission.checked_at)})`, "ops-row"));
    }
  }
  $("operations").replaceChildren(
    node("div", `Build: ${date(data.built_at)}. Refresh attempted: ${date(data.last_refresh_attempt)}. Requests used: ${data.requests_used ?? "unknown"}. Remaining API quota: ${data.rate_remaining ?? "unknown"}. Budget pause: ${data.budget_paused ? "yes (resumable)" : "no"}.`, "ops-row")
  );
  for (const stream of data.streams.filter(stream => stream.enabled)) {
    $("operations").append(node("div", `${stream.repo} / ${stream.kind}: ${stream.run_attempts} collected workflow run attempts (all items, independent of dashboard cohort). ${Object.entries(stream.run_evidence).map(([name, count]) => `${label(name)} ${count}`).join(", ") || "No runs observed"}. Oldest retry audit: ${date(stream.oldest_retry_check)}.`, "ops-row"));
    if (Object.keys(stream.failure_phases).length) $("operations").append(node("div", `Explicit failed steps: ${Object.entries(stream.failure_phases).map(([name, count]) => `${label(name)} ${count}`).join(", ")}. These are run-level failures, not inferred per-item outcomes.`, "ops-row"));
  }
  $("operations").append(node("p", "Run-created windows are bounded and resumable. A rotating audit detects reruns whose original creation date is outside the live window. Historical reruns can remain undiscovered until audited. Expired logs are retained as gaps and are not requested repeatedly.", "caption"));
  $("errors").replaceChildren();
  for (const error of data.errors) {
    $("errors").append(node("div", `${date(error.last_seen)} / ${error.stage}: ${error.message} (${error.count} occurrences)`, "ops-row"));
  }
  if (!data.errors.length) $("errors").append(node("p", "No collector errors recorded. Partial backfill and unknown source evidence are still reported separately.", "caption"));
  freshness();
}
fetch("data.json", {cache: "no-store"}).then(response => {
  if (!response.ok) throw new Error(`Evidence request failed (${response.status})`);
  return response.json();
}).then(value => {
  if (value.schema !== 1) throw new Error("Unsupported evidence schema");
  data = value;
  for (const id of ["repo", "kind", "cohort"]) $(id).addEventListener("change", render);
  render();
  setInterval(freshness, 60000);
}).catch(error => {
  $("freshness").className = "notice failure";
  $("freshness").textContent = `Dashboard evidence could not be loaded: ${error.message}. No metrics are available.`;
});
