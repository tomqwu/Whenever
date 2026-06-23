"""End-to-end tests for the watch scheduler.

These tests call scheduler.main() in-process (not subprocess) so coverage
is captured by pytest-cov. A temp SQLite file is used; it is cleaned up by
the tmp_path fixture.
"""
import os
import sys

import pytest

import app as appmod
import scheduler
from watch import WatchDB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_fare(cheapest_cad, source="kiwi", book="https://k.com/book"):
    """Return a get_fare-shaped dict with the given price."""
    return {
        "cheapest_cad": cheapest_cad,
        "stops": 1,
        "nonstop_cad": None,
        "source": source,
        "book": book,
    }


# ---------------------------------------------------------------------------
# E2E: scheduler.main() — drop detected
# ---------------------------------------------------------------------------

def test_scheduler_main_detects_drop(tmp_path, monkeypatch, capsys):
    """E2E: seed a watch with last_price=8000, monkeypatch get_fare to return 7000,
    call scheduler.main(), assert exit 0, drop printed to stdout, DB updated."""
    db_path = str(tmp_path / "test_watches.db")
    monkeypatch.setenv("WATCH_DB", db_path)
    # Unset webhook so no HTTP call is attempted
    monkeypatch.delenv("WATCH_WEBHOOK_URL", raising=False)

    # Seed the DB with a watch
    db = WatchDB(db_path)
    db.add_watch(
        origin="YYZ",
        dest_iata="PEK",
        dest_city="Beijing",
        dep_date="2026-12-14",
        ret_date="2027-01-04",
        adults=2,
        children=0,
        threshold_pct=25.0,
        last_price=8000,
        last_source="travelpayouts",
        created_at="2026-06-23T00:00:00",
    )
    db.close()

    # Monkeypatch app.get_fare to return a lower price
    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: _fake_fare(7000))

    exit_code = scheduler.main([])
    assert exit_code == 0

    out = capsys.readouterr().out
    assert "[PRICE DROP]" in out

    # Verify DB was updated
    db2 = WatchDB(db_path)
    watches = db2.list_watches()
    db2.close()
    assert len(watches) == 1
    assert watches[0]["last_price"] == 7000


def test_scheduler_main_no_drop(tmp_path, monkeypatch, capsys):
    """E2E: no drop when new price equals old price."""
    db_path = str(tmp_path / "test_watches_nodrop.db")
    monkeypatch.setenv("WATCH_DB", db_path)
    monkeypatch.delenv("WATCH_WEBHOOK_URL", raising=False)

    db = WatchDB(db_path)
    db.add_watch(
        origin="YYZ",
        dest_iata="PEK",
        dest_city="Beijing",
        dep_date="2026-12-14",
        ret_date="2027-01-04",
        adults=2,
        children=0,
        threshold_pct=25.0,
        last_price=8000,
        last_source="travelpayouts",
        created_at="2026-06-23T00:00:00",
    )
    db.close()

    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: _fake_fare(8000))

    exit_code = scheduler.main([])
    assert exit_code == 0

    out = capsys.readouterr().out
    assert "[PRICE DROP]" not in out


def test_scheduler_main_empty_db(tmp_path, monkeypatch, capsys):
    """E2E: no watches in DB → main returns 0 and prints a summary."""
    db_path = str(tmp_path / "empty.db")
    monkeypatch.setenv("WATCH_DB", db_path)
    monkeypatch.delenv("WATCH_WEBHOOK_URL", raising=False)

    exit_code = scheduler.main([])
    assert exit_code == 0


def test_scheduler_main_returns_int(tmp_path, monkeypatch):
    """main() must return an integer (sys.exit-compatible)."""
    db_path = str(tmp_path / "int_test.db")
    monkeypatch.setenv("WATCH_DB", db_path)
    monkeypatch.delenv("WATCH_WEBHOOK_URL", raising=False)

    result = scheduler.main([])
    assert isinstance(result, int)


# ---------------------------------------------------------------------------
# E2E: scheduler helper functions (add_watch, list_watches, remove_watch)
# ---------------------------------------------------------------------------

def test_scheduler_add_and_list_watches(tmp_path, monkeypatch):
    """scheduler.add_watch / list_watches round-trip through the DB."""
    db_path = str(tmp_path / "helpers.db")
    monkeypatch.setenv("WATCH_DB", db_path)

    db = WatchDB(db_path)
    wid = scheduler.add_watch(
        db=db,
        origin="YYZ",
        dest_iata="PEK",
        dest_city="Beijing",
        dep_date="2026-12-14",
        ret_date="2027-01-04",
        adults=2,
        children=0,
        threshold_pct=25.0,
    )
    watches = scheduler.list_watches(db=db)
    db.close()
    assert len(watches) == 1
    assert watches[0]["id"] == wid
    assert watches[0]["origin"] == "YYZ"


def test_scheduler_remove_watch(tmp_path, monkeypatch):
    """scheduler.remove_watch deactivates the watch."""
    db_path = str(tmp_path / "remove.db")
    monkeypatch.setenv("WATCH_DB", db_path)

    db = WatchDB(db_path)
    wid = scheduler.add_watch(
        db=db,
        origin="YYZ",
        dest_iata="PEK",
        dest_city="Beijing",
        dep_date="2026-12-14",
        ret_date="2027-01-04",
        adults=2,
        children=0,
        threshold_pct=25.0,
    )
    scheduler.remove_watch(db=db, watch_id=wid)
    watches = scheduler.list_watches(db=db)
    db.close()
    assert watches == []


def test_scheduler_main_blank_watch_db_falls_back_to_default(monkeypatch):
    """A present-but-empty WATCH_DB ("") must resolve to whenever_watches.db,
    not an anonymous/transient SQLite DB."""
    monkeypatch.setenv("WATCH_DB", "")
    monkeypatch.delenv("WATCH_WEBHOOK_URL", raising=False)

    captured = {}

    class _FakeDB:
        def __init__(self, path):
            captured["path"] = path

        def close(self):
            pass

    monkeypatch.setattr(scheduler, "WatchDB", _FakeDB)
    monkeypatch.setattr(scheduler, "check_all_watches", lambda *a, **k: [])

    exit_code = scheduler.main([])
    assert exit_code == 0
    assert captured["path"] == "whenever_watches.db"


def test_scheduler_main_with_webhook(tmp_path, monkeypatch):
    """E2E: webhook env var set but POST raises; main still returns 0."""
    from unittest.mock import patch

    db_path = str(tmp_path / "webhook_test.db")
    monkeypatch.setenv("WATCH_DB", db_path)
    monkeypatch.setenv("WATCH_WEBHOOK_URL", "https://hooks.example.com/drop")

    db = WatchDB(db_path)
    db.add_watch(
        origin="YYZ",
        dest_iata="PEK",
        dest_city="Beijing",
        dep_date="2026-12-14",
        ret_date="2027-01-04",
        adults=2,
        children=0,
        threshold_pct=25.0,
        last_price=8000,
        last_source="travelpayouts",
        created_at="2026-06-23T00:00:00",
    )
    db.close()

    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: _fake_fare(7000))

    with patch("watch.requests.post", side_effect=ConnectionError("timeout")):
        exit_code = scheduler.main([])

    assert exit_code == 0
