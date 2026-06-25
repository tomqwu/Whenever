"""Unit tests for in-memory per-IP rate limiting (issue #60).

TDD: these tests are written BEFORE the implementation exists.
They describe the required contract; each will fail (ImportError or
AssertionError) until app.py gains the rate-limiting code.
"""
import time
import threading
import pytest
import app as appmod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEARCH_BODY = {
    "origin": "YYZ",
    "destinations": [{"city": "Paris", "iata": "CDG"}],
    "dep_dates": ["2026-12-12"],
    "ret_dates": ["2027-01-04"],
}

_WATCH_BODY = {
    "origin": "YYZ",
    "dest_iata": "CDG",
    "dest_city": "Paris",
    "dep_date": "2026-12-12",
    "ret_date": "2027-01-04",
    "adults": 2,
}


# ---------------------------------------------------------------------------
# Rate-limit infrastructure
# ---------------------------------------------------------------------------

def test_rate_state_exists_and_is_clearable():
    """_rate_state must be a module-level dict that can be .clear()ed."""
    assert isinstance(appmod._rate_state, dict)
    appmod._rate_state["x"] = [1, 2, 3]
    appmod._rate_state.clear()
    assert appmod._rate_state == {}


def test_rate_limit_config_defaults():
    """RATE_LIMIT_ENABLED/WINDOW/SEARCH_RATE_PER_MIN/API_RATE_PER_MIN must exist with right defaults."""
    # We check the attr exists; we can't assert exact value since env may differ,
    # but we do check the type is sensible.
    assert isinstance(appmod.RATE_LIMIT_ENABLED, bool)
    assert isinstance(appmod.RATE_LIMIT_WINDOW, (int, float))
    assert appmod.RATE_LIMIT_WINDOW > 0
    assert isinstance(appmod.SEARCH_RATE_PER_MIN, int)
    assert appmod.SEARCH_RATE_PER_MIN > 0
    assert isinstance(appmod.API_RATE_PER_MIN, int)
    assert appmod.API_RATE_PER_MIN > 0


def test_parse_bool_env_explicit_true(monkeypatch):
    """_parse_bool_env returns True when env var is set to a truthy string like 'yes'."""
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "yes")
    assert appmod._parse_bool_env("RATE_LIMIT_ENABLED", False) is True


def test_parse_bool_env_explicit_false(monkeypatch):
    """_parse_bool_env returns False when env var is '0', 'false', or 'no'."""
    for v in ("0", "false", "no"):
        monkeypatch.setenv("RATE_LIMIT_ENABLED", v)
        assert appmod._parse_bool_env("RATE_LIMIT_ENABLED", True) is False


# ---------------------------------------------------------------------------
# Disabled by default in tests (autouse fixture sets RATE_LIMIT_ENABLED=False)
# ---------------------------------------------------------------------------

def test_rate_limit_disabled_no_429_search(client, monkeypatch):
    """With RATE_LIMIT_ENABLED=False (default in tests) no 429 is ever returned."""
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(appmod, "SEARCH_RATE_PER_MIN", 1)
    monkeypatch.setattr(appmod, "run_search", lambda **kw: {"cells": [], "recommendation": None})

    for _ in range(5):
        r = client.post("/api/search", json=_SEARCH_BODY)
        assert r.status_code != 429


def test_rate_limit_disabled_no_429_api_bucket(client, monkeypatch):
    """API bucket also respects RATE_LIMIT_ENABLED=False."""
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(appmod, "API_RATE_PER_MIN", 1)
    monkeypatch.setattr(appmod, "top_cities", lambda c, n: [{"city": "Paris", "iata": "CDG", "optional": False}])

    for _ in range(5):
        r = client.post("/api/top-cities", json={"country": "France", "n": 3})
        assert r.status_code != 429


# ---------------------------------------------------------------------------
# Exempt endpoints (/ and /api/health)
# ---------------------------------------------------------------------------

def test_health_exempt_from_rate_limit(client, monkeypatch):
    """/api/health is never rate-limited even when limiter is enabled."""
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "API_RATE_PER_MIN", 1)
    monkeypatch.setattr(appmod, "SEARCH_RATE_PER_MIN", 1)
    appmod._rate_state.clear()
    monkeypatch.setattr(appmod, "ollama_ok", lambda: True)

    for _ in range(5):
        r = client.get("/api/health")
        assert r.status_code == 200


def test_index_exempt_from_rate_limit(client, monkeypatch):
    """/ (index) is never rate-limited even when limiter is enabled."""
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "API_RATE_PER_MIN", 1)
    monkeypatch.setattr(appmod, "SEARCH_RATE_PER_MIN", 1)
    appmod._rate_state.clear()

    for _ in range(5):
        r = client.get("/")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Search bucket – 429 on limit exceeded
# ---------------------------------------------------------------------------

def test_search_bucket_429_on_third_request(client, monkeypatch):
    """3rd POST to /api/search within the window returns 429."""
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "SEARCH_RATE_PER_MIN", 2)
    monkeypatch.setattr(appmod, "RATE_LIMIT_WINDOW", 60)
    monkeypatch.setattr(appmod, "run_search", lambda **kw: {"cells": [], "recommendation": None})
    appmod._rate_state.clear()

    r1 = client.post("/api/search", json=_SEARCH_BODY)
    assert r1.status_code == 200

    r2 = client.post("/api/search", json=_SEARCH_BODY)
    assert r2.status_code == 200

    r3 = client.post("/api/search", json=_SEARCH_BODY)
    assert r3.status_code == 429
    body = r3.get_json()
    assert "rate limit exceeded" in body.get("error", "").lower()


def test_search_bucket_429_has_retry_after_header(client, monkeypatch):
    """429 response includes a Retry-After header (positive integer)."""
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "SEARCH_RATE_PER_MIN", 1)
    monkeypatch.setattr(appmod, "RATE_LIMIT_WINDOW", 60)
    monkeypatch.setattr(appmod, "run_search", lambda **kw: {"cells": [], "recommendation": None})
    appmod._rate_state.clear()

    client.post("/api/search", json=_SEARCH_BODY)  # uses the 1 slot
    r = client.post("/api/search", json=_SEARCH_BODY)  # exceeds limit
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    retry_after = int(r.headers["Retry-After"])
    assert retry_after > 0


# ---------------------------------------------------------------------------
# Per-IP isolation
# ---------------------------------------------------------------------------

def test_different_ips_have_independent_buckets(client, monkeypatch):
    """Two different client IPs each get their own rate bucket."""
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "TRUST_PROXY", True)
    monkeypatch.setattr(appmod, "SEARCH_RATE_PER_MIN", 1)
    monkeypatch.setattr(appmod, "RATE_LIMIT_WINDOW", 60)
    monkeypatch.setattr(appmod, "run_search", lambda **kw: {"cells": [], "recommendation": None})
    appmod._rate_state.clear()

    # First IP hits limit
    r_a1 = client.post("/api/search", json=_SEARCH_BODY,
                        headers={"X-Forwarded-For": "1.2.3.4"})
    assert r_a1.status_code == 200
    r_a2 = client.post("/api/search", json=_SEARCH_BODY,
                        headers={"X-Forwarded-For": "1.2.3.4"})
    assert r_a2.status_code == 429

    # Second IP should still succeed
    r_b = client.post("/api/search", json=_SEARCH_BODY,
                       headers={"X-Forwarded-For": "5.6.7.8"})
    assert r_b.status_code == 200


def test_xff_first_hop_used(client, monkeypatch):
    """Only the first IP in X-Forwarded-For is used as client identity (TRUST_PROXY)."""
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "TRUST_PROXY", True)
    monkeypatch.setattr(appmod, "SEARCH_RATE_PER_MIN", 1)
    monkeypatch.setattr(appmod, "RATE_LIMIT_WINDOW", 60)
    monkeypatch.setattr(appmod, "run_search", lambda **kw: {"cells": [], "recommendation": None})
    appmod._rate_state.clear()

    # First request from "10.0.0.1" (via proxy chain)
    r1 = client.post("/api/search", json=_SEARCH_BODY,
                      headers={"X-Forwarded-For": "10.0.0.1, 172.16.0.1"})
    assert r1.status_code == 200

    # Second request: same first hop -> same bucket, should be limited
    r2 = client.post("/api/search", json=_SEARCH_BODY,
                      headers={"X-Forwarded-For": "10.0.0.1, 192.168.0.1"})
    assert r2.status_code == 429


# ---------------------------------------------------------------------------
# TRUST_PROXY: X-Forwarded-For is ignored by default (anti-spoofing)
# ---------------------------------------------------------------------------

def test_xff_ignored_by_default_keyed_on_remote_addr(client, monkeypatch):
    """With TRUST_PROXY False (default), a spoofed X-Forwarded-For is ignored:
    two requests carrying DIFFERENT spoofed XFF but the same test-client
    remote_addr share the SAME bucket, so the limit still applies and an
    attacker cannot rotate XFF to bypass 429s."""
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "TRUST_PROXY", False)
    monkeypatch.setattr(appmod, "SEARCH_RATE_PER_MIN", 1)
    monkeypatch.setattr(appmod, "RATE_LIMIT_WINDOW", 60)
    monkeypatch.setattr(appmod, "run_search", lambda **kw: {"cells": [], "recommendation": None})
    appmod._rate_state.clear()

    # First request with one spoofed XFF — consumes the single slot.
    r1 = client.post("/api/search", json=_SEARCH_BODY,
                     headers={"X-Forwarded-For": "1.2.3.4"})
    assert r1.status_code == 200

    # Second request rotates the spoofed XFF to a totally different value, but
    # since the header is ignored both are keyed on remote_addr (127.0.0.1):
    # the limit still bites.
    r2 = client.post("/api/search", json=_SEARCH_BODY,
                     headers={"X-Forwarded-For": "9.9.9.9"})
    assert r2.status_code == 429

    # Confirm the bucket key is the socket peer, not the spoofed header.
    assert ("127.0.0.1", "search") in appmod._rate_state
    assert ("1.2.3.4", "search") not in appmod._rate_state
    assert ("9.9.9.9", "search") not in appmod._rate_state


def test_xff_used_as_key_when_trust_proxy_true(client, monkeypatch):
    """With TRUST_PROXY True, the XFF first hop IS the key: distinct XFF first
    hops get distinct buckets (behind a trusted proxy)."""
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "TRUST_PROXY", True)
    monkeypatch.setattr(appmod, "SEARCH_RATE_PER_MIN", 1)
    monkeypatch.setattr(appmod, "RATE_LIMIT_WINDOW", 60)
    monkeypatch.setattr(appmod, "run_search", lambda **kw: {"cells": [], "recommendation": None})
    appmod._rate_state.clear()

    # First XFF identity hits its limit.
    assert client.post("/api/search", json=_SEARCH_BODY,
                       headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 200
    assert client.post("/api/search", json=_SEARCH_BODY,
                       headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 429

    # A distinct XFF first hop is a distinct bucket and still succeeds.
    assert client.post("/api/search", json=_SEARCH_BODY,
                       headers={"X-Forwarded-For": "5.6.7.8"}).status_code == 200
    assert ("1.2.3.4", "search") in appmod._rate_state
    assert ("5.6.7.8", "search") in appmod._rate_state


# ---------------------------------------------------------------------------
# Concurrency: lock makes check/append atomic
# ---------------------------------------------------------------------------

def test_rate_lock_is_a_lock():
    """A module-level threading lock guards the count-and-record sequence."""
    assert isinstance(appmod._rate_lock, type(threading.Lock()))


def test_concurrent_same_ip_respects_limit(client, monkeypatch):
    """Many threads hitting a limited endpoint with the same IP: the number of
    200s never exceeds the limit because the lock makes check/append atomic."""
    limit = 5
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "TRUST_PROXY", False)
    monkeypatch.setattr(appmod, "SEARCH_RATE_PER_MIN", limit)
    monkeypatch.setattr(appmod, "RATE_LIMIT_WINDOW", 60)
    monkeypatch.setattr(appmod, "run_search", lambda **kw: {"cells": [], "recommendation": None})
    appmod._rate_state.clear()

    results = []
    barrier = threading.Barrier(20)

    def hit():
        barrier.wait()  # release all threads as simultaneously as possible
        r = client.post("/api/search", json=_SEARCH_BODY)
        results.append(r.status_code)

    threads = [threading.Thread(target=hit) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = results.count(200)
    assert ok == limit, f"expected exactly {limit} successes, got {ok}: {results}"
    assert results.count(429) == 20 - limit


# ---------------------------------------------------------------------------
# Window reset
# ---------------------------------------------------------------------------

def test_window_reset_allows_requests_again(client, monkeypatch):
    """After the rate window expires, requests succeed again (sliding window)."""
    fake_now = [100.0]

    def mock_time():
        return fake_now[0]

    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "SEARCH_RATE_PER_MIN", 1)
    monkeypatch.setattr(appmod, "RATE_LIMIT_WINDOW", 60)
    monkeypatch.setattr(appmod, "run_search", lambda **kw: {"cells": [], "recommendation": None})
    monkeypatch.setattr(appmod, "_rate_time", mock_time)
    appmod._rate_state.clear()

    # t=100 — use the 1 slot
    r1 = client.post("/api/search", json=_SEARCH_BODY,
                      headers={"X-Forwarded-For": "9.9.9.9"})
    assert r1.status_code == 200

    # t=100 — 2nd request exceeds limit
    r2 = client.post("/api/search", json=_SEARCH_BODY,
                      headers={"X-Forwarded-For": "9.9.9.9"})
    assert r2.status_code == 429

    # t=161 — window has expired; should succeed again
    fake_now[0] = 161.0
    r3 = client.post("/api/search", json=_SEARCH_BODY,
                      headers={"X-Forwarded-For": "9.9.9.9"})
    assert r3.status_code == 200


# ---------------------------------------------------------------------------
# Stream endpoint 429 before streaming starts
# ---------------------------------------------------------------------------

def test_stream_endpoint_429_before_streaming(client, monkeypatch):
    """POST /api/search/stream returns 429 before any streaming begins."""
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "SEARCH_RATE_PER_MIN", 1)
    monkeypatch.setattr(appmod, "RATE_LIMIT_WINDOW", 60)
    monkeypatch.setattr(appmod, "run_search", lambda **kw: {"cells": [], "recommendation": None})
    appmod._rate_state.clear()

    r1 = client.post("/api/search/stream", json=_SEARCH_BODY,
                      headers={"X-Forwarded-For": "11.22.33.44"})
    assert r1.status_code == 200  # first passes (valid body is required)

    r2 = client.post("/api/search/stream", json=_SEARCH_BODY,
                      headers={"X-Forwarded-For": "11.22.33.44"})
    assert r2.status_code == 429
    # Response must be JSON with error key (not a streaming body)
    body = r2.get_json()
    assert "rate limit exceeded" in body.get("error", "").lower()


# ---------------------------------------------------------------------------
# Export endpoints in "search" bucket
# ---------------------------------------------------------------------------

def test_export_csv_rate_limited(client, monkeypatch):
    """POST /api/export/csv is in the search bucket."""
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "SEARCH_RATE_PER_MIN", 1)
    monkeypatch.setattr(appmod, "RATE_LIMIT_WINDOW", 60)
    monkeypatch.setattr(appmod, "run_search", lambda **kw: {"cells": [], "recommendation": None})
    import export as exportmod
    monkeypatch.setattr(exportmod, "render_csv", lambda r: "city,iata\n")
    appmod._rate_state.clear()

    r1 = client.post("/api/export/csv", json=_SEARCH_BODY,
                      headers={"X-Forwarded-For": "20.0.0.1"})
    assert r1.status_code == 200

    r2 = client.post("/api/export/csv", json=_SEARCH_BODY,
                      headers={"X-Forwarded-For": "20.0.0.1"})
    assert r2.status_code == 429


def test_export_pdf_rate_limited(client, monkeypatch):
    """POST /api/export/pdf is in the search bucket."""
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "SEARCH_RATE_PER_MIN", 1)
    monkeypatch.setattr(appmod, "RATE_LIMIT_WINDOW", 60)
    monkeypatch.setattr(appmod, "run_search", lambda **kw: {"cells": [], "recommendation": None})
    import export as exportmod
    monkeypatch.setattr(exportmod, "render_pdf", lambda r: b"%PDF-1.4")
    appmod._rate_state.clear()

    r1 = client.post("/api/export/pdf", json=_SEARCH_BODY,
                      headers={"X-Forwarded-For": "21.0.0.1"})
    assert r1.status_code == 200

    r2 = client.post("/api/export/pdf", json=_SEARCH_BODY,
                      headers={"X-Forwarded-For": "21.0.0.1"})
    assert r2.status_code == 429


# ---------------------------------------------------------------------------
# API bucket endpoints
# ---------------------------------------------------------------------------

def test_api_bucket_top_cities_limited(client, monkeypatch):
    """POST /api/top-cities is in the api bucket."""
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "API_RATE_PER_MIN", 2)
    monkeypatch.setattr(appmod, "RATE_LIMIT_WINDOW", 60)
    monkeypatch.setattr(appmod, "top_cities", lambda c, n: [{"city": "Paris", "iata": "CDG", "optional": False}])
    appmod._rate_state.clear()

    r1 = client.post("/api/top-cities", json={"country": "France", "n": 3},
                      headers={"X-Forwarded-For": "30.0.0.1"})
    assert r1.status_code == 200

    r2 = client.post("/api/top-cities", json={"country": "France", "n": 3},
                      headers={"X-Forwarded-For": "30.0.0.1"})
    assert r2.status_code == 200

    r3 = client.post("/api/top-cities", json={"country": "France", "n": 3},
                      headers={"X-Forwarded-For": "30.0.0.1"})
    assert r3.status_code == 429
    assert "Retry-After" in r3.headers


def test_api_bucket_suggest_limited(client, monkeypatch):
    """GET /api/suggest is in the api bucket."""
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "API_RATE_PER_MIN", 1)
    monkeypatch.setattr(appmod, "RATE_LIMIT_WINDOW", 60)
    appmod._rate_state.clear()

    r1 = client.get("/api/suggest?q=Paris", headers={"X-Forwarded-For": "31.0.0.1"})
    assert r1.status_code == 200

    r2 = client.get("/api/suggest?q=Paris", headers={"X-Forwarded-For": "31.0.0.1"})
    assert r2.status_code == 429


def test_api_bucket_resolve_limited(client, monkeypatch):
    """POST /api/resolve is in the api bucket."""
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "API_RATE_PER_MIN", 1)
    monkeypatch.setattr(appmod, "RATE_LIMIT_WINDOW", 60)
    monkeypatch.setattr(appmod, "resolve_airport", lambda city: "CDG")
    appmod._rate_state.clear()

    r1 = client.post("/api/resolve", json={"city": "Paris"},
                      headers={"X-Forwarded-For": "32.0.0.1"})
    assert r1.status_code == 200

    r2 = client.post("/api/resolve", json={"city": "Paris"},
                      headers={"X-Forwarded-For": "32.0.0.1"})
    assert r2.status_code == 429


def test_api_bucket_watch_post_limited(client, monkeypatch):
    """POST /api/watch is in the api bucket."""
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "API_RATE_PER_MIN", 1)
    monkeypatch.setattr(appmod, "RATE_LIMIT_WINDOW", 60)

    fake_db = type("DB", (), {
        "add_watch": lambda self, **kw: 42,
        "close": lambda self: None,
        "list_watches": lambda self, active_only=False: [],
        "remove_watch": lambda self, wid: None,
    })()
    monkeypatch.setattr(appmod, "_watch_db", lambda: fake_db)
    appmod._rate_state.clear()

    r1 = client.post("/api/watch", json=_WATCH_BODY,
                      headers={"X-Forwarded-For": "33.0.0.1"})
    assert r1.status_code == 200

    r2 = client.post("/api/watch", json=_WATCH_BODY,
                      headers={"X-Forwarded-For": "33.0.0.1"})
    assert r2.status_code == 429


def test_api_bucket_watch_get_limited(client, monkeypatch):
    """GET /api/watch is in the api bucket."""
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "API_RATE_PER_MIN", 1)
    monkeypatch.setattr(appmod, "RATE_LIMIT_WINDOW", 60)

    fake_db = type("DB", (), {
        "list_watches": lambda self: [],
        "close": lambda self: None,
    })()
    monkeypatch.setattr(appmod, "_watch_db", lambda: fake_db)
    appmod._rate_state.clear()

    r1 = client.get("/api/watch", headers={"X-Forwarded-For": "34.0.0.1"})
    assert r1.status_code == 200

    r2 = client.get("/api/watch", headers={"X-Forwarded-For": "34.0.0.1"})
    assert r2.status_code == 429


def test_api_bucket_watch_delete_limited(client, monkeypatch):
    """DELETE /api/watch/<id> is in the api bucket."""
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "API_RATE_PER_MIN", 1)
    monkeypatch.setattr(appmod, "RATE_LIMIT_WINDOW", 60)

    fake_db = type("DB", (), {
        "remove_watch": lambda self, wid: None,
        "close": lambda self: None,
    })()
    monkeypatch.setattr(appmod, "_watch_db", lambda: fake_db)
    appmod._rate_state.clear()

    r1 = client.delete("/api/watch/1", headers={"X-Forwarded-For": "35.0.0.1"})
    assert r1.status_code == 200

    r2 = client.delete("/api/watch/1", headers={"X-Forwarded-For": "35.0.0.1"})
    assert r2.status_code == 429


# ---------------------------------------------------------------------------
# Buckets are independent
# ---------------------------------------------------------------------------

def test_search_bucket_does_not_pollute_api_bucket(client, monkeypatch):
    """Consuming the search bucket should not affect the api bucket and vice versa."""
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "SEARCH_RATE_PER_MIN", 1)
    monkeypatch.setattr(appmod, "API_RATE_PER_MIN", 10)
    monkeypatch.setattr(appmod, "RATE_LIMIT_WINDOW", 60)
    monkeypatch.setattr(appmod, "run_search", lambda **kw: {"cells": [], "recommendation": None})
    monkeypatch.setattr(appmod, "top_cities", lambda c, n: [{"city": "Paris", "iata": "CDG", "optional": False}])
    appmod._rate_state.clear()

    ip_hdr = {"X-Forwarded-For": "40.0.0.1"}

    # Exhaust search bucket
    client.post("/api/search", json=_SEARCH_BODY, headers=ip_hdr)
    r_search = client.post("/api/search", json=_SEARCH_BODY, headers=ip_hdr)
    assert r_search.status_code == 429

    # api bucket should still work
    r_api = client.post("/api/top-cities", json={"country": "France", "n": 3}, headers=ip_hdr)
    assert r_api.status_code == 200


# ---------------------------------------------------------------------------
# Retry-After accuracy
# ---------------------------------------------------------------------------

def test_retry_after_reflects_window(client, monkeypatch):
    """Retry-After header should reflect remaining time in sliding window."""
    fake_now = [1000.0]

    def mock_time():
        return fake_now[0]

    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "SEARCH_RATE_PER_MIN", 1)
    monkeypatch.setattr(appmod, "RATE_LIMIT_WINDOW", 60)
    monkeypatch.setattr(appmod, "run_search", lambda **kw: {"cells": [], "recommendation": None})
    monkeypatch.setattr(appmod, "_rate_time", mock_time)
    appmod._rate_state.clear()

    # t=1000: consume the slot
    client.post("/api/search", json=_SEARCH_BODY, headers={"X-Forwarded-For": "50.0.0.1"})

    # t=1030: advance time 30s into the 60s window
    fake_now[0] = 1030.0
    r = client.post("/api/search", json=_SEARCH_BODY, headers={"X-Forwarded-For": "50.0.0.1"})
    assert r.status_code == 429
    retry_after = int(r.headers["Retry-After"])
    # Oldest timestamp is at t=1000; window ends at 1000+60=1060; now=1030 → ~30s remaining
    assert 25 <= retry_after <= 35, f"Unexpected Retry-After: {retry_after}"


# ---------------------------------------------------------------------------
# Zero / negative limit: empty-bucket guard (codex P2)
# ---------------------------------------------------------------------------

def test_search_bucket_zero_limit_returns_429_not_500(client, monkeypatch):
    """A limit of 0 effectively disables the endpoint: the FIRST request hits
    the 429 branch with an empty bucket. It must return a controlled 429 with
    Retry-After=window, not an IndexError -> 500."""
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "SEARCH_RATE_PER_MIN", 0)
    monkeypatch.setattr(appmod, "RATE_LIMIT_WINDOW", 60)
    monkeypatch.setattr(appmod, "run_search", lambda **kw: {"cells": [], "recommendation": None})
    appmod._rate_state.clear()

    r = client.post("/api/search", json=_SEARCH_BODY,
                    headers={"X-Forwarded-For": "60.0.0.1"})
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) == 60


def test_api_bucket_zero_limit_returns_429_not_500(client, monkeypatch):
    """Same empty-bucket guard for the api bucket at limit 0."""
    monkeypatch.setattr(appmod, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(appmod, "API_RATE_PER_MIN", 0)
    monkeypatch.setattr(appmod, "RATE_LIMIT_WINDOW", 60)
    monkeypatch.setattr(appmod, "top_cities", lambda c, n: [{"city": "Paris", "iata": "CDG", "optional": False}])
    appmod._rate_state.clear()

    r = client.post("/api/top-cities", json={"country": "France", "n": 3},
                    headers={"X-Forwarded-For": "61.0.0.1"})
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) == 60
