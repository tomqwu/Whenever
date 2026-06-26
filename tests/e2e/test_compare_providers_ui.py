"""E2E tests for opt-in cross-provider price comparison (#43).

With the "Compare all providers" toggle ON, a streaming search queries every
configured provider per cell, renders the CHEAPEST provider's price, and shows a
subtle "also: SOURCE $PRICE" line for the other providers. With the toggle OFF
(default) a single provider's price renders and no alternatives line appears.

The fixture configures three providers (skyscanner 900, travelpayouts 800,
kiwi 850), uses the REAL get_fare/_get_fare_uncached so the compare/cache/
cheapest-pick logic runs end-to-end, and stubs the provider functions so the
test is fully offline + deterministic (no network, no retries/sleeps).
"""
import threading

import pytest
from werkzeug.serving import make_server

import app as appmod

from tests.e2e.conftest import select_some_chips as _select_some_chips


def _fare(price, source):
    return {
        "cheapest_cad": price, "stops": 1, "nonstop_cad": None,
        "source": source, "book": "https://example.com/" + source,
        "duration_min": None, "nonstop_duration_min": None,
        "airlines": None, "nonstop_airlines": None, "layovers": None,
    }


@pytest.fixture
def compare_live_server(monkeypatch):
    """Live server with three CONFIGURED providers returning distinct real prices.

    travelpayouts (800) is the cheapest -> chosen; kiwi (850) and skyscanner
    (900) become alternatives (ascending). The real get_fare runs so compare
    mode picks the cheapest and attaches alternatives.
    """
    # Configure exactly three providers (others unset -> skipped in compare).
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")        # skyscanner
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "k")
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")
    monkeypatch.setattr(appmod, "SERPAPI_KEY", None)
    monkeypatch.setattr(appmod, "AMADEUS_ID", None)
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", None)

    monkeypatch.setattr(appmod, "skyscanner_fare", lambda *a: _fare(900, "skyscanner"))
    monkeypatch.setattr(appmod, "serpapi_fare", lambda *a: None)
    monkeypatch.setattr(appmod, "amadeus_fare", lambda *a: None)
    monkeypatch.setattr(appmod, "travelpayouts_fare", lambda *a: _fare(800, "travelpayouts"))
    monkeypatch.setattr(appmod, "kiwi_fare", lambda *a: _fare(850, "kiwi"))

    monkeypatch.setattr(appmod, "ollama_ok", lambda: True)
    monkeypatch.setattr(appmod, "build_recommendation",
                        lambda *a, **k: "Best value: test recommendation")
    monkeypatch.setattr(appmod, "top_cities",
                        lambda country, n=6: [{"city": "Shanghai", "iata": "PVG"}])
    # Memory-only cache so compared/fallback runs don't collide across tests.
    monkeypatch.setattr(appmod, "FARE_CACHE_PATH", "")
    appmod._fare_cache.clear()

    srv = make_server("127.0.0.1", 0, appmod.app)
    thread = threading.Thread(target=srv.serve_forever)
    thread.daemon = True
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        thread.join()
        appmod._fare_cache.clear()


def test_compare_on_shows_cheapest_and_alternatives(compare_live_server, page):
    """Toggle ON: the cheapest provider (travelpayouts $800) renders, and an
    'also:' line lists the other providers (kiwi $850, skyscanner $900)."""
    page.goto(compare_live_server)
    page.click("#loadCities")
    _select_some_chips(page)

    # Opt in to cross-provider comparison.
    page.check("#compareProviders")

    page.click("#run")
    page.wait_for_selector("#rec", state="visible", timeout=15000)

    # Chosen (cheapest) price is travelpayouts $800.
    page.wait_for_function(
        "() => [...document.querySelectorAll('td a.price')]"
        ".some(e => e.textContent.includes('800'))",
        timeout=15000,
    )

    # The alternatives line is present and names the other providers + prices.
    page.wait_for_selector("td .alts", timeout=15000)
    alts = page.eval_on_selector_all("td .alts", "els => els.map(e => e.textContent)")
    assert any("also:" in t for t in alts), f"Expected an 'also:' line, got {alts!r}"
    joined = " ".join(alts)
    assert "kiwi" in joined and "850" in joined, f"Expected kiwi 850 alt, got {alts!r}"
    assert "skyscanner" in joined and "900" in joined, \
        f"Expected skyscanner 900 alt, got {alts!r}"


def test_compare_off_shows_single_price_no_alternatives(compare_live_server, page):
    """Toggle OFF (default): ordered fallback renders one provider's price (the
    first configured = skyscanner $900) and NO alternatives line appears."""
    page.goto(compare_live_server)
    page.click("#loadCities")
    _select_some_chips(page)

    # Do NOT check #compareProviders -> ordered fallback.
    page.click("#run")
    page.wait_for_selector("#rec", state="visible", timeout=15000)

    # A price renders (skyscanner is first in the chain -> $900).
    page.wait_for_function(
        "() => document.querySelector('td a.price') !== null",
        timeout=15000,
    )
    prices = page.eval_on_selector_all(
        "td a.price", "els => els.map(e => e.textContent)")
    assert any("900" in t for t in prices), \
        f"Expected fallback skyscanner $900, got {prices!r}"

    # No alternatives line in fallback mode.
    assert page.query_selector("td .alts") is None, \
        "fallback mode must not render an 'also:' alternatives line"
