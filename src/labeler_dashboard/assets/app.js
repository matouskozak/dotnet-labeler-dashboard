"use strict";
let data;
const $ = id => document.getElementById(id);
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
const age = seconds => {
  if (seconds == null) return "Unknown";
  if (seconds < 60) return `${Math.max(0, Math.floor(seconds))}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
};
function safeLink(text, url) {
  if (!/^https:\/\/github\.com\/dotnet\/(?:runtime|machinelearning|roslyn)\/(?:issues|pull|actions\/runs)\/\d+$/.test(url || "")) return node("span", text);
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
    rows.push({date: day, counts: counts.get(day) || {}, complete: streams.every(stream =>
      (stream.daily_coverage || []).some(row => row.date === day && row.complete))});
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
      const visible = row.complete || count > 0;
      tr.append(node("td", row.complete ? count : count > 0 ? `${count} (partial)` : "Unknown"));
      if (!visible) continue;
      const previous = rows[index - 1];
      if (previous && (previous.complete || previous.counts[name] > 0)) {
        svg.append(svgNode("line", {x1: x(index - 1), y1: y(previous.counts[name] || 0), x2: x(index), y2: y(count),
          stroke: color, "stroke-width": 2, "stroke-dasharray": row.complete && previous.complete ? "none" : "5 4"}));
      }
      const description = `${row.date}: ${count} ${text.toLowerCase()}${row.complete ? "" : " (partial)"}`;
      const point = svgNode("circle", {cx: x(index), cy: y(count), r: 4, fill: row.complete ? color : "white",
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
function freshness() {
  const banner = $("freshness");
  const elapsed = data.data_as_of ? (Date.now() - new Date(data.data_as_of)) / 1000 : null;
  const checks = Object.values(data.permissions).flatMap(repo => Object.values(repo.checks));
  const denied = checks.some(check => !check.ok && check.state !== "unverified");
  const unverified = checks.some(check => check.state === "unverified");
  const stale = elapsed == null || elapsed > 3 * 3600;
  banner.className = "notice" + (denied ? " failure" : stale || unverified ? " warning" : "");
  const partial = data.streams.some(stream => stream.enabled &&
    (!stream.intake_complete || stream.pending_timelines || stream.backfill_to < stream.live_to ||
      stream.last_error || incompleteProduction(stream)));
  banner.textContent = denied
    ? "Source access failed. Results are incomplete. See Details."
    : unverified
      ? "Some source access checks are unverified. See Details."
    : stale
      ? `Data is stale. Last discovery refresh: ${date(data.data_as_of)}.`
      : `Updated ${date(data.data_as_of)}.${partial ? " History is incomplete." : ""}`;
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
    card.append(node("h3", `${stream.repo.replace("dotnet/", "")} / ${stream.kind === "pr" ? "Pull requests" : "Issues"}`));
    const complete = stream.enabled && stream.intake_complete && stream.timelines === stream.items &&
      !stream.pending_timelines && stream.backfill_to >= stream.live_to && !stream.last_error &&
      !incompleteProduction(stream);
    card.append(node("span", !stream.enabled ? "NOT ENABLED" : complete ? "COLLECTED" : "PARTIAL", `badge ${!stream.enabled ? "disabled" : complete ? "" : "pending"}`));
    if (!stream.enabled) {
      card.append(node("p", "Not monitored."));
    } else {
      const cohortItems = items.filter(item => item.repo === stream.repo && item.kind === stream.kind);
      card.append(node("p", `${cohortItems.length} selected / ${stream.items} collected items`),
        node("p", `${stream.timelines} timelines / ${stream.pending_timelines} pending`));
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
  const changes = data.changes.filter(change => itemIndex.has(change.item)).sort((a, b) => b.at.localeCompare(a.at));
  $("changes").replaceChildren();
  $("changes-count").textContent = `${changes.length} changes`;
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
fetch("data-v2.json", {cache: "no-store"}).then(response => {
  if (!response.ok) throw new Error(`Data request failed (${response.status})`);
  return response.json();
}).then(value => {
  if (![1, 2].includes(value.schema)) throw new Error("Unsupported data format");
  data = value;
  for (const id of ["repo", "kind", "cohort"]) $(id).addEventListener("change", render);
  render();
  setInterval(freshness, 60000);
}).catch(error => {
  $("freshness").className = "notice failure";
  $("freshness").textContent = `Could not load data: ${error.message}.`;
});
