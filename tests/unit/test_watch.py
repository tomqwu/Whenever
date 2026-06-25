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


def test_watchdb_creates_active_unique_index(db):
    """The partial UNIQUE INDEX guarding active trip keys exists."""
    cur = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )
    names = {r[0] for r in cur.fetchall()}
    assert "idx_watch_active_unique" in names


def test_watchdb_duplicate_active_raises_integrityerror(db):
    """A second identical active watch violates the partial unique index."""
    import sqlite3
    kwargs = dict(
        origin="YYZ", dest_iata="PEK", dest_city="Beijing",
        dep_date="2026-12-14", ret_date="2027-01-04",
        adults=2, child_ages=[11, 9], threshold_pct=25.0,
    )
    db.add_watch(**kwargs)
    with pytest.raises(sqlite3.IntegrityError):
        db.add_watch(**kwargs)
    assert len(db.list_watches(active_only=True)) == 1


def test_watchdb_rewatch_after_remove(db):
    """The partial index only constrains active rows: after remove_watch the
    same trip can be inserted again as a new active row."""
    kwargs = dict(
        origin="YYZ", dest_iata="PEK", dest_city="Beijing",
        dep_date="2026-12-14", ret_date="2027-01-04",
        adults=2, child_ages=[11, 9], threshold_pct=25.0,
    )
    wid = db.add_watch(**kwargs)
    db.remove_watch(wid)
    wid2 = db.add_watch(**kwargs)   # must NOT raise
    assert wid2 != wid
    assert len(db.list_watches(active_only=True)) == 1


def test_watchdb_child_count_distinguishes_active_rows(db):
    """A count-only watch (children=2, child_ages=[]) and an adults-only watch
    (children=0, child_ages=[]) for the same route/dates are DIFFERENT trips
    (priced for different party sizes) and must both stay active — the unique
    key includes the children COUNT, not just child_ages."""
    common = dict(
        origin="YYZ", dest_iata="PEK", dest_city="Beijing",
        dep_date="2026-12-14", ret_date="2027-01-04",
        adults=2, threshold_pct=25.0, created_at="2026-06-23T00:00:00",
    )
    db.add_watch(children=0, **common)   # adults only
    db.add_watch(children=2, **common)   # two kids, ages unknown — must NOT collide
    actives = db.list_watches(active_only=True)
    assert len(actives) == 2
    assert {w["children"] for w in actives} == {0, 2}


def test_watchdb_dedupes_preexisting_active_duplicates_on_init(tmp_path):
    """A DB that already holds duplicate active rows for a trip key (e.g. created
    before this index existed) must NOT crash WatchDB init. Re-opening de-dupes:
    the lowest id stays active, the rest are flagged inactive, and the unique
    index gets created."""
    import sqlite3
    db_path = str(tmp_path / "dupes.db")

    # First open creates the schema (and the unique index).
    db1 = WatchDB(db_path)
    # Insert two duplicate ACTIVE rows DIRECTLY, bypassing add_watch and the
    # index guard, to simulate a pre-index DB. Drop the index so the raw inserts
    # are allowed, mirroring a DB created before the index existed.
    db1._conn.execute("DROP INDEX IF EXISTS idx_watch_active_unique")
    for _ in range(2):
        db1._conn.execute(
            """INSERT INTO watches
               (origin, dest_iata, dest_city, dep_date, ret_date,
                adults, children, child_ages, threshold_pct, created_at, active)
               VALUES ('YYZ','PEK','Beijing','2026-12-14','2027-01-04',
                       2, 0, '[]', 25.0, '2026-06-23T00:00:00', 1)""",
        )
    db1._conn.commit()
    # Two active duplicates now exist with no index.
    assert len(db1.list_watches(active_only=True)) == 2
    db1.close()

    # Re-opening must NOT raise despite the pre-existing duplicates.
    db2 = WatchDB(db_path)
    try:
        actives = db2.list_watches(active_only=True)
        assert len(actives) == 1, "duplicates should be collapsed to one active row"
        # The surviving active row is the lowest id; the other is now inactive.
        all_rows = db2.list_watches(active_only=False)
        assert len(all_rows) == 2
        inactive = [r for r in all_rows if r["active"] == 0]
        assert len(inactive) == 1
        assert actives[0]["id"] == min(r["id"] for r in all_rows)
        # The unique index now exists.
        cur = db2._conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        assert "idx_watch_active_unique" in {r[0] for r in cur.fetchall()}
        # Idempotent: re-running the de-dup pass changes nothing.
        db2._dedupe_active_watches()
        assert len(db2.list_watches(active_only=True)) == 1
    finally:
        db2.close()


def test_watchdb_index_retry_after_dedup_succeeds(tmp_path, monkeypatch):
    """If the FIRST de-dup pass is skipped (so the first CREATE UNIQUE INDEX hits
    duplicates and raises), init's defensive retry runs de-dup again and the
    index is created — init must not crash."""
    # Build a DB with two active duplicates and NO index.
    db_path = str(tmp_path / "retry.db")
    seed = WatchDB(db_path)
    seed._conn.execute("DROP INDEX IF EXISTS idx_watch_active_unique")
    for _ in range(2):
        seed._conn.execute(
            """INSERT INTO watches
               (origin, dest_iata, dest_city, dep_date, ret_date,
                adults, children, child_ages, threshold_pct, created_at, active)
               VALUES ('YYZ','PEK','Beijing','2026-12-14','2027-01-04',
                       2, 0, '[]', 25.0, '2026-06-23T00:00:00', 1)""",
        )
    seed._conn.commit()
    seed.close()

    # Make the FIRST _dedupe_active_watches call a no-op so the first CREATE
    # raises IntegrityError; subsequent calls run the real de-dup (retry path).
    real_dedupe = WatchDB._dedupe_active_watches
    calls = {"n": 0}

    def flaky_dedupe(self):
        calls["n"] += 1
        if calls["n"] == 1:
            return  # skip — leaves duplicates so the first CREATE INDEX fails
        return real_dedupe(self)

    monkeypatch.setattr(WatchDB, "_dedupe_active_watches", flaky_dedupe)
    db2 = WatchDB(db_path)   # must NOT raise — retry de-dup fixes it
    try:
        assert len(db2.list_watches(active_only=True)) == 1
        cur = db2._conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        assert "idx_watch_active_unique" in {r[0] for r in cur.fetchall()}
        assert calls["n"] >= 2  # first (no-op) + retry
    finally:
        db2.close()


def test_watchdb_index_raises_clear_error_when_dedup_never_resolves(tmp_path, monkeypatch):
    """If de-dup never removes the duplicates (both passes are no-ops), init
    surfaces a clear IntegrityError naming the index rather than a bare one."""
    import sqlite3
    db_path = str(tmp_path / "stuck.db")
    seed = WatchDB(db_path)
    seed._conn.execute("DROP INDEX IF EXISTS idx_watch_active_unique")
    for _ in range(2):
        seed._conn.execute(
            """INSERT INTO watches
               (origin, dest_iata, dest_city, dep_date, ret_date,
                adults, children, child_ages, threshold_pct, created_at, active)
               VALUES ('YYZ','PEK','Beijing','2026-12-14','2027-01-04',
                       2, 0, '[]', 25.0, '2026-06-23T00:00:00', 1)""",
        )
    seed._conn.commit()
    seed.close()

    # Every de-dup pass is a no-op: both CREATE attempts fail.
    monkeypatch.setattr(WatchDB, "_dedupe_active_watches", lambda self: None)
    with pytest.raises(sqlite3.IntegrityError) as exc:
        WatchDB(db_path)
    assert "idx_watch_active_unique" in str(exc.value)


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


def test_check_all_watches_book_none_falls_back_to_kayak(db, capsys):
    """When the provider gives no book link, the drop uses a Kayak fallback URL.

    The fallback must be consistent across the drop record, the stdout line,
    the webhook payload, AND the stored price_history.book.
    """
    _sample_watch(db, last_price=8000)
    with patch("watch.requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        drops = check_all_watches(
            db,
            fare_fn=_fare_fn_returning(7000, source="amadeus", book=None),
            webhook_url="https://hooks.example.com/price-drop",
        )
    assert len(drops) == 1
    d = drops[0]
    # drop record uses the kayak fallback
    assert d["book"].startswith("https://www.kayak.com")
    assert "YYZ-PEK" in d["book"]
    # stdout line uses the fallback
    out = capsys.readouterr().out
    assert "[PRICE DROP]" in out
    assert d["book"] in out
    assert "book: None" not in out
    # webhook payload uses the fallback
    call_args = mock_post.call_args
    body = call_args[1].get("json") or call_args[0][1]
    assert body["book"] == d["book"]
    # stored price_history.book uses the fallback
    cur = db._conn.execute("SELECT book FROM price_history WHERE watch_id=?", (d["watch_id"],))
    assert cur.fetchone()[0] == d["book"]


def test_check_all_watches_kayak_fallback_encodes_child_ages(db):
    """A watch with child ages + no provider book link → kayak URL encodes ages."""
    db.add_watch(
        origin="YYZ", dest_iata="PEK", dest_city="Beijing",
        dep_date="2026-12-14", ret_date="2027-01-04",
        adults=2, child_ages=[11, 9], threshold_pct=25.0,
        last_price=8000, last_source="travelpayouts",
        created_at="2026-06-23T00:00:00",
    )
    drops = check_all_watches(
        db, fare_fn=_fare_fn_returning(7000, source="amadeus", book=None)
    )
    assert len(drops) == 1
    book = drops[0]["book"]
    assert book.startswith("https://www.kayak.com")
    # Ages are canonicalized (sorted) on store, so the link carries 9-11.
    assert "children-9-11" in book


def test_check_all_watches_kayak_fallback_count_only_placeholder_ages(db):
    """A watch with a children COUNT but NO ages + no provider book link →
    kayak fallback URL encodes the right number of kids via placeholder ages."""
    db.add_watch(
        origin="YYZ", dest_iata="PEK", dest_city="Beijing",
        dep_date="2026-12-14", ret_date="2027-01-04",
        adults=2, children=2, threshold_pct=25.0,
        last_price=8000, last_source="travelpayouts",
        created_at="2026-06-23T00:00:00",
    )
    drops = check_all_watches(
        db, fare_fn=_fare_fn_returning(7000, source="amadeus", book=None)
    )
    assert len(drops) == 1
    book = drops[0]["book"]
    assert book.startswith("https://www.kayak.com")
    assert "children-10-10" in book


def test_check_all_watches_kayak_fallback_no_children_adults_only(db):
    """A watch with no children → kayak fallback URL has no children segment."""
    _sample_watch(db, last_price=8000)  # child_ages defaults to []
    drops = check_all_watches(
        db, fare_fn=_fare_fn_returning(7000, source="amadeus", book=None)
    )
    assert len(drops) == 1
    book = drops[0]["book"]
    assert book.startswith("https://www.kayak.com")
    assert "children" not in book


def test_watchdb_persists_and_parses_child_ages(db):
    """child_ages is stored JSON-encoded and parsed back to a list; children=len.

    Ages are canonicalized (sorted) on store, so [11, 9] persists as [9, 11].
    """
    db.add_watch(
        origin="YYZ", dest_iata="PEK", dest_city="Beijing",
        dep_date="2026-12-14", ret_date="2027-01-04",
        adults=2, child_ages=[11, 9], threshold_pct=25.0,
        created_at="2026-06-23T00:00:00",
    )
    w = db.list_watches()[0]
    assert w["child_ages"] == [9, 11]
    assert w["children"] == 2
    # The raw stored JSON text is canonical (sorted) so the unique index and
    # the route's order-insensitive dedup agree on the key.
    raw = db._conn.execute("SELECT child_ages FROM watches").fetchone()[0]
    assert raw == "[9, 11]"


def test_watchdb_canonicalizes_child_age_order_single_active_row(db):
    """[11, 9] then [9, 11] for the SAME trip are the same party: the second
    add collides with the unique index (same canonical stored text) instead of
    surviving as an index-distinct duplicate."""
    import sqlite3
    kwargs = dict(
        origin="YYZ", dest_iata="PEK", dest_city="Beijing",
        dep_date="2026-12-14", ret_date="2027-01-04",
        adults=2, threshold_pct=25.0, created_at="2026-06-23T00:00:00",
    )
    db.add_watch(child_ages=[11, 9], **kwargs)
    with pytest.raises(sqlite3.IntegrityError):
        db.add_watch(child_ages=[9, 11], **kwargs)
    actives = db.list_watches(active_only=True)
    assert len(actives) == 1
    assert actives[0]["child_ages"] == [9, 11]


def test_watchdb_dedupes_preexisting_age_order_variants_on_init(tmp_path):
    """A pre-canonical DB holding [11,9] and [9,11] active rows for the same
    trip (distinct to a raw-text index, same party in reality) must collapse to
    one active row on init and let the unique index be created."""
    db_path = str(tmp_path / "ages.db")

    db1 = WatchDB(db_path)
    # Drop the index and INSERT two active rows with DIFFERENT raw age order,
    # simulating rows written before child_ages was canonicalized on store.
    db1._conn.execute("DROP INDEX IF EXISTS idx_watch_active_unique")
    for ages in ("[11, 9]", "[9, 11]"):
        db1._conn.execute(
            """INSERT INTO watches
               (origin, dest_iata, dest_city, dep_date, ret_date,
                adults, children, child_ages, threshold_pct, created_at, active)
               VALUES ('YYZ','PEK','Beijing','2026-12-14','2027-01-04',
                       2, 2, ?, 25.0, '2026-06-23T00:00:00', 1)""",
            (ages,),
        )
    db1._conn.commit()
    assert len(db1.list_watches(active_only=True)) == 2
    db1.close()

    db2 = WatchDB(db_path)
    try:
        actives = db2.list_watches(active_only=True)
        assert len(actives) == 1, "order-variant duplicates collapse to one row"
        cur = db2._conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        assert "idx_watch_active_unique" in {r[0] for r in cur.fetchall()}
    finally:
        db2.close()


def test_watchdb_explicit_children_count_without_ages(db):
    """children passed WITHOUT ages is preserved (not silently zeroed)."""
    db.add_watch(
        origin="YYZ", dest_iata="PEK", dest_city="Beijing",
        dep_date="2026-12-14", ret_date="2027-01-04",
        adults=2, children=2, threshold_pct=25.0,
        created_at="2026-06-23T00:00:00",
    )
    w = db.list_watches()[0]
    assert w["children"] == 2
    assert w["child_ages"] == []


def test_check_all_watches_prices_explicit_children_without_ages(db):
    """check_all_watches calls get_fare with the stored children count (=2),
    not len(child_ages) (=0), when ages are unknown."""
    db.add_watch(
        origin="YYZ", dest_iata="PEK", dest_city="Beijing",
        dep_date="2026-12-14", ret_date="2027-01-04",
        adults=2, children=2, threshold_pct=25.0,
        last_price=8000, last_source="travelpayouts",
        created_at="2026-06-23T00:00:00",
    )
    captured = {}

    def fare_fn(origin, dest, dep, ret, adults, children):
        captured["adults"] = adults
        captured["children"] = children
        return {"cheapest_cad": 7000, "stops": 1, "nonstop_cad": None,
                "source": "kiwi", "book": "https://k.com/book"}

    drops = check_all_watches(db, fare_fn=fare_fn)
    assert len(drops) == 1
    assert captured["adults"] == 2
    assert captured["children"] == 2


def test_check_all_watches_provider_book_link_preserved(db):
    """When the provider supplies a book link, it is used unchanged (no fallback)."""
    _sample_watch(db, last_price=8000)
    drops = check_all_watches(
        db, fare_fn=_fare_fn_returning(7000, book="https://k.com/book")
    )
    assert drops[0]["book"] == "https://k.com/book"
    cur = db._conn.execute("SELECT book FROM price_history")
    assert cur.fetchone()[0] == "https://k.com/book"


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


def test_check_all_watches_seeded_baseline_fires_on_first_run(db):
    """A watch saved WITH last_price reports a drop on the FIRST scheduler run.

    Previously the first run was treated as baseline-setting and the drop was
    suppressed; a seeded baseline must let the first drop fire immediately.
    """
    _sample_watch(db, last_price=8000)  # baseline seeded at save time
    drops = check_all_watches(db, fare_fn=_fare_fn_returning(7000))
    assert len(drops) == 1
    assert drops[0]["old_price"] == 8000
    assert drops[0]["new_price"] == 7000


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


# ---------------------------------------------------------------------------
# Coverage: watch.py line 90 — created_at auto-filled when not provided
# ---------------------------------------------------------------------------

def test_add_watch_auto_fills_created_at(db):
    """When created_at is omitted, add_watch fills it with the current UTC ISO time."""
    wid = db.add_watch(
        origin="YYZ", dest_iata="PEK", dest_city="Beijing",
        dep_date="2026-12-14", ret_date="2027-01-04",
        adults=2, threshold_pct=25.0,
        # created_at intentionally omitted to exercise the auto-fill branch (line 90)
    )
    w = db.list_watches()[0]
    # created_at must be a non-empty ISO 8601 string
    assert w["created_at"], "created_at should have been auto-filled"
    # Parseable as a datetime
    parsed = datetime.datetime.fromisoformat(w["created_at"])
    assert parsed.tzinfo is not None or len(w["created_at"]) >= 19


# ---------------------------------------------------------------------------
# Coverage: watch.py line 41 — WAL journal mode for file-based DBs
# ---------------------------------------------------------------------------

def test_watchdb_wal_mode_enabled_for_file_db(tmp_path):
    """WatchDB opened on a real file should enable WAL journal mode (line 41)."""
    db_path = str(tmp_path / "test_wal.db")
    db = WatchDB(db_path)
    try:
        cur = db._conn.execute("PRAGMA journal_mode")
        mode = cur.fetchone()[0]
        assert mode == "wal", f"Expected WAL mode, got {mode!r}"
    finally:
        db.close()


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
