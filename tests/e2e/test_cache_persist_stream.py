"""E2E test for the fare-cache persistence freshness fix (#42, codex P2).

CLAUDE.md rule 5 requires unit AND e2e for every behaviour change. The unit tests
(tests/unit/test_cache_persist.py) cover _load/_save/get_fare internals; this test
drives the real Flask server over HTTP through /api/search/stream and asserts the
USER-VISIBLE effect of the fix:

Each persisted fare stores WHEN it was fetched and is revalidated against the CURRENT
FARE_CACHE_TTL on load. So a cache file whose only fare was fetched ~1 hour ago:

  * loaded under a LOWERED TTL (5 min) -> the fare is now stale -> dropped on load ->
    the live search must hit the provider (real fetch) to fill the cell; AND
  * loaded under a RAISED TTL (2 h)   -> the fare is still fresh -> restored on load ->
    the live search serves it from cache with ZERO provider calls.

Fully offline/deterministic: the real get_fare + _get_fare_uncached run, but the only
configured provider is a counting in-process stub (no network, no sleep).
"""
import json
import threading

import pytest
from werkzeug.serving import make_server

import app as appmod

from .conftest import _patch_common

# One destination, one date pair -> exactly one grid cell -> at most one fare lookup.
# adults=2/children=0 matches _search_args_from_body's defaults for this body.
_KEY = ("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
_SEARCH_BODY = {
    "origin": "YYZ",
    "destinations": [{"city": "Shanghai", "iata": "PVG"}],
    "dep_dates": ["2026-12-12"],
    "ret_dates": ["2027-01-04"],
}
_REAL_FARE = {
    "cheapest_cad": 4200, "stops": 1, "nonstop_cad": None,
    "source": "stub-provider", "book": "https://example.com/book",
    "duration_min": None, "nonstop_duration_min": None,
    "airlines": None, "nonstop_airlines": None, "layovers": None,
}


@pytest.fixture
def persist_cache_server(monkeypatch, tmp_path):
    """Live server with the REAL get_fare, a counting provider stub, and a cache file.

    Yields (base_url, calls, set_ttl): calls['n'] counts provider lookups; set_ttl(t)
    rewrites FARE_CACHE_TTL on the running app so the test can reload the same cache
    file under different TTLs.
    """
    real_get_fare = appmod.get_fare  # capture BEFORE _patch_common stubs it out
    _patch_common(monkeypatch)  # disables every real provider credential + stubs ollama
    monkeypatch.setattr(appmod, "top_cities",
                        lambda country, n=6: [{"city": "Shanghai", "iata": "PVG"}])
    # _patch_common replaces get_fare with a flat stub; restore the REAL cache-aware
    # one so persistence/freshness logic actually runs end-to-end over HTTP.
    monkeypatch.setattr(appmod, "get_fare", real_get_fare)

    # The ONLY fare source is this in-process counter (no network, no retry/sleep).
    calls = {"n": 0}

    def counting_provider(*_a, **_k):
        calls["n"] += 1
        return dict(_REAL_FARE)

    monkeypatch.setattr(appmod, "_get_fare_uncached", counting_provider)

    cache_path = tmp_path / "fare_cache.json"
    monkeypatch.setattr(appmod, "FARE_CACHE_PATH", str(cache_path))

    def set_ttl(seconds):
        monkeypatch.setattr(appmod, "FARE_CACHE_TTL", seconds)
        appmod._fare_cache.clear()      # simulate a fresh process
        appmod._load_fare_cache()        # reload the SAME file under the new TTL

    srv = make_server("127.0.0.1", 0, appmod.app)
    thread = threading.Thread(target=srv.serve_forever)
    thread.daemon = True
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}", calls, set_ttl, cache_path
    finally:
        srv.shutdown()
        thread.join()
        appmod._fare_cache.clear()


def _run_stream_search(page, base_url):
    """POST the search over HTTP and return the parsed cell price (or None)."""
    resp = page.request.post(f"{base_url}/api/search/stream", data=_SEARCH_BODY)
    assert resp.ok, f"stream must start OK, got {resp.status}"
    price = None
    for line in resp.text().splitlines():
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("type") == "cell":
            price = msg.get("cheapest_cad")
    return price


def test_lowered_ttl_drops_stale_persisted_fare_then_refetches(persist_cache_server, page):
    """A ~1h-old persisted fare is stale under a 5-min TTL: dropped on load, re-fetched."""
    base_url, calls, set_ttl, cache_path = persist_cache_server

    # Hand-write a cache file whose only fare was fetched ~1 hour ago.
    records = [{"key": list(_KEY), "fetched": appmod.time.time() - 3600, "result": _REAL_FARE}]
    cache_path.write_text(json.dumps(records), encoding="utf-8")

    set_ttl(300)  # 5 minutes -> the 1-hour-old fare is now stale and dropped on load
    assert _KEY not in appmod._fare_cache, "stale fare must be dropped under lowered TTL"

    price = _run_stream_search(page, base_url)
    assert price == 4200, "cell must show the freshly re-fetched real price"
    assert calls["n"] == 1, "lowered TTL must force a real provider fetch (cache miss)"


def test_raised_ttl_keeps_fresh_persisted_fare_no_provider_call(persist_cache_server, page):
    """The SAME ~1h-old fare is fresh under a 2h TTL: restored on load, served cache-only."""
    base_url, calls, set_ttl, cache_path = persist_cache_server

    records = [{"key": list(_KEY), "fetched": appmod.time.time() - 3600, "result": _REAL_FARE}]
    cache_path.write_text(json.dumps(records), encoding="utf-8")

    set_ttl(7200)  # 2 hours -> the 1-hour-old fare is still fresh and restored on load
    assert _KEY in appmod._fare_cache, "fresh fare must be restored under raised TTL"

    price = _run_stream_search(page, base_url)
    assert price == 4200, "cell must show the persisted real price"
    assert calls["n"] == 0, "raised TTL must serve the persisted fare with NO provider call"
