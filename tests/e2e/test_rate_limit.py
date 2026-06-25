"""E2E tests for per-IP rate limiting (issue #60) — Rule 5 requires unit AND e2e.

Rate limiting is OFF by default for the test suite (the unit conftest disables it
and the e2e autouse fixture does not touch it). These tests spin up a LIVE Flask
server with the limiter explicitly ENABLED and a tiny per-window limit so the very
next request trips the 429. State is reset around each test so nothing leaks.

Two user-facing paths are exercised:

1. API-level: two POSTs to /api/search/stream — the 2nd returns 429 with a
   Retry-After header (the limiter contract surfaced over the network).
2. UI-level (Fix 1): exhaust the /api/top-cities bucket out-of-band, then trigger
   a country expansion in the browser and assert the existing destination chips
   are NOT cleared and a rate-limit message is shown — proving expandCountry's
   non-OK guard keeps the user's chips on a 429.
"""
import threading
import pytest
from werkzeug.serving import make_server
import app as appmod

from .conftest import _patch_common


@pytest.fixture
def rate_limited_server(monkeypatch):
    """Live Flask server with the limiter ON and a 1-per-window cap per bucket.

    The autouse e2e fixture does NOT reset ``_rate_state`` (it stays OFF for the
    other e2e tests), so this fixture owns the bucket state: clear it before the
    server thread starts and again on teardown so neighbouring tests are pristine.
    Patches are applied via monkeypatch BEFORE ``make_server`` so the request
    handler thread observes the enabled limiter and the tiny limits.
    """
    _patch_common(monkeypatch)
    monkeypatch.setattr(appmod, "top_cities",
                        lambda country, n=6: [{"city": "Shanghai", "iata": "PVG"}])
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "SEARCH_RATE_PER_MIN", 1)
    monkeypatch.setattr(appmod, "API_RATE_PER_MIN", 1)
    appmod._rate_state.clear()

    srv = make_server("127.0.0.1", 0, appmod.app)
    thread = threading.Thread(target=srv.serve_forever)
    thread.daemon = True
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        thread.join()
        appmod._rate_state.clear()


_SEARCH_BODY = {
    "origin": "YYZ",
    "destinations": [{"city": "Shanghai", "iata": "PVG"}],
    "dep_dates": ["2026-12-12"],
    "ret_dates": ["2027-01-04"],
}


def test_search_stream_second_request_is_429_with_retry_after(rate_limited_server, page):
    """Two POSTs to /api/search/stream: the 1st passes, the 2nd is 429 + Retry-After.

    Uses Playwright's request context so the requests share the page's client IP
    (the limiter keys on IP), exercising the real network path end to end.
    """
    url = f"{rate_limited_server}/api/search/stream"

    first = page.request.post(url, data=_SEARCH_BODY)
    assert first.ok, f"First request must pass, got {first.status}"

    second = page.request.post(url, data=_SEARCH_BODY)
    assert second.status == 429, f"Second request must be 429, got {second.status}"
    assert second.headers.get("retry-after"), \
        "429 response must carry a Retry-After header"
    body = second.json()
    assert "error" in body and "rate limit" in body["error"].lower(), \
        f"429 body must explain the rate limit, got {body!r}"


def test_top_cities_429_keeps_chips_and_shows_message(rate_limited_server, page):
    """Fix 1: a 429 from /api/top-cities must NOT clear the user's chips.

    Add a city chip (PVG) directly, then exhaust the API bucket out-of-band via
    page.request so the NEXT /api/top-cities (the UI expansion) returns 429. The
    UI expansion must then alert a rate-limit message and LEAVE the existing chip
    in place — never replace CITIES with an empty list.
    """
    page.goto(rate_limited_server)

    # The page load itself fires api-bucket requests (/api/health, /api/watch),
    # so reset the bucket to OWN it deterministically: after the reset, the FIRST
    # api call is the priming POST below, which passes; the SECOND (the UI
    # expansion) then trips the 1-per-window 429.
    appmod._rate_state.clear()

    # Add one destination chip via the UI helper (no network) so we have state to
    # protect. addCity() pushes a chip and draws it.
    page.evaluate("addCity('Shanghai', 'PVG')")
    page.wait_for_selector(".chip.on:has-text('Shanghai')")
    assert page.query_selector(".chip.on:has-text('Shanghai')") is not None

    # Burn the single API-bucket token out-of-band so the next /api/top-cities 429s.
    burn = page.request.post(
        f"{rate_limited_server}/api/top-cities",
        data={"country": "China", "n": 6},
    )
    assert burn.ok, f"Priming request should pass, got {burn.status}"

    dialog_messages = []
    page.on("dialog", lambda d: (dialog_messages.append(d.message), d.dismiss()))

    # Trigger a country expansion from the UI — this hits a now-rate-limited
    # /api/top-cities and returns 429.
    page.evaluate("expandCountry('China')")
    page.wait_for_timeout(600)

    assert len(dialog_messages) == 1, \
        f"Expected exactly one rate-limit alert, got {dialog_messages!r}"
    assert "rate limit" in dialog_messages[0].lower(), \
        f"Expected a rate-limit message, got: {dialog_messages[0]!r}"

    # The pre-existing Shanghai chip must STILL be present and selected — the 429
    # did not wipe CITIES.
    assert page.query_selector(".chip.on:has-text('Shanghai')") is not None, \
        "The existing chip must survive a 429 from /api/top-cities (Fix 1)"
