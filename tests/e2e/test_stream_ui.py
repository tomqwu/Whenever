"""E2E tests for /api/search/stream: drive the browser through a live-server
streaming search and assert progressive rendering produces the correct final state."""

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
    page.wait_for_selector("table")

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

    # Assert bar goes non-zero DURING the search
    page.wait_for_function(
        "() => { const el = document.querySelector('#prog'); "
        "return el && el.style.width && el.style.width !== '0%'; }",
        timeout=15000,
    )

    # Wait for stream to complete
    page.wait_for_selector("#rec", state="visible", timeout=15000)

    # After done + 600 ms timeout, progress bar should be ~0% (reset)
    page.wait_for_timeout(800)
    width = page.eval_on_selector("#prog", "el => el.style.width")
    assert width in ("0%", ""), f"Expected progress bar reset to 0%, got {width!r}"


def test_streaming_no_fare_card_finalized(nofare_live_server, page):
    """A city with no fares for every cell must finalize to '— / no fares / —'
    (never left on the '…' placeholder) once the stream completes."""
    page.goto(nofare_live_server)
    page.click("#loadCities")
    page.wait_for_selector(".chip")
    page.click("#run")

    # Wait for stream completion (recommendation visible).
    page.wait_for_selector("#rec", state="visible", timeout=15000)

    # Beijing (PEK) is the 2nd destination -> card index 1, all cells no-data.
    # Its card must show '—' price and 'no fares' meta (not the '…' skeleton).
    page.wait_for_function(
        "() => { const el = document.getElementById('card-price-1'); "
        "return el && el.textContent === '\\u2014'; }",
        timeout=15000,
    )
    assert page.inner_text("#card-price-1") == "—"
    assert page.inner_text("#card-meta-1") == "no fares"
    assert page.inner_text("#card-group-1") == "—"

    # No grid cell anywhere may still show the '…' loading placeholder.
    loading_left = page.eval_on_selector_all(
        "#grids td.loading", "els => els.length")
    assert loading_left == 0, f"{loading_left} cells still loading after done"

    # The no-fare city's cells must render the n/a (err) style.
    pek_na = page.eval_on_selector_all(
        "#blk-1 td.err", "els => els.length")
    assert pek_na > 0, "Expected Beijing cells to render as n/a"

    # Footer provenance must be restored (no flight API configured in tests).
    foot = page.inner_text("#foot")
    assert "Per family of" in foot, f"Footer not restored: {foot!r}"
    assert "no fares" in foot, f"Footer missing no-fares warning: {foot!r}"
