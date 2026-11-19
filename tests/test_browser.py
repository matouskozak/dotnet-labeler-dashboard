import hashlib
import json
from copy import deepcopy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from playwright.sync_api import sync_playwright
from test_metrics import CONFIG, fixture

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
        assert "(partial)" in page.locator("#trend-values").text_content()
        assert page.locator("#trend svg").get_attribute("role") == "img"
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
