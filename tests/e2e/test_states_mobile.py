"""E2E tests for #39 (friendly empty/error states) and #40 (mobile-responsive layout).

These drive the browser through live-server scenarios and assert that:
- no-data cells read as a calm "no result" ("—" with a title), not an alarming "n/a";
- a whole-search no-data state shows a friendly empty-state message;
- a no-provider state shows a prominent "configure a key" banner;
- at a mobile viewport the page has no horizontal overflow, the matrix lives in a
  horizontal-scroll wrapper, and the form inputs are usable/stacked.
"""

import pytest

from tests.e2e.conftest import select_some_chips as _select_chips


# --------------------------- #39 empty/error states ---------------------------

def test_no_provider_banner_shows_configure_key(live_server, page):
    """With NO flight provider configured the page must surface a prominent,
    friendly banner explaining no provider is set and how to add one (env keys),
    rather than only an empty/quiet status line."""
    page.goto(live_server)
    # The banner appears on load (health() reports no providers in the test env).
    page.wait_for_selector("#noProviderBanner", state="visible", timeout=10000)
    banner = page.inner_text("#noProviderBanner")
    assert "TRAVELPAYOUTS_TOKEN" in banner, f"Banner should name an env key, got: {banner!r}"
    assert "Amadeus" in banner, f"Banner should mention Amadeus keys, got: {banner!r}"
    # Friendly, not a raw error dump.
    assert "no flight" in banner.lower() or "no provider" in banner.lower(), \
        f"Banner should explain no provider is set, got: {banner!r}"


def test_no_data_cells_are_calm_not_alarming(nofare_live_server, page):
    """A no-fare cell should render a calm '—' with an explanatory title
    ('no fare found...'), not a bare red 'n/a' that reads like an error."""
    page.goto(nofare_live_server)
    page.click("#loadCities")
    _select_chips(page)
    page.click("#run")
    page.wait_for_selector("#rec", state="visible", timeout=15000)

    # Beijing (PEK) is dest index 1, all cells no-data.
    page.wait_for_function(
        "() => { const t = document.getElementById('tbl-1'); "
        "return t && [...t.querySelectorAll('td.nodata')].length > 0; }",
        timeout=15000,
    )
    # The no-data cells use the calm '—' glyph and a helpful title.
    cell = page.query_selector("#tbl-1 td.nodata")
    assert cell is not None, "Expected a .nodata cell"
    assert cell.inner_text().strip() == "—", "No-data cell should show an em dash"
    title = cell.get_attribute("title") or ""
    assert "no fare" in title.lower(), f"No-data cell needs an explanatory title, got: {title!r}"
    # It must NOT carry the alarming red 'err' class or literal 'n/a' text.
    err_cells = page.eval_on_selector_all("#grids td.err", "els => els.length")
    assert err_cells == 0, "No-data cells must not use the alarming .err style"


def test_whole_city_no_data_shows_friendly_empty_state(nofare_live_server, page):
    """When every cell for a city is no-data, that city must show a friendly
    empty-state message (not just a silent grid of dashes)."""
    page.goto(nofare_live_server)
    page.click("#loadCities")
    _select_chips(page)
    page.click("#run")
    page.wait_for_selector("#rec", state="visible", timeout=15000)

    # Beijing (PEK) is dest index 1 — its block must include the empty-state note.
    page.wait_for_function(
        "() => { const b = document.getElementById('blk-1'); "
        "return b && b.querySelector('.empty-state') !== null; }",
        timeout=15000,
    )
    note = page.inner_text("#blk-1 .empty-state")
    assert "No fares found" in note, f"Expected friendly empty-state, got: {note!r}"
    # The friendly note guides the user toward a fix.
    assert "different dates" in note.lower() or "provider" in note.lower(), \
        f"Empty-state should suggest a remedy, got: {note!r}"


# --------------------------- #40 mobile-responsive ----------------------------

MOBILE = {"width": 390, "height": 844}


def test_mobile_no_horizontal_overflow(seed_live_server, page):
    """At a mobile viewport the page body must not overflow horizontally, even
    after a search renders the matrix."""
    page.set_viewport_size(MOBILE)
    page.goto(seed_live_server)
    page.click("#loadCities")
    _select_chips(page)
    page.click("#run")
    page.wait_for_selector("#rec", state="visible", timeout=15000)
    page.wait_for_function(
        "() => document.querySelector('td a.price') !== null", timeout=15000)

    # The document must not be wider than the viewport (small tolerance for rounding).
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 2, f"Horizontal overflow detected: scrollWidth-innerWidth={overflow}"


def test_mobile_matrix_is_in_scroll_container(seed_live_server, page):
    """Each city's matrix table must live inside a horizontal-scroll wrapper so a
    wide grid scrolls within its container instead of overflowing the viewport."""
    page.set_viewport_size(MOBILE)
    page.goto(seed_live_server)
    page.click("#loadCities")
    _select_chips(page)
    page.click("#run")
    page.wait_for_selector("#rec", state="visible", timeout=15000)
    page.wait_for_selector("table", timeout=15000)

    # Every table must have a .table-scroll ancestor whose overflow-x is auto/scroll.
    wrapped = page.evaluate(
        "() => [...document.querySelectorAll('#grids table')].every(t => {"
        "  const w = t.closest('.table-scroll');"
        "  if (!w) return false;"
        "  const ox = getComputedStyle(w).overflowX;"
        "  return ox === 'auto' || ox === 'scroll';"
        "})")
    assert wrapped, "Each matrix table must be inside an overflow-x scroll wrapper"

    # The legacy bare-table selector must still find a table (no broken selectors).
    assert page.query_selector("table") is not None


def test_mobile_form_inputs_usable(seed_live_server, page):
    """At a mobile viewport the form's key inputs must be visible and not wider
    than the viewport (stacked/full-width, not overflowing)."""
    page.set_viewport_size(MOBILE)
    page.goto(seed_live_server)

    for sel in ("#depCity", "#country", "#run"):
        assert page.is_visible(sel), f"{sel} should be visible on mobile"
        box = page.eval_on_selector(sel, "el => el.getBoundingClientRect().width")
        assert box <= MOBILE["width"], f"{sel} ({box}px) overflows the {MOBILE['width']}px viewport"
