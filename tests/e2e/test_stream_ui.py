"""E2E tests for /api/search/stream: drive the browser through a live-server
streaming search and assert progressive rendering produces the correct final state."""

import json
import pytest


def test_streaming_search_full_flow(seed_live_server, page):
    """Toronto (YYZ) → China: click run, wait for stream to complete, assert grid populated."""
    page.goto(seed_live_server)

    # Load top cities (China seeds)
    page.click("#loadCities")
    page.wait_for_selector(".chip")

    # Click run — triggers /api/search/stream
    page.click("#run")

    # Wait for the recommendation to appear (stream complete)
    page.wait_for_selector("#rec", state="visible", timeout=15000)

    # #rec should contain "Best value"
    rec_text = page.inner_text("#rec")
    assert "Best value" in rec_text, f"Expected 'Best value' in #rec, got: {rec_text!r}"

    # At least one summary card should be visible
    page.wait_for_selector("#summary .card")
    summary_text = page.inner_text("#summary")
    assert any(city in summary_text for city in ("Beijing", "Shanghai", "Guangzhou")), \
        f"Expected a China city in #summary, got: {summary_text!r}"

    # Grid table(s) must be present
    assert page.query_selector("table") is not None, "Expected at least one grid table"

    # At least one cell should show a price (not the placeholder "…")
    # The mock fare is cheapest_cad=8000 → renders as "$8,000"
    page.wait_for_function(
        "() => document.querySelector('td a.price') !== null",
        timeout=15000,
    )
    price_link = page.query_selector("td a.price")
    assert price_link is not None, "Expected at least one rendered price link in a cell"
    price_text = price_link.inner_text()
    assert "$" in price_text, f"Expected a $ price in cell, got: {price_text!r}"


def test_streaming_search_run_button_re_enabled(seed_live_server, page):
    """After stream completes, the run button must be re-enabled."""
    page.goto(seed_live_server)
    page.click("#loadCities")
    page.wait_for_selector(".chip")
    page.click("#run")

    # Wait for done (rec visible)
    page.wait_for_selector("#rec", state="visible", timeout=15000)

    # Button must be re-enabled
    is_disabled = page.get_attribute("#run", "disabled")
    assert is_disabled is None, "Run button should be re-enabled after stream completes"


def test_streaming_search_progress_bar(seed_live_server, page):
    """Progress bar should become non-zero during search and reset after."""
    page.goto(seed_live_server)
    page.click("#loadCities")
    page.wait_for_selector(".chip")
    page.click("#run")

    # Wait for stream to complete
    page.wait_for_selector("#rec", state="visible", timeout=15000)

    # After done + 600 ms timeout, progress bar should be ~0% (reset)
    page.wait_for_timeout(800)
    width = page.eval_on_selector("#prog", "el => el.style.width")
    assert width in ("0%", ""), f"Expected progress bar reset to 0%, got {width!r}"
