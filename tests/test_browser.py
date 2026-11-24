import hashlib
import json
from copy import deepcopy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from playwright.sync_api import sync_playwright
from test_metrics import CONFIG, fixture
from test_roslyn import NOW as ROSLYN_NOW
from test_roslyn import roslyn_fixture
from test_sources import NOW as SOURCES_NOW
from test_sources import source_fixture

from labeler_dashboard.render import render
from labeler_dashboard.store import instant


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def site(tmp_path):
    requests = []

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            super().do_GET()

        def end_headers(self):
            self.send_header("Cache-Control", "max-age=600")
            super().end_headers()

    render(fixture(), CONFIG, instant("2026-11-19T12:00:00Z"), tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=tmp_path))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield tmp_path, f"http://127.0.0.1:{server.server_port}/", requests
    server.shutdown()
    server.server_close()
    thread.join()


def test_asset_versions_and_public_alias_stay_stable_across_data_refreshes(site):
    output, _, _ = site
    html = (output / "index.html").read_text()
    for name in ("app.js", "style.css"):
        digest = hashlib.sha256((output / name).read_bytes()).hexdigest()[:16]
        assert f'"{name}?v={digest}"' in html
    render(fixture(), CONFIG, instant("2026-11-19T13:00:00Z"), output)
    assert (output / "index.html").read_text() == html
    assert (output / "data.json").read_bytes() == (output / "data-v2.json").read_bytes()
    assert json.loads((output / "data.json").read_text())["schema"] == 2


def test_warm_schema1_client_reproduces_failure_then_new_entry_bypasses_cached_script(
    site, browser
):
    output, url, requests = site
    old_html = '<div id="freshness"></div><script src="app.js"></script>'
    old_script = """
fetch("data.json", {cache: "no-store"}).then(r => r.json()).then(data => {
  if (data.schema !== 1) throw new Error("Unsupported evidence schema");
  document.getElementById("freshness").textContent = "Legacy loaded";
}).catch(e => document.getElementById("freshness").textContent = e.message);
"""
    (output / "index.html").write_text(old_html)
    (output / "app.js").write_text(old_script)
    (output / "data.json").write_text('{"schema":1}')
    with browser.new_context() as context:
        page = context.new_page()
        page.goto(url)
        page.get_by_text("Legacy loaded", exact=True).wait_for()
        render(fixture(), CONFIG, instant("2026-11-19T12:00:00Z"), output)
        (output / "index.html").write_text(old_html)
        page.goto(url + "?old-page=2")
        page.get_by_text("Unsupported evidence schema", exact=True).wait_for()
        assert requests.count("/app.js") == 1  # The second navigation used the browser cache.
        render(fixture(), CONFIG, instant("2026-11-19T12:00:00Z"), output)
        page.goto(url + "?v=current", wait_until="networkidle")
        assert page.title() == "Issue labeler dashboard"
        assert "Could not load" not in page.locator("#freshness").inner_text()
        assert page.locator("#metrics .metric").count() == 6
        assert any(path.startswith("/app.js?v=") for path in requests)
        assert "/data-v2.json" in requests
        assert page.locator("#changes tr td").count() == 5


def test_cached_v2_consumer_keeps_its_schema_and_fields_after_additive_update(site, browser):
    output, url, _ = site
    (output / "legacy-v2.html").write_text("""
<div id="result"></div><script src="legacy-v2.js"></script>
""")
    (output / "legacy-v2.js").write_text("""
fetch("data.json", {cache: "no-store"}).then(r => r.json()).then(data => {
  if (data.schema !== 2) throw new Error("Unsupported evidence schema");
  document.getElementById("result").textContent =
    data.changes[0].before.join(", ") + " -> " + data.changes[0].after.join(", ");
});
""")
    with browser.new_context() as context:
        page = context.new_page()
        for revision in (1, 2):
            render(fixture(), CONFIG, instant(f"2026-11-19T1{revision}:00:00Z"), output)
            page.goto(url + f"legacy-v2.html?v={revision}")
            page.get_by_text("area-Infrastructure -> area-Trainers", exact=True).wait_for()


def test_new_client_reads_legacy_rows_without_inventing_full_label_states(site, browser):
    output, url, _ = site
    payload = json.loads((output / "data-v2.json").read_text())
    payload["schema"] = 1
    payload["changes"] = [
        {
            "item": payload["items"][0]["key"],
            "at": "2026-11-11T14:03:01Z",
            "category": "human_area_added",
            "label": "area-Trainers",
            "actor": "human",
            "original_label": None,
            "replacement": None,
        }
    ]
    payload["items"][0].pop("activity")
    (output / "data-v2.json").write_text(json.dumps(payload))
    with browser.new_page() as page:
        page.goto(url, wait_until="networkidle")
        assert page.locator("#changes-count").inner_text() == "0 edits"
        page.select_option("#change-scope", "all")
        cells = page.locator("#changes tr td")
        assert cells.nth(1).inner_text() == "Unknown"
        assert cells.nth(2).inner_text() == "Added area-Trainers"
        assert "No daily data" in page.locator("#trend").inner_text()
        assert page.locator("#metrics .metric").count() == 6
        assert "Could not load" not in page.locator("#freshness").inner_text()


def test_daily_plot_filters_deduplicates_and_distinguishes_partial_days(site, browser):
    output, url, _ = site
    payload = json.loads((output / "data-v2.json").read_text())
    item = deepcopy(payload["items"][0])
    item.update(
        key="runtime:1",
        number=1,
        repo="dotnet/runtime",
        kind="pr",
        created_at="2026-11-18T00:00:00Z",
        activity=[{"date": "2026-11-18", "series": ["applied", "applied", "human_changed"]}],
    )
    payload["items"].append(item)
    payload["items"].append(
        {
            **item,
            "key": "runtime:2",
            "number": 2,
            "kind": "issue",
            "created_at": "2026-10-25T00:00:00Z",
        }
    )
    for kind in ("pr", "issue"):
        payload["streams"].append(
            {
                **payload["streams"][0],
                "repo": "dotnet/runtime",
                "kind": kind,
                "daily_coverage": [
                    {"date": "2026-11-16", "complete": True},
                    {"date": "2026-11-18", "complete": False},
                ],
            }
        )
    (output / "data-v2.json").write_text(json.dumps(payload))
    with browser.new_page() as page:
        page.goto(url, wait_until="networkidle")
        page.select_option("#repo", "dotnet/runtime")
        values = page.evaluate("dailyRows(data.items.filter(selected))")
        assert next(row for row in values if row["date"] == "2026-11-18")["counts"]["applied"] == 2
        page.select_option("#kind", "pr")
        page.select_option("#cohort", "7")
        values = page.evaluate("dailyRows(data.items.filter(selected))")
        assert len(values) == 7
        assert not page.evaluate("selected({...data.items[1], created_at: '2026-11-12T23:59:59Z'})")
        assert page.evaluate("selected({...data.items[1], created_at: '2026-11-13T00:00:00Z'})")
        last = next(row for row in values if row["date"] == "2026-11-18")
        assert last["counts"] == {"applied": 1, "human_changed": 1}
        assert not last["complete"]
        assert page.locator('#trend circle[aria-label*="2026-11-16: 0"]').count() == 3
        assert page.locator('#trend circle[aria-label*="2026-11-17"]').count() == 0
        assert page.locator('#trend circle[aria-label*="2026-11-18: 1"]').count() == 2
        assert "(partial:" in page.locator("#trend-values").text_content()
        assert page.locator("#trend svg").get_attribute("role") == "img"
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("production_gap", [False, True])
def test_manual_and_dry_run_unknowns_only_affect_operational_counts(site, browser, production_gap):
    output, url, _ = site
    state = fixture()
    now = instant("2026-11-19T12:00:00Z")
    state["data_as_of"] = "2026-11-19T12:00:00Z"
    state["repositories"]["dotnet/machinelearning"] = {
        "intake_complete": True,
        "updated_checkpoint": "2026-11-19T12:00:00Z",
    }
    for key in ("manual_cancelled", "manual_unsupported", "dry_run"):
        state["runs"][key] = {
            **state["runs"]["run1"],
            "manual": key != "dry_run",
            "evidence": "unknown",
            "conclusion": "cancelled" if key == "manual_cancelled" else "success",
            "branch": "main",
            "configs": [
                {
                    "dry_run": key == "dry_run",
                    "action_version": None,
                    "threshold": None,
                    "action_sha": None,
                    "cache_hit": None,
                }
            ],
        }
    if production_gap:
        state["runs"]["unresolved_auto"] = {
            **state["runs"]["run1"],
            "evidence": "unknown",
        }
    render(state, CONFIG, now, output)
    payload = json.loads((output / "data-v2.json").read_text())
    assert payload["streams"][0]["production_incomplete_runs"] == int(production_gap)
    assert payload["streams"][0]["run_evidence"]["unknown"] == 3 + int(production_gap)
    with browser.new_page() as page:
        page.clock.install(time=now)
        page.goto(url, wait_until="networkidle")
        assert page.locator("#streams .badge").first.inner_text() == "CAUGHT UP"
        assert "Collection caught up" in page.locator("#freshness").inner_text()
        assert (
            "Prediction results missing for 1 run" in page.locator("#streams").inner_text()
        ) is production_gap
        page.get_by_text("Details", exact=True).click()
        assert (
            f"{4 + int(production_gap)} collected workflow run attempts"
            in page.locator("#operations").inner_text()
        )
        assert f"unknown {3 + int(production_gap)}" in page.locator("#operations").inner_text()


def test_unverified_preflight_is_not_displayed_as_permission_denial(site, browser):
    output, url, _ = site
    payload = json.loads((output / "data-v2.json").read_text())
    payload["permissions"] = {
        "dotnet/runtime": {
            "checked_at": payload["built_at"],
            "checks": {
                "logs": {
                    "ok": False,
                    "state": "unverified",
                    "reason": "No readable log sample found",
                }
            },
        }
    }
    (output / "data-v2.json").write_text(json.dumps(payload))
    with browser.new_page() as page:
        page.goto(url, wait_until="networkidle")
        assert "unverified" in page.locator("#freshness").inner_text()
        assert "failed" not in page.locator("#freshness").inner_text()
        assert page.locator("#freshness").get_attribute("class") == "notice warning"


def test_roslyn_filter_links_no_selection_summary_and_daily_plot(site, browser):
    output, url, _ = site
    state, config = roslyn_fixture()
    render(state, config, ROSLYN_NOW, output)
    with browser.new_page() as page:
        page.goto(url, wait_until="networkidle")
        page.select_option("#repo", "dotnet/roslyn")
        assert page.locator("#cohort-note").inner_text() == "2 items"
        cards = page.locator("#streams .stream").filter(has_text="roslyn")
        assert cards.count() == 2
        assert "NOT ENABLED" not in cards.all_text_contents()
        metric = page.locator("#metrics .metric").filter(has_text="No label chosen")
        assert metric.locator(".value").inner_text() == "1"
        waiting = page.locator("#waiting tr").filter(has_text="#86548")
        assert waiting.count() == 1
        assert (
            waiting.locator("a").get_attribute("href")
            == "https://github.com/dotnet/roslyn/issues/86548"
        )
        assert "Absent" in waiting.inner_text()
        change = page.locator("#changes tr").filter(has_text="#86547")
        assert change.locator("td").nth(1).inner_text() == "Area-Compilers"
        assert change.locator("td").nth(2).inner_text() == "Area-IDE"
        day = next(
            row
            for row in page.evaluate("dailyRows(data.items.filter(selected))")
            if row["date"] == "2026-11-19"
        )
        assert day["counts"] == {"applied": 1, "no_selection": 1}
        assert (
            page.evaluate("safeLink('PR','https://github.com/dotnet/roslyn/pull/86557').tagName")
            == "A"
        )
        for address in (
            "https://github.com/dotnet/roslyn-other/issues/1",
            "https://github.com/other/roslyn/issues/1",
            "https://github.com.evil.example/dotnet/roslyn/issues/1",
            "javascript:alert(1)",
        ):
            assert page.evaluate("url => safeLink('untrusted',url).tagName", address) == "SPAN"
        page.select_option("#kind", "pr")
        assert page.locator("#cohort-note").inner_text() == "0 items"
        assert page.locator('#changes a[href$="/86557"]').count() == 0


def test_collected_history_with_prediction_gaps_has_independent_chart_lines_and_status(
    site, browser
):
    output, url, _ = site
    state = fixture()
    now = instant("2026-11-19T12:00:00Z")
    state["data_as_of"] = "2026-11-19T12:00:00Z"
    state["repositories"]["dotnet/machinelearning"] = {
        "intake_complete": True,
        "updated_checkpoint": state["data_as_of"],
    }
    for key, evidence, status, conclusion in (
        ("unknown", "unknown", "completed", "success"),
        ("queued", "pending", "queued", None),
        ("failed", "technical_failure", "completed", "failure"),
        ("blocked", "unknown", "completed", "action_required"),
    ):
        state["runs"][key] = {
            **state["runs"]["run1"],
            "evidence": evidence,
            "status": status,
            "conclusion": conclusion,
            "collected": status == "completed",
        }
    state["runs"]["unknown"]["configs"] = [
        {
            "threshold": None,
            "action_sha": None,
            "action_version": "v2.3.0",
            "cache_hit": None,
            "dry_run": False,
        }
    ]
    state["errors"]["historic"] = {
        "stage": "collector",
        "message": "Previous request failed",
        "count": 1,
        "first_seen": "2026-11-11T00:00:00Z",
        "last_seen": "2026-11-11T00:00:00Z",
    }
    render(state, CONFIG, now, output)
    payload = json.loads((output / "data-v2.json").read_text())
    assert payload["streams"][0]["prediction_results"] == {
        "missing": 1,
        "pending": 1,
        "failed_or_blocked": 2,
    }
    with browser.new_page() as page:
        page.clock.install(time=now)
        page.goto(url, wait_until="networkidle")
        assert page.locator("#streams .badge").first.inner_text() == "CAUGHT UP"
        card = page.locator("#streams .stream").first.inner_text()
        assert "Prediction results missing for 1 run" in card
        assert "1 run pending" in card
        assert "2 failed or blocked runs" in card
        assert page.locator("#freshness").inner_text() == (
            "Updated 2026-11-19 12:00 UTC. Collection caught up. Recorded errors in Details."
        )
        values = page.evaluate("dailyRows(data.items.filter(selected))")
        day = next(row for row in values if row["date"] == "2026-11-11")
        assert not day["complete"]
        assert day["series_complete"] == {
            "applied": False,
            "no_selection": False,
            "human_changed": True,
        }
        human = page.locator('#trend circle[aria-label="2026-11-11: 1 human label changes"]')
        assert human.count() == 1 and human.get_attribute("fill") != "white"
        applied = page.locator(
            '#trend circle[aria-label="2026-11-11: 1 labels applied (partial: prediction results missing)"]'
        )
        assert applied.count() == 1 and applied.get_attribute("fill") == "white"
        assert (
            page.locator('#trend circle[aria-label*="2026-11-11: 0 no label chosen"]').count() == 0
        )
        assert (
            page.locator('#trend circle[aria-label="2026-11-12: 0 human label changes"]').count()
            == 1
        )
        page.select_option("#kind", "pr")
        assert "No daily data" in page.locator("#trend").inner_text()
        page.select_option("#kind", "issue")
        assert human.count() == 1
        page.get_by_text("Details", exact=True).click()
        assert "Previous request failed" in page.locator("#errors").inner_text()
        page.clock.fast_forward(4 * 3600 * 1000)
        assert (
            "Data is stale. Last discovery refresh: 2026-11-19 12:00 UTC."
            == page.locator("#freshness").inner_text()
        )


@pytest.mark.parametrize("unfinished", ["backfill", "parser", "timeline", "fetch_error"])
def test_unfinished_collection_does_not_get_caught_up_badge(site, browser, unfinished):
    output, url, _ = site
    state = fixture()
    now = instant("2026-11-19T12:00:00Z")
    state["data_as_of"] = "2026-11-19T12:00:00Z"
    state["repositories"]["dotnet/machinelearning"] = {
        "intake_complete": True,
        "updated_checkpoint": state["data_as_of"],
    }
    if unfinished == "backfill":
        state["streams"]["dotnet/machinelearning:issue"]["backfill_to"] = "2026-11-15T00:00:00Z"
    elif unfinished == "parser":
        state["runs"]["run1"]["parser_version"] = 2
    elif unfinished == "timeline":
        next(iter(state["items"].values()))["reconcile_requested"] = True
    else:
        state["runs"]["run1"].update(collected=False, evidence="unavailable", status="completed")
    render(state, CONFIG, now, output)
    with browser.new_page() as page:
        page.clock.install(time=now)
        page.goto(url, wait_until="networkidle")
        assert page.locator("#streams .badge").first.inner_text() == "COLLECTING"
        assert "Collection is incomplete" in page.locator("#freshness").inner_text()


def test_new_source_filters_preserve_owner_names_prefixes_and_exact_safe_links(site, browser):
    output, url, _ = site
    state, settings = source_fixture()
    render(state, settings, SOURCES_NOW, output)
    with browser.new_page() as page:
        page.goto(url, wait_until="networkidle")
        page.select_option("#repo", "gitextensions/gitextensions")
        assert page.locator("#cohort-note").inner_text() == "2 items"
        cards = page.locator("#streams .stream").filter(has_text="gitextensions/gitextensions")
        assert cards.count() == 2
        assert "PR bases: main, release/** only" in cards.last.inner_text()
        change = page.locator("#changes tr").filter(has_text="#13466")
        assert change.locator("td").nth(1).inner_text() == "area: worktree"
        assert change.locator("td").nth(2).inner_text() == "area: merge"
        assert (
            change.locator("a").get_attribute("href")
            == "https://github.com/gitextensions/gitextensions/issues/13466"
        )
        assert page.locator("#waiting tr").filter(has_text="#13468").count() == 1
        day = next(
            row
            for row in page.evaluate("dailyRows(data.items.filter(selected))")
            if row["date"] == "2026-11-20"
        )
        assert day["counts"] == {"applied": 1, "human_changed": 1, "no_selection": 1}
        page.select_option("#repo", "dotnet/extensions")
        page.select_option("#kind", "pr")
        assert page.locator("#cohort-note").inner_text() == "1 items"
        assert (
            page.locator("#metrics .metric")
            .filter(has_text="Confirmed labels")
            .locator(".value")
            .inner_text()
            == "1"
        )
        for address in (
            "https://github.com/dotnet/extensions/pull/7972",
            "https://github.com/gitextensions/gitextensions/pull/13467",
            "https://github.com/microsoft/aspire/issues/22511",
            "https://github.com/microsoft/aspire/pull/900001",
        ):
            assert page.evaluate("url => safeLink('source',url).tagName", address) == "A"
        for address in (
            "https://github.com/dotnet/gitextensions/issues/1",
            "https://github.com/gitextensions/extensions/issues/1",
            "https://github.com/other/gitextensions/issues/1",
            "https://github.com/dotnet/aspire/issues/22511",
            "https://github.com/microsoft/extensions/issues/7971",
            "https://github.com/other/aspire/issues/22511",
            "https://github.com.evil.example/gitextensions/gitextensions/issues/1",
            "https://github.com/gitextensions/gitextensions/issues/1?next=evil",
        ):
            assert page.evaluate("url => safeLink('untrusted',url).tagName", address) == "SPAN"
        page.select_option("#repo", "microsoft/aspire")
        page.select_option("#kind", "issue")
        assert page.locator("#cohort-note").inner_text() == "1 items"
        waiting = page.locator("#waiting tr").filter(has_text="#22511")
        assert (
            waiting.locator("a").get_attribute("href")
            == "https://github.com/microsoft/aspire/issues/22511"
        )
        assert (
            page.locator("#metrics .metric")
            .filter(has_text="No label chosen")
            .locator(".value")
            .inner_text()
            == "1"
        )
        cards = page.locator("#streams .stream").filter(has_text="microsoft/aspire")
        assert cards.count() == 2
        assert "PR bases: main, release/** only" in cards.last.inner_text()
        page.select_option("#kind", "pr")
        assert page.locator("#cohort-note").inner_text() == "1 items"
        assert (
            page.locator("#metrics .metric")
            .filter(has_text="Confirmed labels")
            .locator(".value")
            .inner_text()
            == "1"
        )
