"""Unit tests for watch.py — WatchDB and check_all_watches.

All SQLite operations use `:memory:` so no files are left on disk.
All fare calls use injected `fare_fn` — no real network.
"""
import datetime
import sys
from unittest.mock import patch, MagicMock

import pytest

import watch as watchmod
from watch import WatchDB, check_all_watches


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    """In-memory WatchDB; closed after each test."""
    _db = WatchDB(":memory:")
    yield _db
    _db.close()


def _sample_watch(db, last_price=8000):
    """Add a watch and return its id."""
    return db.add_watch(
        origin="YYZ",
        dest_iata="PEK",
        dest_city="Beijing",
        dep_date="2026-12-14",
        ret_date="2027-01-04",
        adults=2,
        children=0,
        threshold_pct=25.0,
        last_price=last_price,
        last_source="travelpayouts",
        created_at="2026-06-23T00:00:00",
    )


# ---------------------------------------------------------------------------
# WatchDB — schema
# ---------------------------------------------------------------------------

def test_watchdb_creates_tables(db):
    cur = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {r[0] for r in cur.fetchall()}
    assert "watches" in tables
    assert "price_history" in tables


def test_watchdb_add_and_list(db):
    wid = _sample_watch(db)
    watches = db.list_watches()
    assert len(watches) == 1
    w = watches[0]
    assert w["id"] == wid
    assert w["origin"] == "YYZ"
    assert w["dest_iata"] == "PEK"
    assert w["dest_city"] == "Beijing"
    assert w["dep_date"] == "2026-12-14"
    assert w["ret_date"] == "2027-01-04"
    assert w["adults"] == 2
    assert w["children"] == 0
    assert w["threshold_pct"] == 25.0
    assert w["last_price"] == 8000
    assert w["last_source"] == "travelpayouts"
    assert w["active"] == 1


def test_watchdb_add_returns_integer_id(db):
    wid = _sample_watch(db)
    assert isinstance(wid, int)
    assert wid > 0


def test_watchdb_list_active_only(db):
    _sample_watch(db)
    db._conn.execute("UPDATE watches SET active=0 WHERE 1")
    db._conn.commit()
    assert db.list_watches(active_only=True) == []
    assert len(db.list_watches(active_only=False)) == 1


def test_watchdb_remove_sets_inactive(db):
    wid = _sample_watch(db)
    db.remove_watch(wid)
    assert db.list_watches(active_only=True) == []
    rows = db.list_watches(active_only=False)
    assert len(rows) == 1
    assert rows[0]["active"] == 0


def test_watchdb_update_price_inserts_history(db):
    wid = _sample_watch(db, last_price=8000)
    db.update_price(wid, 7000, "kiwi", "https://kiwi.com/book", "2026-06-23T10:00:00")
    # history row inserted
    cur = db._conn.execute(
        "SELECT watch_id, price, source, book, checked_at FROM price_history WHERE watch_id=?",
        (wid,),
    )
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][1] == 7000
    assert rows[0][2] == "kiwi"
    assert rows[0][3] == "https://kiwi.com/book"
    assert rows[0][4] == "2026-06-23T10:00:00"


def test_watchdb_update_price_updates_last_price(db):
    wid = _sample_watch(db, last_price=8000)
    db.update_price(wid, 7000, "kiwi", "https://kiwi.com/book", "2026-06-23T10:00:00")
    w = db.list_watches()[0]
    assert w["last_price"] == 7000
    assert w["last_source"] == "kiwi"


def test_watchdb_update_price_with_null_price(db):
    """Null price is stored in history but does NOT overwrite last_price."""
    wid = _sample_watch(db, last_price=8000)
    db.update_price(wid, None, "no-data", None, "2026-06-23T10:00:00")
    # history row has None price
    cur = db._conn.execute(
        "SELECT price FROM price_history WHERE watch_id=?", (wid,)
    )
    assert cur.fetchone()[0] is None
    # last_price in watches stays at 8000
    w = db.list_watches()[0]
    assert w["last_price"] == 8000


def test_watchdb_multiple_history_rows(db):
    wid = _sample_watch(db, last_price=8000)
    db.update_price(wid, 7500, "kiwi", None, "2026-06-23T09:00:00")
    db.update_price(wid, 7000, "kiwi", "https://k.com", "2026-06-23T10:00:00")
    cur = db._conn.execute(
        "SELECT price FROM price_history WHERE watch_id=? ORDER BY id", (wid,)
    )
    prices = [r[0] for r in cur.fetchall()]
    assert prices == [7500, 7000]


def test_watchdb_close_idempotent(db):
    db.close()
    # second close should not raise (hits the except Exception: pass path)
    db.close()


def test_watchdb_close_exception_suppressed():
    """If conn.close() raises, WatchDB.close() silently continues."""
    _db = WatchDB(":memory:")
    real_conn = _db._conn  # keep a handle so we can close it for real

    class _BrokenConn:
        def close(self):
            raise OSError("disk error")

    _db._conn = _BrokenConn()
    try:
        _db.close()  # must not raise
    finally:
        real_conn.close()  # avoid leaking the real connection (ResourceWarning)


def test_check_all_watches_default_fare_fn(db, monkeypatch):
    """check_all_watches without explicit fare_fn falls back to app.get_fare."""
    import app as appmod
    _sample_watch(db, last_price=8000)

    def fake_get_fare(*a, **k):
        return {"cheapest_cad": 7500, "stops": 1, "nonstop_cad": None,
                "source": "kiwi", "book": None}

    monkeypatch.setattr(appmod, "get_fare", fake_get_fare)
    # Pass fare_fn=None so the lazy import path is exercised
    drops = check_all_watches(db, fare_fn=None)
    assert len(drops) == 1
    assert drops[0]["new_price"] == 7500


# ---------------------------------------------------------------------------
# check_all_watches — drop detection
# ---------------------------------------------------------------------------

def _fare_fn_returning(cheapest_cad, source="kiwi", book="https://k.com/book"):
    """Return a fare_fn that always returns the given fare dict."""
    def fare_fn(origin, dest, dep, ret, adults, children):
        return {
            "cheapest_cad": cheapest_cad,
            "stops": 1,
            "nonstop_cad": None,
            "source": source,
            "book": book,
        }
    return fare_fn


def test_check_all_watches_drop_path(db, capsys):
    """Price drops from 8000 → 7000: returns a drop record and prints to stdout."""
    _sample_watch(db, last_price=8000)
    drops = check_all_watches(db, fare_fn=_fare_fn_returning(7000))
    assert len(drops) == 1
    d = drops[0]
    assert d["origin"] == "YYZ"
    assert d["dest_iata"] == "PEK"
    assert d["old_price"] == 8000
    assert d["new_price"] == 7000
    assert d["delta"] == -1000
    assert d["source"] == "kiwi"
    assert d["book"] == "https://k.com/book"
    # stdout line
    out = capsys.readouterr().out
    assert "[PRICE DROP]" in out
    assert "YYZ" in out
    assert "PEK" in out
    assert "7,000" in out or "7000" in out


def test_check_all_watches_drop_stdout_format(db, capsys):
    """Stdout line contains origin→dest, both prices, and book URL."""
    _sample_watch(db, last_price=8000)
    check_all_watches(db, fare_fn=_fare_fn_returning(7000, book="https://k.com/trip"))
    out = capsys.readouterr().out
    assert "YYZ" in out
    assert "PEK" in out
    assert "book:" in out
    assert "https://k.com/trip" in out


def test_check_all_watches_no_drop_same_price(db, capsys):
    """Same price: no drop record and no stdout."""
    _sample_watch(db, last_price=8000)
    drops = check_all_watches(db, fare_fn=_fare_fn_returning(8000))
    assert drops == []
    out = capsys.readouterr().out
    assert "[PRICE DROP]" not in out


def test_check_all_watches_no_drop_higher_price(db):
    """Higher price: no drop record."""
    _sample_watch(db, last_price=8000)
    drops = check_all_watches(db, fare_fn=_fare_fn_returning(9000))
    assert drops == []


def test_check_all_watches_no_last_price(db):
    """When last_price is None, no drop (first check establishes baseline)."""
    wid = db.add_watch(
        origin="YYZ", dest_iata="PEK", dest_city="Beijing",
        dep_date="2026-12-14", ret_date="2027-01-04",
        adults=2, children=0, threshold_pct=25.0,
        last_price=None, last_source=None,
        created_at="2026-06-23T00:00:00",
    )
    drops = check_all_watches(db, fare_fn=_fare_fn_returning(7000))
    assert drops == []
    # last_price should now be set to the new price
    w = db.list_watches()[0]
    assert w["last_price"] == 7000


def test_check_all_watches_no_data_no_drop(db):
    """fare_fn returns cheapest_cad=None: no drop, last_price preserved."""
    _sample_watch(db, last_price=8000)
    fare_fn = _fare_fn_returning(None, source="no-data", book=None)
    drops = check_all_watches(db, fare_fn=fare_fn)
    assert drops == []
    # last_price must stay at 8000, not overwritten with None
    w = db.list_watches()[0]
    assert w["last_price"] == 8000


def test_check_all_watches_updates_last_price_on_drop(db):
    """After a drop, last_price in DB is updated to the new lower price."""
    _sample_watch(db, last_price=8000)
    check_all_watches(db, fare_fn=_fare_fn_returning(7000))
    w = db.list_watches()[0]
    assert w["last_price"] == 7000


def test_check_all_watches_updates_last_price_no_drop(db):
    """On a non-drop (higher price), last_price is still updated (tracks latest)."""
    _sample_watch(db, last_price=8000)
    check_all_watches(db, fare_fn=_fare_fn_returning(9000))
    w = db.list_watches()[0]
    assert w["last_price"] == 9000


def test_check_all_watches_inserts_price_history(db):
    """Every check inserts a price_history row regardless of drop."""
    _sample_watch(db, last_price=8000)
    check_all_watches(db, fare_fn=_fare_fn_returning(7000))
    cur = db._conn.execute("SELECT price FROM price_history")
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 7000


def test_check_all_watches_skips_inactive(db):
    """Inactive watches are not checked."""
    _sample_watch(db, last_price=8000)
    db._conn.execute("UPDATE watches SET active=0")
    db._conn.commit()
    call_count = {"n": 0}

    def counting_fare_fn(*a):
        call_count["n"] += 1
        return {"cheapest_cad": 7000, "stops": 1, "nonstop_cad": None, "source": "k", "book": None}

    drops = check_all_watches(db, fare_fn=counting_fare_fn)
    assert drops == []
    assert call_count["n"] == 0


def test_check_all_watches_uses_now_iso(db):
    """Passing now_iso overrides the timestamp recorded in price_history."""
    _sample_watch(db, last_price=8000)
    check_all_watches(
        db,
        fare_fn=_fare_fn_returning(7000),
        now_iso="2099-01-01T00:00:00",
    )
    cur = db._conn.execute("SELECT checked_at FROM price_history")
    assert cur.fetchone()[0] == "2099-01-01T00:00:00"


# ---------------------------------------------------------------------------
# check_all_watches — webhook
# ---------------------------------------------------------------------------

def test_check_all_watches_webhook_called_on_drop(db):
    """webhook_url is set and a drop occurs: requests.post is called."""
    _sample_watch(db, last_price=8000)
    with patch("watch.requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        check_all_watches(
            db, fare_fn=_fare_fn_returning(7000),
            webhook_url="https://hooks.example.com/price-drop"
        )
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    url = call_kwargs[0][0] if call_kwargs[0] else call_kwargs[1].get("url")
    assert url == "https://hooks.example.com/price-drop"
    # webhook POST must be bounded by a timeout so a slow endpoint can't hang
    assert call_kwargs.kwargs.get("timeout") is not None
    # JSON body must contain the drop fields
    import json as _json
    body = call_kwargs[1].get("json") or call_kwargs[0][1]
    assert body["old_price"] == 8000
    assert body["new_price"] == 7000
    assert body["delta"] == -1000


def test_check_all_watches_webhook_not_called_on_no_drop(db):
    """No drop: requests.post is NOT called even if webhook_url is set."""
    _sample_watch(db, last_price=8000)
    with patch("watch.requests.post") as mock_post:
        check_all_watches(
            db, fare_fn=_fare_fn_returning(8000),
            webhook_url="https://hooks.example.com/price-drop"
        )
    mock_post.assert_not_called()


def test_check_all_watches_webhook_exception_does_not_crash(db):
    """If the webhook POST raises, the run continues and returns drop records."""
    _sample_watch(db, last_price=8000)
    with patch("watch.requests.post", side_effect=ConnectionError("timeout")):
        drops = check_all_watches(
            db, fare_fn=_fare_fn_returning(7000),
            webhook_url="https://hooks.example.com/price-drop"
        )
    # Drop should still be reported despite webhook failure
    assert len(drops) == 1
    assert drops[0]["new_price"] == 7000
