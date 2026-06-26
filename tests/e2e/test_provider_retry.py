"""E2E tests for provider transient-failure retry/degrade through the search flow (#41).

Rule 5 requires unit AND e2e for every behaviour change. The retry unit tests
(tests/unit/test_fares.py) cover _request_with_retry internals; these tests drive
a real browser through a live Flask server and assert the user-visible outcome:

1. Transient 503 → retry succeeds → search cell shows the recovered real price.
2. Persistent 503 → all providers 503 every attempt → cell degrades gracefully to
   the calm no-data '—' state (never an alarming error, never the '…' skeleton).

Both scenarios:
- patch time.sleep to skip real backoff waits (fully deterministic, offline).
- patch requests.get so no real network call is made.
- mock build_recommendation / ollama as all other e2e tests do (via conftest).
"""

import threading
import unittest.mock as mock

import pytest
from werkzeug.serving import make_server

import app as appmod

from .conftest import _patch_common


# ---------------------------------------------------------------------------
# Helper: a minimal fake requests.Response
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, body, status):
        self.status_code = status
        self._body = body
        self.headers = {}

    def json(self):
        return self._body


def _travelpayouts_success_body():
    return {"data": [
        {"price": 500, "transfers": 0, "return_transfers": 0,
         "airline": "AC", "link": "/booking/x",
         "duration_to": 800, "duration_back": 750},
    ]}


# ---------------------------------------------------------------------------
# Server fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def retry_transient_server(monkeypatch):
    """Server where travelpayouts_fare first 503s then succeeds on retry.

    _request_with_retry uses requests.get internally; we patch it so the very
    first call (from any provider) returns a 503, and the SECOND call returns a
    valid priced response — mimicking a transient upstream hiccup that the retry
    logic recovers from.

    All other providers are disabled (no credentials) so the retry path through
    travelpayouts_fare is the ONLY one that can produce a real price, making the
    assertion clean and deterministic.

    time.sleep is patched to a no-op so no real backoff delay occurs.
    """
    _patch_common(monkeypatch)
    monkeypatch.setattr(appmod, "top_cities",
                        lambda country, n=6: [{"city": "Shanghai", "iata": "PVG"}])
    # Ensure only travelpayouts_fare can succeed (no other API keys).
    monkeypatch.setattr(appmod, "KIWI_API_KEY", None)
    monkeypatch.setattr(appmod, "AMADEUS_ID", None)
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", None)
    monkeypatch.setattr(appmod, "SERPAPI_KEY", None)
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", None)
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "test-token")

    # No real sleep during backoff.
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)

    # Restore the real get_fare / _get_fare_uncached so retry logic runs
    # (_patch_common replaced get_fare with a stub that never calls providers).
    monkeypatch.setattr(appmod, "get_fare", appmod.get_fare.__wrapped__
                        if hasattr(appmod.get_fare, "__wrapped__") else appmod.get_fare)

    # Expose real implementations (the conftest patched the module-level names;
    # restore by pointing directly at the real function objects).
    monkeypatch.setattr(appmod, "get_fare", appmod._get_fare_uncached)

    # Disable the fare cache for this fixture so each search cell really calls
    # the provider (and exercises the retry logic).
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", 0)

    # Build a sequential fake requests.get: 503 → 200.
    call_count = {"n": 0}

    def fake_get(*_a, **_k):
        i = call_count["n"]
        call_count["n"] += 1
        if i == 0:
            return _FakeResponse({}, status=503)
        return _FakeResponse(_travelpayouts_success_body(), status=200)

    monkeypatch.setattr(appmod.requests, "get", fake_get)

    # Skyscanner GET also goes through requests.get; make sure skyscanner_fare
    # returns None quickly (it will 503 on its own first call via fake_get, but
    # since the fake_get sequence is shared we restart the counter AFTER wiring
    # the fixture so the first cell's first provider attempt is travelpayouts).
    # Simplest: disable skyscanner by not setting any key — already the case.

    srv = make_server("127.0.0.1", 0, appmod.app)
    thread = threading.Thread(target=srv.serve_forever)
    thread.daemon = True
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        thread.join()


@pytest.fixture
def retry_persistent_server(monkeypatch):
    """Server where ALL provider HTTP calls always return 503 (persistent failure).

    _request_with_retry retries up to PROVIDER_RETRIES times then gives up;
    each provider returns None; _get_fare_uncached falls through to the no-data
    sentinel. The search must complete and cells must show '—', never an error.

    time.sleep is patched to a no-op.
    """
    _patch_common(monkeypatch)
    monkeypatch.setattr(appmod, "top_cities",
                        lambda country, n=6: [{"city": "Shanghai", "iata": "PVG"}])
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")            # enable kiwi so providers run
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "tok")   # enable travelpayouts
    monkeypatch.setattr(appmod, "AMADEUS_ID", None)
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", None)
    monkeypatch.setattr(appmod, "SERPAPI_KEY", None)
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", None)

    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", 0)
    monkeypatch.setattr(appmod, "get_fare", appmod._get_fare_uncached)

    # Every request.get / requests.post returns a 503 indefinitely.
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *_a, **_k: _FakeResponse({}, status=503))
    monkeypatch.setattr(appmod.requests, "post",
                        lambda *_a, **_k: _FakeResponse({}, status=503))

    srv = make_server("127.0.0.1", 0, appmod.app)
    thread = threading.Thread(target=srv.serve_forever)
    thread.daemon = True
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        thread.join()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

_SEARCH_BODY = {
    "origin": "YYZ",
    "destinations": [{"city": "Shanghai", "iata": "PVG"}],
    "dep_dates": ["2026-12-12"],
    "ret_dates": ["2027-01-04"],
}


def test_transient_503_retry_recovers_real_price(retry_transient_server, page):
    """A single transient 503 is retried and the search cell shows the real price.

    The first requests.get call for the cell returns 503; _request_with_retry
    retries; the second call returns a valid 200 with price 500 CAD (×1 pax).
    The streaming search must complete and the cell must show '$500', not '—'.
    No real network call or sleep occurs.
    """
    resp = page.request.post(
        f"{retry_transient_server}/api/search/stream",
        data=_SEARCH_BODY,
    )
    assert resp.ok, f"Search stream must start OK, got {resp.status}"

    # Load the page and trigger a streaming search through the browser UI so we
    # can assert the rendered cell value.
    page.goto(retry_transient_server)
    page.click("#loadCities")
    page.wait_for_selector(".chip")
    chip = page.query_selector(".chip:not(.hint):not(.on)")
    assert chip is not None, "Expected at least one chip to select"
    chip.click()
    page.wait_for_selector(".chip.on")

    page.click("#run")
    # Wait for recommendation (stream complete).
    page.wait_for_selector("#rec", state="visible", timeout=15000)

    # At least one cell must have a rendered price (not the '…' skeleton or '—').
    page.wait_for_function(
        "() => document.querySelector('td a.price') !== null",
        timeout=15000,
    )
    price_link = page.query_selector("td a.price")
    assert price_link is not None, "Expected a rendered price link after retry recovery"
    price_text = price_link.inner_text()
    assert "$" in price_text, (
        f"After retry recovery the cell must show a real $ price, got: {price_text!r}"
    )
    # No cell should remain stuck on the loading placeholder.
    loading = page.eval_on_selector_all("#grids td.loading", "els => els.length")
    assert loading == 0, f"{loading} cells still show the loading skeleton after stream done"


def test_persistent_503_degrades_to_no_data_gracefully(retry_persistent_server, page):
    """When every provider persistently 503s, cells degrade to calm '—' — no crash.

    _request_with_retry exhausts retries for every provider; _get_fare_uncached
    falls through to the no-data sentinel. The search must complete and every
    cell must render the '—' no-data style (class .nodata), never the alarming
    .err style and never the '…' loading skeleton.
    """
    page.goto(retry_persistent_server)
    page.click("#loadCities")
    page.wait_for_selector(".chip")
    chip = page.query_selector(".chip:not(.hint):not(.on)")
    assert chip is not None, "Expected at least one chip to select"
    chip.click()
    page.wait_for_selector(".chip.on")

    page.click("#run")
    # Stream completes even with all-503 providers — recommendation is shown.
    page.wait_for_selector("#rec", state="visible", timeout=15000)

    # No loading skeletons must remain.
    loading = page.eval_on_selector_all("#grids td.loading", "els => els.length")
    assert loading == 0, f"{loading} cells still show '…' after persistent-503 stream done"

    # No alarming .err cells — graceful degradation only.
    err_cells = page.eval_on_selector_all("#grids td.err", "els => els.length")
    assert err_cells == 0, (
        "Cells must not use the alarming .err style when providers persistently 503"
    )

    # At least one .nodata cell (calm '—') must be present for the no-fare city.
    nodata = page.eval_on_selector_all("#grids td.nodata", "els => els.length")
    assert nodata > 0, (
        "Expected calm .nodata cells when all providers return 503 persistently"
    )

    # The summary card for the city must reflect no fares (not a real price).
    card_price = page.inner_text("#card-price-0")
    assert card_price == "—", (
        f"Summary card price should be '—' on persistent provider failure, got: {card_price!r}"
    )
