"""
Unit tests for disk-persisted fare cache (#42).

The fare cache is in-memory (keyed by a tuple) but also persists real fares to a JSON
file so they survive a restart while still fresh. Each record stores the fare's FETCH
time (not an absolute expiry), so freshness is judged against the CURRENT
FARE_CACHE_TTL on every check/load: records are a list of
{"key": [...], "fetched": ts, "result": {}}. Lowering FARE_CACHE_TTL between runs
immediately drops fares that are now stale.

Real fares only: no-data sentinels are never persisted. Persistence is disabled when
FARE_CACHE_PATH is empty (memory-only) or FARE_CACHE_TTL <= 0 (no caching at all).

TDD: every test below was written before the implementation and verified RED first.
"""
import json
import os

import app as appmod

# 7-element key: (origin, dest, dep, ret, adults, children, compare). The trailing
# compare flag (#43) keeps compared vs ordered-fallback fares in separate cache slots.
_ARGS = ("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0, False)
_REAL_FARE = {"cheapest_cad": 4200, "stops": 1, "nonstop_cad": None, "source": "travelpayouts"}
_NO_DATA = {"cheapest_cad": None, "stops": None, "nonstop_cad": None,
            "source": "no-data", "duration_min": None,
            "nonstop_duration_min": None, "airlines": None,
            "nonstop_airlines": None, "layovers": None,
            "alternatives": None}


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
    """Still-fresh entries survive a save -> clear -> load with tuple keys intact."""
    path = _enable_persistence(monkeypatch, tmp_path)
    now = 1_000_000.0
    monkeypatch.setattr(appmod.time, "time", lambda: now)

    appmod._fare_cache[_ARGS] = (now, _REAL_FARE)
    with appmod._fare_cache_lock:
        appmod._save_fare_cache()

    assert path.exists()
    appmod._fare_cache.clear()
    appmod._load_fare_cache()

    assert _ARGS in appmod._fare_cache, "tuple key must be rebuilt on load"
    fetched, result = appmod._fare_cache[_ARGS]
    assert fetched == now
    assert result == _REAL_FARE


def test_load_drops_stale_entries(monkeypatch, tmp_path):
    """Entries fetched longer ago than the current TTL are dropped; fresh ones survive."""
    path = _enable_persistence(monkeypatch, tmp_path, ttl=3600)
    now = 1_000_000.0
    monkeypatch.setattr(appmod.time, "time", lambda: now)

    fresh = ("YYZ", "LHR", "2026-12-12", "2027-01-04", 2, 0, False)
    # Hand-write a file with one stale (fetched > TTL ago) and one fresh record.
    records = [
        {"key": list(_ARGS), "fetched": now - 3601, "result": _REAL_FARE},   # stale
        {"key": list(fresh), "fetched": now - 60, "result": _REAL_FARE},     # fresh
    ]
    path.write_text(json.dumps(records), encoding="utf-8")

    appmod._fare_cache.clear()
    appmod._load_fare_cache()

    assert _ARGS not in appmod._fare_cache, "stale entry must be dropped on load"
    assert fresh in appmod._fare_cache, "fresh entry must be restored"


def test_load_revalidates_against_current_ttl(monkeypatch, tmp_path):
    """THE FIX: the same file is stale under a lowered TTL but fresh under a raised one.

    A fare fetched ~1 hour ago: with FARE_CACHE_TTL=300 (5 min) it is now stale and
    dropped; with FARE_CACHE_TTL=7200 (2 h) it is still fresh and loads. Freshness is
    judged against the CURRENT TTL, not an absolute expiry written at fetch time.
    """
    path = tmp_path / "fare_cache.json"
    now = 1_000_000.0
    monkeypatch.setattr(appmod.time, "time", lambda: now)
    fetched_an_hour_ago = now - 3600
    records = [{"key": list(_ARGS), "fetched": fetched_an_hour_ago, "result": _REAL_FARE}]
    path.write_text(json.dumps(records), encoding="utf-8")

    # Lowered TTL (5 min): the 1-hour-old fare is stale -> dropped.
    monkeypatch.setattr(appmod, "FARE_CACHE_PATH", str(path))
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", 300)
    appmod._fare_cache.clear()
    appmod._load_fare_cache()
    assert _ARGS not in appmod._fare_cache, "lowered TTL must drop the now-stale fare"

    # Raised TTL (2 h): the SAME file's fare is still fresh -> loads.
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", 7200)
    appmod._fare_cache.clear()
    appmod._load_fare_cache()
    assert _ARGS in appmod._fare_cache, "raised TTL must keep the still-fresh fare"


def test_save_prunes_stale_entries(monkeypatch, tmp_path):
    """A save prunes entries stale under the current TTL and never writes them to disk."""
    path = _enable_persistence(monkeypatch, tmp_path, ttl=3600)
    now = 1_000_000.0
    monkeypatch.setattr(appmod.time, "time", lambda: now)

    stale_key = ("YYZ", "CDG", "2026-12-12", "2027-01-04", 2, 0, False)
    appmod._fare_cache[stale_key] = (now - 3601, _REAL_FARE)   # fetched > TTL ago
    appmod._fare_cache[_ARGS] = (now - 60, _REAL_FARE)         # fresh

    with appmod._fare_cache_lock:
        appmod._save_fare_cache()

    assert stale_key not in appmod._fare_cache, "stale entry pruned from memory on save"
    records = json.loads(path.read_text(encoding="utf-8"))
    keys = [tuple(r["key"]) for r in records]
    assert list(_ARGS) in [r["key"] for r in records]
    assert tuple(stale_key) not in keys


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
    assert isinstance(records[0]["fetched"], (int, float)), "fetch time must be persisted"
    assert "expiry" not in records[0], "absolute expiry must not be persisted"


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
    path.write_text(json.dumps([{"key": list(_ARGS)}]), encoding="utf-8")  # no fetched/result

    appmod._fare_cache.clear()
    appmod._load_fare_cache()

    assert appmod._fare_cache == {}


def test_load_old_expiry_format_file_dropped(monkeypatch, tmp_path):
    """A cache file from the previous (absolute-``expiry``) format is dropped, not crashed.

    The fetch-time format is new/unreleased; an old record carries ``expiry`` but no
    ``fetched``. ``rec.get('fetched')`` is None -> fails the number check -> the record
    is skipped. The load must complete cleanly (empty cache), never raise.
    """
    path = _enable_persistence(monkeypatch, tmp_path)
    now = 1_000_000.0
    monkeypatch.setattr(appmod.time, "time", lambda: now)
    # Old format: absolute future expiry, no "fetched" key.
    records = [{"key": list(_ARGS), "expiry": now + 3600, "result": _REAL_FARE}]
    path.write_text(json.dumps(records), encoding="utf-8")

    appmod._fare_cache.clear()
    appmod._load_fare_cache()  # must not raise

    assert appmod._fare_cache == {}, "old expiry-only records must be dropped on load"


def test_load_missing_file_starts_empty(monkeypatch, tmp_path):
    """No file on disk -> load is a no-op, cache stays empty."""
    _enable_persistence(monkeypatch, tmp_path)  # file never created

    appmod._fare_cache.clear()
    appmod._load_fare_cache()

    assert appmod._fare_cache == {}


# ---------------------------------------------------------------------------
# per-record validation: mixed valid + invalid records
# ---------------------------------------------------------------------------

_KEY_B = ("YYZ", "PEK", "2026-12-15", "2027-01-10", 2, 0, False)  # result is a string
_KEY_C = ("YYZ", "CAN", "2026-12-20", "2027-01-15", 1, 0, False)  # result no-data dict
_KEY_D = ("YYZ",)                                            # bad/short key placeholder


def test_load_per_record_validation_only_valid_loaded(monkeypatch, tmp_path):
    """A mix of valid and invalid records: only the real-priced record is loaded.

    Records:
      (a) valid real-priced entry  -> loaded
      (b) result is a string       -> dropped
      (c) result is no-data dict   -> dropped
      (d) key has wrong length     -> dropped
      (e) stale entry              -> dropped
    """
    path = _enable_persistence(monkeypatch, tmp_path, ttl=3600)
    now = 1_000_000.0
    monkeypatch.setattr(appmod.time, "time", lambda: now)

    records = [
        # (a) valid
        {"key": list(_ARGS), "fetched": now - 60, "result": _REAL_FARE},
        # (b) result is a string
        {"key": list(_KEY_B), "fetched": now - 60, "result": "some string"},
        # (c) result is no-data dict (cheapest_cad absent)
        {"key": list(_KEY_C), "fetched": now - 60,
         "result": {"cheapest_cad": None, "stops": None}},
        # (d) bad key — only 1 element
        {"key": ["YYZ"], "fetched": now - 60, "result": _REAL_FARE},
        # (e) stale (fetched > TTL ago) — valid 7-element key, dropped for staleness
        {"key": ["YYZ", "MEL", "2026-12-12", "2027-01-04", 2, 1, False],
         "fetched": now - 3601, "result": _REAL_FARE},
    ]
    path.write_text(json.dumps(records), encoding="utf-8")

    appmod._fare_cache.clear()
    appmod._load_fare_cache()

    # Only record (a) should be in the cache
    assert _ARGS in appmod._fare_cache, "valid real-priced record must be loaded"
    assert _KEY_B not in appmod._fare_cache, "string result must be dropped"
    assert _KEY_C not in appmod._fare_cache, "no-data dict result must be dropped"
    assert len(appmod._fare_cache) == 1, "only the one valid record must be in cache"


def test_load_string_result_causes_real_fetch(monkeypatch, tmp_path):
    """A poisoned record (string result) is dropped so get_fare does a real fetch."""
    path = _enable_persistence(monkeypatch, tmp_path)
    now = 1_000_000.0
    monkeypatch.setattr(appmod.time, "time", lambda: now)

    records = [
        {"key": list(_KEY_B), "fetched": now - 60, "result": "poison"},
    ]
    path.write_text(json.dumps(records), encoding="utf-8")

    appmod._fare_cache.clear()
    appmod._load_fare_cache()

    assert _KEY_B not in appmod._fare_cache

    calls = {"n": 0}

    def fake_provider(*a):
        calls["n"] += 1
        return _REAL_FARE

    monkeypatch.setattr(appmod, "amadeus_fare", fake_provider)
    monkeypatch.setattr(appmod, "travelpayouts_fare", lambda *a: None)

    result = appmod.get_fare(*_KEY_B)
    assert result == _REAL_FARE
    assert calls["n"] == 1, "poisoned cache must not suppress provider call"


def test_load_no_data_result_causes_real_fetch(monkeypatch, tmp_path):
    """A no-data dict (cheapest_cad None) is dropped so get_fare does a real fetch."""
    path = _enable_persistence(monkeypatch, tmp_path)
    now = 1_000_000.0
    monkeypatch.setattr(appmod.time, "time", lambda: now)

    records = [
        {"key": list(_KEY_C), "fetched": now - 60,
         "result": {"cheapest_cad": None, "stops": None}},
    ]
    path.write_text(json.dumps(records), encoding="utf-8")

    appmod._fare_cache.clear()
    appmod._load_fare_cache()

    assert _KEY_C not in appmod._fare_cache

    calls = {"n": 0}

    def fake_provider(*a):
        calls["n"] += 1
        return _REAL_FARE

    monkeypatch.setattr(appmod, "amadeus_fare", fake_provider)
    monkeypatch.setattr(appmod, "travelpayouts_fare", lambda *a: None)

    result = appmod.get_fare(*_KEY_C)
    assert result == _REAL_FARE
    assert calls["n"] == 1, "no-data dict in cache must not suppress provider call"


def test_load_bad_key_length_dropped(monkeypatch, tmp_path):
    """A record with a key that isn't exactly 7 elements is dropped silently."""
    path = _enable_persistence(monkeypatch, tmp_path)
    now = 1_000_000.0
    monkeypatch.setattr(appmod.time, "time", lambda: now)

    records = [
        # 6 elements (old pre-#43 layout, missing the compare flag)
        {"key": ["YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0],
         "fetched": now - 60, "result": _REAL_FARE},
        # 8 elements (extra)
        {"key": ["YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0, False, "extra"],
         "fetched": now - 60, "result": _REAL_FARE},
    ]
    path.write_text(json.dumps(records), encoding="utf-8")

    appmod._fare_cache.clear()
    appmod._load_fare_cache()

    assert appmod._fare_cache == {}, "bad-length keys must be dropped"


def test_load_non_dict_record_dropped(monkeypatch, tmp_path):
    """A list item that isn't a dict (e.g. a string or int) is skipped, no crash."""
    path = _enable_persistence(monkeypatch, tmp_path)
    now = 1_000_000.0
    monkeypatch.setattr(appmod.time, "time", lambda: now)

    # Mix: two non-dict items, then one valid real-priced record
    records_raw = json.dumps([
        "just a string",
        42,
        {"key": list(_ARGS), "fetched": now - 60, "result": _REAL_FARE},
    ])
    path.write_text(records_raw, encoding="utf-8")

    appmod._fare_cache.clear()
    appmod._load_fare_cache()

    assert _ARGS in appmod._fare_cache, "valid record after non-dict items must still load"
    assert len(appmod._fare_cache) == 1


def test_load_exception_in_record_skipped(monkeypatch, tmp_path):
    """If processing a record raises unexpectedly, it is skipped and load continues."""
    import json as _json

    path = _enable_persistence(monkeypatch, tmp_path)
    now = 1_000_000.0
    monkeypatch.setattr(appmod.time, "time", lambda: now)

    # Write a valid record; we'll inject an exception on the first iteration only
    records_raw = _json.dumps([
        {"key": list(_ARGS), "fetched": now - 60, "result": _REAL_FARE},
        {"key": list(_KEY_B), "fetched": now - 60, "result": _REAL_FARE},
    ])
    path.write_text(records_raw, encoding="utf-8")

    original_tuple = __builtins__["tuple"] if isinstance(__builtins__, dict) else tuple
    call_count = {"n": 0}

    def raising_tuple(iterable):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated unexpected error on first record")
        return original_tuple(iterable)

    monkeypatch.setattr(appmod, "tuple", raising_tuple, raising=False)

    # Patch tuple inside _load_fare_cache's scope via the app module's builtins
    import builtins as _builtins
    original = _builtins.tuple
    _builtins.tuple = raising_tuple

    try:
        appmod._fare_cache.clear()
        appmod._load_fare_cache()  # must not raise
    finally:
        _builtins.tuple = original

    # Second record should still have loaded
    assert _KEY_B in appmod._fare_cache, "second record must load despite first raising"


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
    appmod._fare_cache[_ARGS] = (appmod.time.time(), _REAL_FARE)

    with appmod._fare_cache_lock:
        appmod._save_fare_cache()

    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["fare_cache.json"], f"expected only the cache file, got {names}"
    assert not any(".tmp" in n for n in names)


def test_save_replace_failure_cleans_up_temp(monkeypatch, tmp_path):
    """If os.replace raises OSError, the warning path runs and the temp file is removed."""
    path = _enable_persistence(monkeypatch, tmp_path)
    appmod._fare_cache[_ARGS] = (appmod.time.time(), _REAL_FARE)

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
    appmod._fare_cache[_ARGS] = (appmod.time.time(), _REAL_FARE)

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
    appmod._fare_cache[_ARGS] = (now, _REAL_FARE)
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
