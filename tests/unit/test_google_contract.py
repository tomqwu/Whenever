"""Contract tests for the Google Flights provider against a REAL captured response.

WHY THIS FILE EXISTS: every other test mocks the provider with a response shape WE
invented, so they verify "does our parser handle the shape we imagined" — they cannot
catch the provider changing its actual contract. That is exactly the failure that hid a
broken integration behind a green suite (the app returned no fares while every test
passed). These two tests close that gap:

1. ``test_parser_matches_real_captured_response`` — feeds google_flights_fare a REAL
   response captured from the live API (``tests/fixtures/google_search_roundtrip.json``).
   If our parsing drifts from the captured reality, this fails. Re-capture the fixture
   whenever the provider's shape changes (see the helper command in the fixture's PR).

2. ``test_live_contract`` — OPT-IN (set ``RUN_LIVE_CONTRACT=1`` and ``RAPIDAPI_KEY``).
   Hits the REAL endpoint and asserts a parseable fare comes back. This is the ONLY true
   drift-catcher; wire it into a nightly/pre-release CI job so a provider contract change
   fails loudly instead of silently passing. It is skipped in the normal offline gate to
   stay deterministic and to protect API quota.
"""
import json
import os
import time

import pytest

import app as appmod

_FIXTURE = os.path.join(os.path.dirname(__file__), "..", "fixtures",
                        "google_search_roundtrip.json")


class _Resp:
    """Minimal stand-in for a requests.Response wrapping the captured body."""

    def __init__(self, body):
        self.status_code = 200
        self.headers = {}
        self._body = body

    def json(self):
        return self._body


def test_parser_matches_real_captured_response(monkeypatch):
    """google_flights_fare must correctly parse the REAL captured Google response."""
    with open(_FIXTURE) as fh:
        body = json.load(fh)
    flights = body["data"]["topFlights"] + body["data"]["otherFlights"]
    expected_cheapest = round(min(f["price"] for f in flights
                                  if isinstance(f.get("price"), (int, float))))

    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: _Resp(body))

    res = appmod.google_flights_fare("YYZ", "PEK", "2026-12-12", "2027-01-04", 2, 2)

    assert res is not None, "parser returned None on a REAL captured success response"
    assert res["source"] == "google"
    # Pinned to the real response's own cheapest price (robust to fixture refresh).
    assert res["cheapest_cad"] == expected_cheapest
    assert res["stops"] is not None
    assert res["airlines"]  # real response carries carrier names


def test_google_retries_through_transient_error(monkeypatch):
    """A transient Google error (HTTP 200, status:false) is retried — the next good
    response wins, so a single blip never surfaces as 'no fares'."""
    with open(_FIXTURE) as fh:
        good = json.load(fh)
    bad = {"status": False, "message": "Errors", "data": None}
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod, "GOOGLE_RETRIES", 3)
    monkeypatch.setattr(appmod, "GOOGLE_RETRY_BACKOFF", 0)
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        return _Resp(bad if calls["n"] == 1 else good)

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    res = appmod.google_flights_fare("YYZ", "PEK", "2026-12-12", "2027-01-04", 2, 0)
    assert res is not None and res["source"] == "google"
    assert calls["n"] == 2  # first attempt failed transiently, second succeeded


def test_google_gives_up_after_bounded_retries(monkeypatch):
    """Persistent transient errors → None after exactly GOOGLE_RETRIES+1 attempts."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod, "GOOGLE_RETRIES", 3)
    monkeypatch.setattr(appmod, "GOOGLE_RETRY_BACKOFF", 0)
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        return _Resp({"status": False, "message": "Errors", "data": None})

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    res = appmod.google_flights_fare("YYZ", "PEK", "2026-12-12", "2027-01-04", 2, 0)
    assert res is None
    assert calls["n"] == 4  # GOOGLE_RETRIES (3) + 1


@pytest.mark.skipif(
    not (os.environ.get("RUN_LIVE_CONTRACT") and os.environ.get("RAPIDAPI_KEY")),
    reason="live contract test: set RUN_LIVE_CONTRACT=1 and RAPIDAPI_KEY to run",
)
def test_live_contract(monkeypatch):
    """Hit the REAL Google Flights endpoint and assert a parseable fare comes back.

    The provider retries transient errors internally (GOOGLE_RETRIES); if it still can't
    return a fare, that's a real signal the provider is unhealthy — fail loudly.
    """
    # The autouse _reset_state fixture nulls the key and zeroes retries for offline
    # determinism — restore them so this live test actually calls the real API.
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", os.environ["RAPIDAPI_KEY"])
    monkeypatch.setattr(appmod, "RAPIDAPI_HOST",
                        os.environ.get("RAPIDAPI_HOST", "flights-sky.p.rapidapi.com"))
    monkeypatch.setattr(appmod, "GOOGLE_RETRIES", 5)
    monkeypatch.setattr(appmod, "GOOGLE_RETRY_BACKOFF", 3)
    res = appmod.google_flights_fare("YYZ", "PEK", "2026-12-12", "2027-01-04", 2, 0)
    assert res and isinstance(res.get("cheapest_cad"), (int, float)) and res["cheapest_cad"] > 0, \
        f"live Google contract returned no parseable fare (got {res})"
    assert res["source"] == "google"
