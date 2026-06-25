"""
Unit tests for the in-memory TTL fare cache in app.py.
All tests follow TDD: each test was written before the implementation and verified RED first.
"""
import app as appmod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ARGS = ("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
_REAL_FARE = {"cheapest_cad": 4200, "stops": 1, "nonstop_cad": None, "source": "travelpayouts"}
_NO_DATA = {"cheapest_cad": None, "stops": None, "nonstop_cad": None,
            "source": "no-data", "duration_min": None}


def _patch_uncached(monkeypatch, return_value, counter=None):
    """Patch the uncached provider path and count calls."""
    calls = counter if counter is not None else {"n": 0}

    def _fake(*args):
        calls["n"] += 1
        return return_value

    # Patch both provider functions so neither actually fires
    monkeypatch.setattr(appmod, "amadeus_fare", _fake)
    monkeypatch.setattr(appmod, "travelpayouts_fare", lambda *a: None)
    return calls


# ---------------------------------------------------------------------------
# Test 1: Cache hit — provider called only once for same args
# ---------------------------------------------------------------------------

def test_cache_hit_calls_provider_once(monkeypatch):
    """With TTL > 0, a second call with identical args must return the cached result."""
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", 3600)
    calls = {"n": 0}

    def fake_amadeus(*a):
        calls["n"] += 1
        return _REAL_FARE

    monkeypatch.setattr(appmod, "amadeus_fare", fake_amadeus)
    monkeypatch.setattr(appmod, "travelpayouts_fare", lambda *a: None)

    result1 = appmod.get_fare(*_ARGS)
    result2 = appmod.get_fare(*_ARGS)

    assert calls["n"] == 1, f"Provider called {calls['n']} times; expected 1 (cache miss then hit)"
    assert result1 == result2


# ---------------------------------------------------------------------------
# Test 2: Distinct keys — different args bypass the cache
# ---------------------------------------------------------------------------

def test_distinct_keys_each_call_provider(monkeypatch):
    """Different args must NOT be served from each other's cache entry."""
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", 3600)
    calls = {"n": 0}

    def fake_amadeus(*a):
        calls["n"] += 1
        return _REAL_FARE

    monkeypatch.setattr(appmod, "amadeus_fare", fake_amadeus)
    monkeypatch.setattr(appmod, "travelpayouts_fare", lambda *a: None)

    appmod.get_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    appmod.get_fare("YYZ", "LHR", "2026-12-12", "2027-01-04", 2, 0)  # different dest

    assert calls["n"] == 2, "Each distinct key must invoke the provider separately"


# ---------------------------------------------------------------------------
# Test 3: TTL expiry — stale entries are re-fetched
# ---------------------------------------------------------------------------

def test_cache_entry_expires_after_ttl(monkeypatch):
    """After the TTL elapses the provider must be called again."""
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", 60)
    calls = {"n": 0}
    now = [1_000_000.0]

    monkeypatch.setattr(appmod.time, "time", lambda: now[0])

    def fake_amadeus(*a):
        calls["n"] += 1
        return _REAL_FARE

    monkeypatch.setattr(appmod, "amadeus_fare", fake_amadeus)
    monkeypatch.setattr(appmod, "travelpayouts_fare", lambda *a: None)

    # First call — caches with expiry = now[0] + 60
    appmod.get_fare(*_ARGS)
    assert calls["n"] == 1

    # Advance time past expiry
    now[0] += 61

    # Second call — entry is stale; provider must fire again
    appmod.get_fare(*_ARGS)
    assert calls["n"] == 2, "Stale cache entry should trigger a re-fetch"


# ---------------------------------------------------------------------------
# Test 4: Disabled (TTL <= 0) — provider called every time
# ---------------------------------------------------------------------------

def test_cache_disabled_calls_provider_every_time(monkeypatch):
    """When FARE_CACHE_TTL <= 0, caching must be entirely skipped."""
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", 0)
    calls = {"n": 0}

    def fake_amadeus(*a):
        calls["n"] += 1
        return _REAL_FARE

    monkeypatch.setattr(appmod, "amadeus_fare", fake_amadeus)
    monkeypatch.setattr(appmod, "travelpayouts_fare", lambda *a: None)

    appmod.get_fare(*_ARGS)
    appmod.get_fare(*_ARGS)

    assert calls["n"] == 2, "With TTL=0 the provider must be called on every request"


def test_cache_disabled_negative_ttl(monkeypatch):
    """TTL < 0 must also disable caching."""
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", -1)
    calls = {"n": 0}

    def fake_amadeus(*a):
        calls["n"] += 1
        return _REAL_FARE

    monkeypatch.setattr(appmod, "amadeus_fare", fake_amadeus)
    monkeypatch.setattr(appmod, "travelpayouts_fare", lambda *a: None)

    appmod.get_fare(*_ARGS)
    appmod.get_fare(*_ARGS)

    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Test 5: no-data sentinel must NOT be cached
# ---------------------------------------------------------------------------

def test_no_data_not_cached(monkeypatch):
    """A no-data result (cheapest_cad is None/falsy) must never be stored in the cache."""
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", 3600)
    calls = {"n": 0}

    def fake_amadeus(*a):
        calls["n"] += 1
        return None  # provider returns nothing → get_fare returns no-data sentinel

    monkeypatch.setattr(appmod, "amadeus_fare", fake_amadeus)
    monkeypatch.setattr(appmod, "travelpayouts_fare", lambda *a: None)

    result1 = appmod.get_fare(*_ARGS)
    result2 = appmod.get_fare(*_ARGS)

    assert result1 == _NO_DATA
    assert result2 == _NO_DATA
    assert calls["n"] == 2, "no-data result must not be cached; provider called each time"
