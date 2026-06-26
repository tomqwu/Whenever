"""
Unit tests for disk-persisted fare cache (#42).

The fare cache is in-memory (TTL-keyed by a tuple) but also persists real fares to
a JSON file so they survive a restart within FARE_CACHE_TTL. JSON can't key on
tuples, so records are stored as a list of {"key": [...], "expiry": ts, "result": {}}.

Real fares only: no-data sentinels are never persisted. Persistence is disabled when
FARE_CACHE_PATH is empty (memory-only) or FARE_CACHE_TTL <= 0 (no caching at all).

TDD: every test below was written before the implementation and verified RED first.
"""
import json
import os

import app as appmod

_ARGS = ("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
_REAL_FARE = {"cheapest_cad": 4200, "stops": 1, "nonstop_cad": None, "source": "travelpayouts"}
_NO_DATA = {"cheapest_cad": None, "stops": None, "nonstop_cad": None,
            "source": "no-data", "duration_min": None,
            "nonstop_duration_min": None, "airlines": None,
            "nonstop_airlines": None, "layovers": None}


def _enable_persistence(monkeypatch, tmp_path, ttl=3600):
    """Point persistence at a tmp file and return that path."""
    path = tmp_path / "fare_cache.json"
    monkeypatch.setattr(appmod, "FARE_CACHE_PATH", str(path))
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", ttl)
    return path


# ---------------------------------------------------------------------------
# save -> load round-trip
# ---------------------------------------------------------------------------

def test_save_then_load_round_trip(monkeypatch, tmp_path):
    """Non-expired entries survive a save -> clear -> load with tuple keys intact."""
    path = _enable_persistence(monkeypatch, tmp_path)
    now = 1_000_000.0
    monkeypatch.setattr(appmod.time, "time", lambda: now)

    appmod._fare_cache[_ARGS] = (now + 3600, _REAL_FARE)
    with appmod._fare_cache_lock:
        appmod._save_fare_cache()

    assert path.exists()
    appmod._fare_cache.clear()
    appmod._load_fare_cache()

    assert _ARGS in appmod._fare_cache, "tuple key must be rebuilt on load"
    expiry, result = appmod._fare_cache[_ARGS]
    assert expiry == now + 3600
    assert result == _REAL_FARE


def test_load_drops_expired_entries(monkeypatch, tmp_path):
    """Entries whose expiry <= now are dropped on load; fresh ones survive."""
    path = _enable_persistence(monkeypatch, tmp_path)
    now = 1_000_000.0
    monkeypatch.setattr(appmod.time, "time", lambda: now)

    fresh = ("YYZ", "LHR", "2026-12-12", "2027-01-04", 2, 0)
    # Hand-write a file with one expired and one fresh record.
    records = [
        {"key": list(_ARGS), "expiry": now - 1, "result": _REAL_FARE},          # expired
        {"key": list(fresh), "expiry": now + 3600, "result": _REAL_FARE},       # fresh
    ]
    path.write_text(json.dumps(records), encoding="utf-8")

    appmod._fare_cache.clear()
    appmod._load_fare_cache()

    assert _ARGS not in appmod._fare_cache, "expired entry must be dropped on load"
    assert fresh in appmod._fare_cache, "fresh entry must be restored"


def test_save_prunes_expired_entries(monkeypatch, tmp_path):
    """A save prunes expired entries from memory and never writes them to disk."""
    path = _enable_persistence(monkeypatch, tmp_path)
    now = 1_000_000.0
    monkeypatch.setattr(appmod.time, "time", lambda: now)

    expired_key = ("YYZ", "CDG", "2026-12-12", "2027-01-04", 2, 0)
    appmod._fare_cache[expired_key] = (now - 1, _REAL_FARE)
    appmod._fare_cache[_ARGS] = (now + 3600, _REAL_FARE)

    with appmod._fare_cache_lock:
        appmod._save_fare_cache()

    assert expired_key not in appmod._fare_cache, "expired entry pruned from memory on save"
    records = json.loads(path.read_text(encoding="utf-8"))
    keys = [tuple(r["key"]) for r in records]
    assert list(_ARGS) in [r["key"] for r in records]
    assert tuple(expired_key) not in keys


# ---------------------------------------------------------------------------
# get_fare persists real results; not no-data
# ---------------------------------------------------------------------------

def test_get_fare_persists_real_result(monkeypatch, tmp_path):
    """A real priced result is written to the tmp file as a record."""
    path = _enable_persistence(monkeypatch, tmp_path)
    monkeypatch.setattr(appmod, "amadeus_fare", lambda *a: _REAL_FARE)
    monkeypatch.setattr(appmod, "travelpayouts_fare", lambda *a: None)

    appmod.get_fare(*_ARGS)

    assert path.exists()
    records = json.loads(path.read_text(encoding="utf-8"))
    assert len(records) == 1
    assert records[0]["key"] == list(_ARGS)
    assert records[0]["result"] == _REAL_FARE


def test_get_fare_does_not_persist_no_data(monkeypatch, tmp_path):
    """A no-data result must never be written to disk."""
    path = _enable_persistence(monkeypatch, tmp_path)
    monkeypatch.setattr(appmod, "amadeus_fare", lambda *a: None)
    monkeypatch.setattr(appmod, "travelpayouts_fare", lambda *a: None)

    result = appmod.get_fare(*_ARGS)

    assert result == _NO_DATA
    assert not path.exists(), "no-data result must not create the cache file"


# ---------------------------------------------------------------------------
# corrupt / missing file
# ---------------------------------------------------------------------------

def test_load_corrupt_file_starts_empty(monkeypatch, tmp_path):
    """Garbage JSON -> load starts empty without crashing."""
    path = _enable_persistence(monkeypatch, tmp_path)
    path.write_text("{not valid json!!!", encoding="utf-8")

    appmod._fare_cache.clear()
    appmod._load_fare_cache()  # must not raise

    assert appmod._fare_cache == {}


def test_load_non_list_file_starts_empty(monkeypatch, tmp_path):
    """Valid JSON but wrong shape (a dict, not a list) -> empty, no crash."""
    path = _enable_persistence(monkeypatch, tmp_path)
    path.write_text(json.dumps({"oops": 1}), encoding="utf-8")

    appmod._fare_cache.clear()
    appmod._load_fare_cache()

    assert appmod._fare_cache == {}


def test_load_malformed_record_starts_empty(monkeypatch, tmp_path):
    """A record missing required keys -> empty (KeyError caught), no crash."""
    path = _enable_persistence(monkeypatch, tmp_path)
    path.write_text(json.dumps([{"key": list(_ARGS)}]), encoding="utf-8")  # no expiry/result

    appmod._fare_cache.clear()
    appmod._load_fare_cache()

    assert appmod._fare_cache == {}


def test_load_missing_file_starts_empty(monkeypatch, tmp_path):
    """No file on disk -> load is a no-op, cache stays empty."""
    _enable_persistence(monkeypatch, tmp_path)  # file never created

    appmod._fare_cache.clear()
    appmod._load_fare_cache()

    assert appmod._fare_cache == {}


# ---------------------------------------------------------------------------
# disabled persistence
# ---------------------------------------------------------------------------

def test_empty_path_writes_no_file(monkeypatch, tmp_path):
    """FARE_CACHE_PATH='' -> memory-only; get_fare still caches but writes nothing."""
    monkeypatch.setattr(appmod, "FARE_CACHE_PATH", "")
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", 3600)
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}

    def fake(*a):
        calls["n"] += 1
        return _REAL_FARE

    monkeypatch.setattr(appmod, "amadeus_fare", fake)
    monkeypatch.setattr(appmod, "travelpayouts_fare", lambda *a: None)

    appmod.get_fare(*_ARGS)
    appmod.get_fare(*_ARGS)  # served from memory

    assert calls["n"] == 1, "memory cache still works with persistence off"
    assert list(tmp_path.iterdir()) == [], "no file written when path is empty"


def test_whitespace_path_writes_no_file(monkeypatch, tmp_path):
    """A blank/whitespace FARE_CACHE_PATH is treated as disabled."""
    monkeypatch.setattr(appmod, "FARE_CACHE_PATH", "   ")
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", 3600)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(appmod, "amadeus_fare", lambda *a: _REAL_FARE)
    monkeypatch.setattr(appmod, "travelpayouts_fare", lambda *a: None)

    appmod.get_fare(*_ARGS)

    assert list(tmp_path.iterdir()) == [], "whitespace path must not write a file"


def test_ttl_zero_disables_persistence(monkeypatch, tmp_path):
    """FARE_CACHE_TTL <= 0 disables caching AND persistence even with a path set."""
    path = _enable_persistence(monkeypatch, tmp_path, ttl=0)
    calls = {"n": 0}

    def fake(*a):
        calls["n"] += 1
        return _REAL_FARE

    monkeypatch.setattr(appmod, "amadeus_fare", fake)
    monkeypatch.setattr(appmod, "travelpayouts_fare", lambda *a: None)

    appmod.get_fare(*_ARGS)
    appmod.get_fare(*_ARGS)

    assert calls["n"] == 2, "TTL<=0 disables caching"
    assert not path.exists(), "TTL<=0 must not persist to disk"
    assert not appmod._persistence_enabled()


# ---------------------------------------------------------------------------
# atomic write — no temp file left behind
# ---------------------------------------------------------------------------

def test_atomic_write_leaves_no_temp_file(monkeypatch, tmp_path):
    """After a save only the cache file exists; the temp file is os.replace'd away."""
    path = _enable_persistence(monkeypatch, tmp_path)
    appmod._fare_cache[_ARGS] = (appmod.time.time() + 3600, _REAL_FARE)

    with appmod._fare_cache_lock:
        appmod._save_fare_cache()

    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["fare_cache.json"], f"expected only the cache file, got {names}"
    assert not any(".tmp" in n for n in names)


def test_save_replace_failure_cleans_up_temp(monkeypatch, tmp_path):
    """If os.replace raises OSError, the warning path runs and the temp file is removed."""
    path = _enable_persistence(monkeypatch, tmp_path)
    appmod._fare_cache[_ARGS] = (appmod.time.time() + 3600, _REAL_FARE)

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(appmod.os, "replace", boom)

    with appmod._fare_cache_lock:
        appmod._save_fare_cache()  # must not raise

    # Final file never created (replace failed); temp file cleaned up by os.remove.
    assert not path.exists()
    assert list(tmp_path.iterdir()) == [], "temp file must be removed after replace failure"


def test_save_open_failure_is_swallowed(monkeypatch, tmp_path):
    """If the temp-file open itself fails, the OSError is logged, not raised.

    Points FARE_CACHE_PATH inside a non-existent directory so open(tmp) raises;
    os.remove of the never-created temp then also raises and is swallowed.
    """
    missing_dir = tmp_path / "does_not_exist"
    monkeypatch.setattr(appmod, "FARE_CACHE_PATH", str(missing_dir / "cache.json"))
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", 3600)
    appmod._fare_cache[_ARGS] = (appmod.time.time() + 3600, _REAL_FARE)

    with appmod._fare_cache_lock:
        appmod._save_fare_cache()  # must not raise

    assert not missing_dir.exists()


def test_lock_is_a_real_lock():
    """The mutation/IO guard must be a threading lock (concurrency safety, #33)."""
    import threading
    assert isinstance(appmod._fare_cache_lock, type(threading.Lock()))


def test_load_restores_then_get_fare_is_cache_hit(monkeypatch, tmp_path):
    """End-to-end restart simulation: persisted fare is served without a provider call."""
    path = _enable_persistence(monkeypatch, tmp_path)
    now = 1_000_000.0
    monkeypatch.setattr(appmod.time, "time", lambda: now)

    # "Run 1": store + persist.
    appmod._fare_cache[_ARGS] = (now + 3600, _REAL_FARE)
    with appmod._fare_cache_lock:
        appmod._save_fare_cache()

    # "Run 2": fresh process — clear memory, load from disk.
    appmod._fare_cache.clear()
    appmod._load_fare_cache()

    calls = {"n": 0}

    def fake(*a):
        calls["n"] += 1
        return _REAL_FARE

    monkeypatch.setattr(appmod, "amadeus_fare", fake)
    monkeypatch.setattr(appmod, "travelpayouts_fare", lambda *a: None)

    result = appmod.get_fare(*_ARGS)
    assert result == _REAL_FARE
    assert calls["n"] == 0, "persisted fare must be served from cache, no provider call"
