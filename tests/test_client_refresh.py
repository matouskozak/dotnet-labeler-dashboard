import json
from copy import deepcopy

import pytest
from test_browser import browser as browser
from test_browser import site as site

from labeler_dashboard.store import instant


def weekend_report(output, at, days=()):
    report = json.loads((output / "data-v2.json").read_text())
    report.update(built_at=at, data_as_of=at, last_refresh_attempt=at)
    report["items"][0].update(
        created_at="2026-11-20T08:00:00Z",
        activity=[{"date": day, "series": ["applied", "human_changed"]} for day in days],
    )
    return report


def set_report(output, value):
    (output / "data-v2.json").write_text(json.dumps(value))


def test_visible_poll_updates_weekend_dates_and_preserves_filters_details_scroll_and_focus(
    site, browser
):
    output, url, requests = site
    friday = weekend_report(output, "2026-11-20T12:00:00Z", ["2026-11-20"])
    monday = weekend_report(
        output, "2026-11-23T08:00:00Z", ["2026-11-20", "2026-11-22", "2026-11-23"]
    )
    set_report(output, friday)
    with browser.new_page() as page:
        page.clock.install(time=instant(friday["built_at"]))
        page.goto(url, wait_until="networkidle")
        page.select_option("#repo", "dotnet/machinelearning")
        page.select_option("#kind", "issue")
        page.select_option("#cohort", "7")
        page.get_by_text("Details", exact=True).click()
        page.locator("summary").focus()
        scroll = page.evaluate("window.scrollY")
        page.evaluate("""() => {
            window.renderCount = 0;
            const originalRender = render;
            render = () => { window.renderCount++; originalRender(); };
        }""")
        set_report(output, monday)
        with page.expect_request("**/data-v2.json"):
            page.clock.fast_forward(5 * 60000)
        page.wait_for_function("() => data.built_at === '2026-11-23T08:00:00Z' && !refreshInFlight")
        assert page.url == url
        assert page.locator("#repo").input_value() == "dotnet/machinelearning"
        assert page.locator("#kind").input_value() == "issue"
        assert page.locator("#cohort").input_value() == "7"
        assert page.locator("details").get_attribute("open") is not None
        assert page.evaluate("document.activeElement.tagName") == "SUMMARY"
        assert abs(page.evaluate("window.scrollY") - scroll) <= 1
        assert page.locator('#trend circle[aria-label*="2026-11-22: 1"]').count() == 2
        assert page.locator('#trend circle[aria-label*="2026-11-23: 1"]').count() == 2
        assert page.evaluate("window.renderCount") == 1
        page.select_option("#cohort", "30")
        assert page.evaluate("window.renderCount") == 2
        page.evaluate("window.metric = document.querySelector('#metrics .metric')")
        with page.expect_request("**/data-v2.json"):
            page.clock.fast_forward(5 * 60000)
        page.wait_for_function("() => !refreshInFlight")
        assert page.evaluate("window.metric === document.querySelector('#metrics .metric')")
        assert page.evaluate("window.renderCount") == 2
        assert requests.count("/data-v2.json") == 3


def test_hidden_tab_does_not_poll_and_returning_loads_new_report_without_navigation(site, browser):
    output, url, requests = site
    friday = weekend_report(output, "2026-11-20T12:00:00Z")
    monday = weekend_report(output, "2026-11-23T08:00:00Z", ["2026-11-22", "2026-11-23"])
    set_report(output, friday)
    with browser.new_page() as page:
        page.clock.install(time=instant(friday["built_at"]))
        page.goto(url, wait_until="networkidle")
        page.select_option("#repo", "dotnet/machinelearning")
        page.evaluate("""() => {
            window.tabHidden = true;
            Object.defineProperty(document, 'hidden', {get: () => window.tabHidden});
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
        page.clock.fast_forward(3 * 24 * 3600 * 1000)
        assert requests.count("/data-v2.json") == 1
        assert page.evaluate("data.built_at") == friday["built_at"]
        set_report(output, monday)
        with page.expect_request("**/data-v2.json"):
            page.evaluate(
                "window.tabHidden = false; document.dispatchEvent(new Event('visibilitychange'));"
            )
        page.wait_for_function("() => data.built_at === '2026-11-23T08:00:00Z' && !refreshInFlight")
        assert page.locator("#repo").input_value() == "dotnet/machinelearning"
        assert page.locator('#trend circle[aria-label*="2026-11-23"]').count() == 2


def test_refresh_requests_do_not_overlap_and_use_no_store(site, browser):
    output, url, _ = site
    report = weekend_report(output, "2026-11-23T08:00:00Z", ["2026-11-23"])
    held = []
    with browser.new_page() as page:
        page.clock.install(time=instant(report["built_at"]))
        page.goto(url, wait_until="networkidle")
        page.evaluate("""() => {
            const realFetch = window.fetch;
            window.fetchOptions = [];
            window.fetch = (url, options) => {
                window.fetchOptions.push({url, cache: options.cache, signal: !!options.signal});
                return realFetch(url, options);
            };
        }""")
        page.route("**/data-v2.json", lambda route: held.append(route))
        page.evaluate("void refreshData()")
        page.wait_for_function("() => window.fetchOptions.length === 1")
        page.evaluate(
            "for (let i=0; i<10; i++) { refreshData(); document.dispatchEvent(new Event('visibilitychange')); }"
        )
        assert len(held) == 1
        assert page.evaluate("window.fetchOptions") == [
            {"url": "data-v2.json", "cache": "no-store", "signal": True},
        ]
        held[0].fulfill(json=report)
        page.wait_for_function("() => data.built_at === '2026-11-23T08:00:00Z' && !refreshInFlight")
        assert page.locator("#metrics .metric").count() == 6


@pytest.mark.parametrize("failure", ["http", "json", "schema", "nested_shape", "missing_section"])
def test_background_failure_preserves_valid_dom_and_recovers_on_later_poll(site, browser, failure):
    output, url, _ = site
    previous = weekend_report(output, "2026-11-20T12:00:00Z", ["2026-11-20"])
    updated = weekend_report(output, "2026-11-23T08:00:00Z", ["2026-11-23"])
    set_report(output, previous)
    bad = deepcopy(updated)
    if failure == "schema":
        bad["schema"] = 999
    elif failure == "nested_shape":
        bad["items"][0]["intake_24"] = {}
    elif failure == "missing_section":
        bad.pop("permissions")
    with browser.new_page() as page:
        page.clock.install(time=instant(previous["built_at"]))
        page.goto(url, wait_until="networkidle")
        page.select_option("#repo", "dotnet/machinelearning")
        page.evaluate(
            "window.metric = document.querySelector('#metrics .metric'); window.chart = document.querySelector('#trend svg');"
        )
        page.route(
            "**/data-v2.json",
            lambda route: route.fulfill(
                status=503 if failure == "http" else 200,
                body="broken {" if failure == "json" else json.dumps(bad),
                content_type="application/json",
            ),
        )
        with page.expect_request("**/data-v2.json"):
            page.clock.fast_forward(5 * 60000)
        page.wait_for_function("() => refreshError !== null && !refreshInFlight")
        assert page.evaluate("data.built_at") == previous["built_at"]
        assert page.evaluate("window.metric === document.querySelector('#metrics .metric')")
        assert page.evaluate("window.chart === document.querySelector('#trend svg')")
        warning = page.locator("#freshness").inner_text()
        assert warning.startswith("Refresh failed:")
        assert "Showing data through 2026-11-20 12:00 UTC" in warning
        page.clock.fast_forward(60000)
        assert page.locator("#freshness").inner_text() == warning
        page.unroute("**/data-v2.json")
        set_report(output, updated)
        with page.expect_request("**/data-v2.json"):
            page.clock.fast_forward(5 * 60000)
        page.wait_for_function("() => data.built_at === '2026-11-23T08:00:00Z' && !refreshInFlight")
        assert page.locator("#repo").input_value() == "dotnet/machinelearning"
        assert "Refresh failed" not in page.locator("#freshness").inner_text()
        assert page.locator('#trend circle[aria-label*="2026-11-23"]').count() == 2


@pytest.mark.parametrize("regression", ["build", "coverage"])
def test_older_report_cannot_roll_back_loaded_snapshot(site, browser, regression):
    output, url, _ = site
    latest = weekend_report(output, "2026-11-23T08:00:00Z", ["2026-11-22", "2026-11-23"])
    old = deepcopy(latest)
    old["built_at"] = "2026-11-20T12:00:00Z" if regression == "build" else "2026-11-23T09:00:00Z"
    old["data_as_of"] = "2026-11-20T12:00:00Z"
    set_report(output, latest)
    with browser.new_page() as page:
        page.goto(url, wait_until="networkidle")
        set_report(output, old)
        page.evaluate("refreshData()")
        assert page.evaluate("data.built_at") == latest["built_at"]
        assert page.evaluate("data.data_as_of") == latest["data_as_of"]
        assert "older snapshot" in page.locator("#freshness").inner_text()


def test_refresh_timeout_keeps_old_report_and_allows_later_retry(site, browser):
    output, url, _ = site
    updated = weekend_report(output, "2026-11-23T08:00:00Z", ["2026-11-23"])
    held = []
    with browser.new_page() as page:
        page.clock.install()
        page.goto(url, wait_until="networkidle")
        old = page.evaluate("data.built_at")
        page.route("**/data-v2.json", lambda route: held.append(route))
        with page.expect_request("**/data-v2.json"):
            page.evaluate("void refreshData()")
        page.clock.fast_forward(20001)
        page.wait_for_function("() => !refreshInFlight")
        assert page.evaluate("data.built_at") == old
        assert "Request timed out" in page.locator("#freshness").inner_text()
        page.unroute("**/data-v2.json")
        set_report(output, updated)
        page.evaluate("refreshData()")
        assert page.evaluate("data.built_at") == updated["built_at"]
        assert "Refresh failed" not in page.locator("#freshness").inner_text()


def test_new_report_without_selected_repo_does_not_reset_selection(site, browser):
    output, url, _ = site
    with browser.new_page() as page:
        page.goto(url, wait_until="networkidle")
        page.select_option("#repo", "dotnet/machinelearning")
        page.select_option("#kind", "issue")
        updated = weekend_report(output, "2026-11-23T08:00:00Z")
        updated.update(items=[], changes=[], streams=[])
        set_report(output, updated)
        page.evaluate("refreshData()")
        assert page.locator("#repo").input_value() == "dotnet/machinelearning"
        assert page.locator("#kind").input_value() == "issue"
        assert page.locator("#cohort-note").inner_text() == "0 items"
        assert "Refresh failed" not in page.locator("#freshness").inner_text()


def test_render_exception_cannot_leave_mixed_report_sections(site, browser):
    output, url, _ = site
    with browser.new_page() as page:
        page.goto(url, wait_until="networkidle")
        previous = page.evaluate("data.built_at")
        page.evaluate("""() => {
            window.metric = document.querySelector('#metrics .metric');
            window.chart = document.querySelector('#trend svg');
            render = () => {
                $('metrics').replaceChildren(node('p', 'Incomplete render'));
                throw new Error('Render failed');
            };
        }""")
        set_report(output, weekend_report(output, "2026-11-23T08:00:00Z"))
        page.evaluate("refreshData()")
        assert page.evaluate("data.built_at") == previous
        assert page.evaluate("window.metric === document.querySelector('#metrics .metric')")
        assert page.evaluate("window.chart === document.querySelector('#trend svg')")
        assert "Incomplete render" not in page.locator("#metrics").inner_text()
        assert "Render failed" in page.locator("#freshness").inner_text()
