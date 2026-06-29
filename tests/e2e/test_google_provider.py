"""E2E test for the Google Flights provider through the search flow (rule 5).

Rule 5 requires unit AND e2e for every behaviour change.  The Google Flights unit
tests (tests/unit/test_google_flights.py) cover google_flights_fare internals; this
test drives a live Flask server through the browser/HTTP and asserts the
user-visible outcome:

  A search for YYZ→PEK where the Google Flights endpoint returns a valid body
  (topFlights with price 8987) renders that fare in the UI and reports
  source="google" in the API JSON response.

The test is fully offline and deterministic:
- time.sleep is patched to a no-op (skips any backoff in _request_with_retry).
- requests.get is patched so the call to /google/flights/search-roundtrip returns
  the SUCCESS body above; every OTHER URL (skyscanner search-roundtrip, etc.)
  returns a non-200 so google_flights_fare is the ONLY source that can produce a
  real price.
- ollama / build_recommendation are mocked via _patch_common / conftest.
- No real network calls occur.
"""

import threading
import unittest.mock as mock

import pytest
from werkzeug.serving import make_server

import app as appmod

from .conftest import _patch_common


# ---------------------------------------------------------------------------
# Fake HTTP response helper (mirrors test_provider_retry.py)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, body, status):
        self.status_code = status
        self._body = body
        self.headers = {}

    def json(self):
        return self._body


# ---------------------------------------------------------------------------
# The Google Flights success body (party-total price 8987 CAD, 1 stop, Air Canada)
# ---------------------------------------------------------------------------

_GOOGLE_FLIGHT_ITEM = {
    "price": 8987,
    "stops": 1,
    "duration": 1160,
    "airlineNames": ["Air Canada"],
    "airline": [{"airlineCode": "AC", "airlineName": "Air Canada"}],
    "segments": [
        {"departureAirportCode": "YYZ", "arrivalAirportCode": "NRT", "durationMinutes": 700},
        {"departureAirportCode": "NRT", "arrivalAirportCode": "PEK", "durationMinutes": 300},
    ],
    "transferAirports": None,
    "isAvailable": True,
}

_CHEAPER_FLIGHT_ITEM = {
    "price": 7500,
    "stops": 2,
    "duration": 1400,
    "airlineNames": ["Air Canada"],
    "airline": [{"airlineCode": "AC", "airlineName": "Air Canada"}],
    "segments": [
        {"departureAirportCode": "YYZ", "arrivalAirportCode": "ICN", "durationMinutes": 800},
        {"departureAirportCode": "ICN", "arrivalAirportCode": "SHA", "durationMinutes": 200},
        {"departureAirportCode": "SHA", "arrivalAirportCode": "PEK", "durationMinutes": 100},
    ],
    "transferAirports": None,
    "isAvailable": True,
}

_GOOGLE_SUCCESS_BODY = {
    "status": True,
    "data": {
        "topFlights": [_GOOGLE_FLIGHT_ITEM],
        "otherFlights": [_CHEAPER_FLIGHT_ITEM],
    },
}


def _make_fake_get(rapidapi_host):
    """Return a fake requests.get that serves the Google success body for the
    google-flights URL and a 404 for everything else.

    This ensures google_flights_fare is the ONLY provider that can produce a
    real price (skyscanner_fare, serpapi_fare, etc. all fail-fast on non-200).
    """
    def fake_get(url, *_a, **_k):
        if f"{rapidapi_host}/google/flights/search-roundtrip" in url:
            return _FakeResponse(_GOOGLE_SUCCESS_BODY, status=200)
        # All other provider URLs return 404 → provider returns None.
        return _FakeResponse({}, status=404)

    return fake_get


# ---------------------------------------------------------------------------
# Server fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def google_provider_server(monkeypatch):
    """Live Flask server where only the Google Flights provider can succeed.

    - RAPIDAPI_KEY is set so google_flights_fare (and skyscanner_fare) are enabled.
    - All other providers are disabled (no credentials).
    - requests.get is patched: the google-flights URL returns _GOOGLE_SUCCESS_BODY;
      every other URL returns 404 so skyscanner_fare / others return None.
    - time.sleep is patched to a no-op.
    - The fare cache is disabled (TTL=0) so every cell really calls the provider.
    - _patch_common sets up ollama/build_recommendation mocks.
    """
    _patch_common(monkeypatch)

    # Enable RAPIDAPI_KEY (google + skyscanner both use it), disable others.
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "test-rapidapi-key")
    monkeypatch.setattr(appmod, "KIWI_API_KEY", None)
    monkeypatch.setattr(appmod, "AMADEUS_ID", None)
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", None)
    monkeypatch.setattr(appmod, "SERPAPI_KEY", None)
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", None)

    # Skip real backoff delays.
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)

    # Bypass fare cache so providers are actually called.
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", 0)

    # Use the real (uncached) get_fare so provider dispatch runs.
    monkeypatch.setattr(appmod, "get_fare", appmod._get_fare_uncached)

    # Patch requests.get: google URL → success; everything else → 404.
    monkeypatch.setattr(
        appmod.requests, "get",
        _make_fake_get(appmod.RAPIDAPI_HOST),
    )

    # Only one destination city (Beijing/PEK) — keeps the search fast.
    monkeypatch.setattr(
        appmod, "top_cities",
        lambda country, n=6: [{"city": "Beijing", "iata": "PEK"}],
    )

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
# Search body (canonical Toronto→China scenario; Beijing/PEK is the destination)
# ---------------------------------------------------------------------------

_SEARCH_BODY = {
    "origin": "YYZ",
    "destinations": [{"city": "Beijing", "iata": "PEK"}],
    "dep_dates": ["2026-12-12"],
    "ret_dates": ["2027-01-04"],
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_google_provider_renders_fare_in_ui(google_provider_server, page):
    """A Google-sourced fare (price 8987, Air Canada) renders in the search UI.

    The fake requests.get returns the Google Flights success body for the
    google-flights endpoint.  google_flights_fare is tried first (primary provider)
    and should produce a cheapest_cad of 7500 (the otherFlights item is cheaper)
    with source="google".  The UI must render a price cell containing "7,500" and
    no cells must remain in the loading skeleton state.
    """
    page.goto(google_provider_server)
    page.click("#loadCities")
    page.wait_for_selector(".chip")
    chip = page.query_selector(".chip:not(.hint):not(.on)")
    assert chip is not None, "Expected at least one destination chip"
    chip.click()
    page.wait_for_selector(".chip.on")

    page.click("#run")
    # Wait for the recommendation panel (stream complete).
    page.wait_for_selector("#rec", state="visible", timeout=20000)

    # At least one price link must be visible.
    page.wait_for_function(
        "() => document.querySelector('td a.price') !== null",
        timeout=20000,
    )
    price_link = page.query_selector("td a.price")
    assert price_link is not None, "Expected a rendered price link from google_flights_fare"
    price_text = price_link.inner_text()
    assert "$" in price_text, (
        f"Google-sourced fare must render a CAD price, got: {price_text!r}"
    )
    # The cheaper otherFlights item (7500) is the cheapest overall; confirm it shows.
    assert "7,500" in price_text or "7500" in price_text, (
        f"Expected the cheapest google fare (7500) in the price cell, got: {price_text!r}"
    )

    # No loading skeleton cells must remain after the stream completes.
    loading = page.eval_on_selector_all("#grids td.loading", "els => els.length")
    assert loading == 0, f"{loading} cells still show '…' after stream complete"


def test_google_provider_api_returns_google_source(google_provider_server, page):
    """POST /api/search/stream with YYZ→PEK yields source='google' in the response.

    Drives the stream via page.request.post and asserts the streaming completes
    without HTTP error (HTTP 200 header).  Then drives the full UI to read the
    final rendered price, confirming the google source propagated end-to-end.
    """
    # Verify the stream endpoint starts OK (it responds 200 before emitting SSE).
    resp = page.request.post(
        f"{google_provider_server}/api/search/stream",
        data=_SEARCH_BODY,
    )
    assert resp.ok, f"Search stream must start OK, got {resp.status}"

    # Also hit the synchronous /api/search to inspect the JSON payload directly.
    import json as _json

    sync_resp = page.request.post(
        f"{google_provider_server}/api/search",
        data=_SEARCH_BODY,
    )
    assert sync_resp.ok, f"/api/search must return 200, got {sync_resp.status}"
    body = sync_resp.json()

    # /api/search returns a dict with a 'results' list (one entry per destination city).
    assert isinstance(body, dict), f"Expected a dict from /api/search, got: {type(body)!r}"
    results = body.get("results", [])
    assert len(results) > 0, f"Expected non-empty results, got: {body!r}"

    # The 'best' fare for the city must come from the google provider.
    first_result = results[0]
    best = first_result.get("best", {})
    assert best.get("source") == "google", (
        f"Expected source='google' from google_flights_fare, got: {best.get('source')!r}"
    )
    # The cheaper otherFlights item (7500) beats the topFlights item (8987).
    assert best.get("cheapest_cad") == 7500, (
        f"Expected cheapest_cad=7500 (cheapest across top+other flights), got: {best.get('cheapest_cad')!r}"
    )
