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


def test_streaming_search_renders_duration(seed_live_server, page):
    """Total flight duration (#53) must render in a grid cell AND the summary card.

    The shared e2e fare stub returns duration_min=875 → '14h 35m'.
    """
    page.goto(seed_live_server)
    page.click("#loadCities")
    page.wait_for_selector(".chip")
    page.click("#run")

    # Stream complete
    page.wait_for_selector("#rec", state="visible", timeout=15000)

    # A grid cell's .stops line shows "1 stop · 14h 35m"
    page.wait_for_function(
        "() => [...document.querySelectorAll('td .stops')]"
        ".some(e => e.textContent.includes('14h 35m'))",
        timeout=15000,
    )
    stops_texts = page.eval_on_selector_all(
        "td .stops", "els => els.map(e => e.textContent)")
    assert any("14h 35m" in t and "stop" in t for t in stops_texts), \
        f"Expected 'stop · 14h 35m' in a cell, got: {stops_texts!r}"

    # The summary card meta also names the duration.
    card_meta = page.inner_text("#card-meta-0")
    assert "14h 35m" in card_meta, \
        f"Expected duration in summary card meta, got: {card_meta!r}"


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


def test_streaming_best_tiebreak_matches_backend(seed_live_server, page):
    """When every cell for a city ties on chosen_cad (the seed fixture returns a
    constant fare), the summary card + the highlighted .best cell must resolve
    to the SAME deterministic cell the backend picks: lowest chosen_cad, then
    earliest dep index, then earliest ret index. With span 2x2 all four cells
    tie, so the backend's min() over the dep-major flat grid picks the first
    cell (earliest dep x earliest ret). Cells stream in as_completed order, so a
    naive 'first to arrive' frontend could land elsewhere; assert it does not."""
    page.goto(seed_live_server)
    page.click("#loadCities")
    page.wait_for_selector(".chip")
    page.click("#run")

    # Wait for the recommendation (stream complete + highlights applied).
    page.wait_for_selector("#rec", state="visible", timeout=15000)
    page.wait_for_function(
        "() => document.querySelector('#grids td.best') !== null",
        timeout=15000,
    )

    # The deterministic best cell = first dep (2026-12-12) x first ret
    # (2027-01-04). For every city table, the single .best cell must be that key
    # and the summary card meta must name those same two dates.
    expected_key = "2026-12-12|2027-01-04"
    best_keys = page.eval_on_selector_all(
        "#grids td.best", "els => els.map(e => e.getAttribute('data-k'))")
    assert best_keys, "Expected at least one highlighted .best cell"
    assert all(k == expected_key for k in best_keys), \
        f"Tied best highlight did not match backend cell {expected_key!r}: {best_keys!r}"

    # Exactly one .best per city table (prior tie highlights must be cleared).
    per_table = page.eval_on_selector_all(
        "#grids table",
        "tbls => tbls.map(t => t.querySelectorAll('td.best').length)")
    assert all(n == 1 for n in per_table), \
        f"Each city should have exactly one .best cell, got {per_table!r}"

    # Summary card meta for the first city must reference the deterministic dates.
    card_meta = page.inner_text("#card-meta-0")
    assert "Dec" in card_meta and "12" in card_meta, \
        f"Summary card best should name the earliest dep date: {card_meta!r}"
    assert "Jan" in card_meta and "4" in card_meta, \
        f"Summary card best should name the earliest ret date: {card_meta!r}"


def test_streaming_progress_resets_on_400(seed_live_server, page):
    """A failed (400) stream search must leave #prog reset to width 0 — the
    early non-ok return previously skipped the only reset, leaving the bar
    stuck. Clearing the departure date forces an empty dep_dates -> backend
    400, exercising the !response.ok early-return path."""
    page.goto(seed_live_server)
    page.click("#loadCities")
    page.wait_for_selector(".chip")

    # The non-ok path calls alert(); auto-dismiss so the handler proceeds.
    page.on("dialog", lambda d: d.dismiss())

    # Clear the departure date -> dep_dates is empty -> backend returns 400.
    page.fill("#depStart", "")
    page.click("#run")

    # The run button is re-enabled in finally, signalling the handler returned.
    page.wait_for_function(
        "() => { const b = document.querySelector('#run'); return b && !b.disabled; }",
        timeout=15000,
    )

    # Progress bar must be reset to 0 on this failure path.
    width = page.eval_on_selector("#prog", "el => el.style.width")
    assert width in ("0%", ""), \
        f"Expected #prog reset to 0% after a 400 failure, got {width!r}"


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
