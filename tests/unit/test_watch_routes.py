"""Unit tests for the /api/watch web endpoints (issue #38).

Each test points the watch DB at an in-memory WatchDB via a monkeypatched
``_watch_db`` so no DB files are left on disk and there is no cross-thread reuse.
A single shared in-memory connection is used per test so add/list/delete all
observe the same data.
"""
import pytest

import app as appmod
import watch as watchmod


@pytest.fixture
def watch_db(monkeypatch):
    """Point app._watch_db at one shared in-memory WatchDB for the whole test.

    add_watch / list_watches / remove_watch must all see the same rows, so the
    same connection is reused; the route's try/finally close() is neutralized so
    the shared DB survives across requests within a test, then closed at teardown.
    """
    db = watchmod.WatchDB(":memory:")
    monkeypatch.setattr(db, "close", lambda: None)  # keep shared conn alive across requests
    monkeypatch.setattr(appmod, "_watch_db", lambda: db)
    yield db
    db._conn.close()


_VALID_BODY = {
    "origin": "YYZ",
    "dest_iata": "PVG",
    "dest_city": "Shanghai",
    "dep_date": "2026-12-12",
    "ret_date": "2027-01-04",
    "adults": 2,
    "child_ages": [11, 9],
    "threshold_pct": 25.0,
    "last_price": 8000,
    "last_source": "travelpayouts",
}


def test_add_watch_creates_row(client, watch_db):
    r = client.post("/api/watch", json=_VALID_BODY)
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert isinstance(body["id"], int)

    rows = watch_db.list_watches()
    assert len(rows) == 1
    row = rows[0]
    assert row["origin"] == "YYZ"
    assert row["dest_iata"] == "PVG"
    assert row["dest_city"] == "Shanghai"
    assert row["dep_date"] == "2026-12-12"
    assert row["ret_date"] == "2027-01-04"
    assert row["last_price"] == 8000
    assert row["last_source"] == "travelpayouts"
    assert row["child_ages"] == [11, 9]


def test_add_watch_minimal_body(client, watch_db):
    """Only the required fields — optional ones default."""
    r = client.post("/api/watch", json={
        "origin": "YYZ", "dest_iata": "PVG",
        "dep_date": "2026-12-12", "ret_date": "2027-01-04",
    })
    assert r.status_code == 200
    rows = watch_db.list_watches()
    assert len(rows) == 1
    assert rows[0]["adults"] == 2          # default
    assert rows[0]["last_price"] is None
    assert rows[0]["dest_city"] is None


@pytest.mark.parametrize("missing", ["origin", "dest_iata", "dep_date", "ret_date"])
def test_add_watch_missing_required_field_400(client, watch_db, missing):
    body = dict(_VALID_BODY)
    body.pop(missing)
    r = client.post("/api/watch", json=body)
    assert r.status_code == 400
    assert "error" in r.get_json()
    assert watch_db.list_watches() == []   # nothing persisted on a 400


def test_list_watches_returns_saved(client, watch_db):
    client.post("/api/watch", json=_VALID_BODY)
    client.post("/api/watch", json={**_VALID_BODY, "dest_iata": "PEK", "dest_city": "Beijing"})

    r = client.get("/api/watch")
    assert r.status_code == 200
    watches = r.get_json()["watches"]
    assert len(watches) == 2
    iatas = {w["dest_iata"] for w in watches}
    assert iatas == {"PVG", "PEK"}
    # JSON-friendly dict shape
    w = watches[0]
    for key in ("id", "origin", "dest_iata", "dest_city", "dep_date", "ret_date", "last_price"):
        assert key in w


def test_list_watches_empty(client, watch_db):
    r = client.get("/api/watch")
    assert r.status_code == 200
    assert r.get_json()["watches"] == []


def test_delete_watch_removes_it(client, watch_db):
    wid = client.post("/api/watch", json=_VALID_BODY).get_json()["id"]
    assert len(watch_db.list_watches()) == 1

    r = client.delete(f"/api/watch/{wid}")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert watch_db.list_watches() == []   # active_only -> removed


def test_add_watch_non_numeric_last_price_400(client, watch_db):
    """A non-numeric last_price (e.g. "8,000") is rejected with 400 and no row.

    Persisting TEXT in last_price would later crash check_all_watches' int<str
    comparison and block every watch check, so the route must refuse it.
    """
    body = {**_VALID_BODY, "last_price": "8,000"}
    r = client.post("/api/watch", json=body)
    assert r.status_code == 400
    assert r.get_json()["error"] == "last_price must be numeric"
    assert watch_db.list_watches() == []   # nothing persisted on a 400


@pytest.mark.parametrize("value", [8000, 8000.0, "8000", "8000.5"])
def test_add_watch_numeric_last_price_stored_as_int(client, watch_db, value):
    """A numeric last_price is coerced to and stored as a Python int."""
    r = client.post("/api/watch", json={**_VALID_BODY, "last_price": value})
    assert r.status_code == 200
    row = watch_db.list_watches()[0]
    assert row["last_price"] == 8000
    assert isinstance(row["last_price"], int)
    assert not isinstance(row["last_price"], bool)


def test_add_watch_omitted_last_price_is_none(client, watch_db):
    """last_price omitted -> stored as None (no baseline)."""
    body = dict(_VALID_BODY)
    body.pop("last_price")
    r = client.post("/api/watch", json=body)
    assert r.status_code == 200
    assert watch_db.list_watches()[0]["last_price"] is None


def test_add_watch_null_last_price_is_none(client, watch_db):
    """Explicit null last_price -> stored as None."""
    r = client.post("/api/watch", json={**_VALID_BODY, "last_price": None})
    assert r.status_code == 200
    assert watch_db.list_watches()[0]["last_price"] is None


def test_add_watch_bad_adults_400(client, watch_db):
    r = client.post("/api/watch", json={**_VALID_BODY, "adults": "two"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "adults must be numeric"
    assert watch_db.list_watches() == []


def test_add_watch_bad_threshold_pct_400(client, watch_db):
    r = client.post("/api/watch", json={**_VALID_BODY, "threshold_pct": "lots"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "threshold_pct must be numeric"
    assert watch_db.list_watches() == []


def test_add_watch_drops_non_int_child_ages(client, watch_db):
    """Non-int child_ages entries are dropped; the int ones are kept."""
    r = client.post("/api/watch", json={**_VALID_BODY, "child_ages": [11, "x", 9, None]})
    assert r.status_code == 200
    assert watch_db.list_watches()[0]["child_ages"] == [11, 9]


def test_add_watch_string_child_ages_400(client, watch_db):
    """A non-list child_ages (e.g. the string "11,9") is rejected with 400.

    Iterating a string per-character would persist nonsense ages ([1,1,9]),
    so the route must refuse anything that isn't a JSON array.
    """
    r = client.post("/api/watch", json={**_VALID_BODY, "child_ages": "11,9"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "child_ages must be a list"
    assert watch_db.list_watches() == []   # nothing persisted on a 400


def test_add_watch_list_child_ages_stored(client, watch_db):
    """A list child_ages is stored as exactly those children/ages."""
    r = client.post("/api/watch", json={**_VALID_BODY, "child_ages": [11, 9]})
    assert r.status_code == 200
    row = watch_db.list_watches()[0]
    assert row["child_ages"] == [11, 9]
    assert row["children"] == 2


def test_add_watch_omitted_child_ages_is_empty(client, watch_db):
    """child_ages omitted -> stored as an empty list (no children)."""
    body = dict(_VALID_BODY)
    body.pop("child_ages")
    r = client.post("/api/watch", json=body)
    assert r.status_code == 200
    row = watch_db.list_watches()[0]
    assert row["child_ages"] == []
    assert row["children"] == 0


def test_add_watch_integrityerror_returns_existing(client, watch_db, monkeypatch):
    """The DB unique-index backstop: if the pre-check misses but the INSERT hits
    an IntegrityError (concurrent duplicate), the route resolves to the existing
    active row's id with existing=True — a single active row, never a 500.
    """
    import sqlite3

    # Seed the trip directly so an active row already exists in the DB.
    first_id = watch_db.add_watch(
        origin="YYZ", dest_iata="PVG", dest_city="Shanghai",
        dep_date="2026-12-12", ret_date="2027-01-04", adults=2,
        child_ages=[11, 9], threshold_pct=25.0,
        last_price=8000, last_source="travelpayouts",
    )

    # Force the fast-path pre-check to miss so the INSERT runs and the partial
    # UNIQUE INDEX raises IntegrityError (the atomic backstop branch).
    real_list = watch_db.list_watches
    calls = {"n": 0}

    def flaky_list(active_only=True):
        calls["n"] += 1
        if calls["n"] == 1:
            return []          # pre-check sees nothing -> proceeds to INSERT
        return real_list(active_only=active_only)  # post-IntegrityError lookup

    monkeypatch.setattr(watch_db, "list_watches", flaky_list)

    r = client.post("/api/watch", json=_VALID_BODY)
    assert r.status_code == 200
    body = r.get_json()
    assert body["existing"] is True
    assert body["id"] == first_id
    # Still exactly one active row.
    assert len(real_list(active_only=True)) == 1


def test_add_watch_integrityerror_unrelated_reraises(client, watch_db, monkeypatch):
    """If an IntegrityError fires but no matching active row is found (an
    unrelated constraint), the error is re-raised rather than masked as existing.
    """
    import sqlite3

    def boom_add(**kwargs):
        raise sqlite3.IntegrityError("some other constraint")

    monkeypatch.setattr(watch_db, "list_watches", lambda active_only=True: [])
    monkeypatch.setattr(watch_db, "add_watch", boom_add)

    with pytest.raises(sqlite3.IntegrityError):
        client.post("/api/watch", json=_VALID_BODY)


def test_add_watch_db_unique_index_one_active_row(client, watch_db):
    """Two identical inserts straight through WatchDB.add_watch hit the partial
    UNIQUE INDEX: the second raises IntegrityError, leaving ONE active row.
    """
    import sqlite3

    kwargs = dict(
        origin="YYZ", dest_iata="PVG", dest_city="Shanghai",
        dep_date="2026-12-12", ret_date="2027-01-04", adults=2,
        child_ages=[11, 9], threshold_pct=25.0,
    )
    watch_db.add_watch(**kwargs)
    with pytest.raises(sqlite3.IntegrityError):
        watch_db.add_watch(**kwargs)
    assert len(watch_db.list_watches(active_only=True)) == 1


def test_add_watch_rewatch_after_remove(client, watch_db):
    """After remove_watch (active -> 0), the partial index frees the key, so the
    same trip can be watched again as a fresh active row.
    """
    r1 = client.post("/api/watch", json=_VALID_BODY)
    wid = r1.get_json()["id"]
    client.delete(f"/api/watch/{wid}")
    assert watch_db.list_watches(active_only=True) == []

    r2 = client.post("/api/watch", json=_VALID_BODY)
    assert r2.status_code == 200
    body = r2.get_json()
    assert "existing" not in body          # brand-new row, not a dedup hit
    assert body["id"] != wid
    assert len(watch_db.list_watches(active_only=True)) == 1


@pytest.mark.parametrize("body", [None, []])
def test_add_watch_non_dict_body_400(client, watch_db, body):
    """A null or non-object JSON body -> documented 400, never a 500, no row.

    request.get_json() returns None / a list here; the route must reject it
    before calling b.get(...) (which would raise AttributeError -> 500).
    """
    r = client.post("/api/watch", json=body)
    assert r.status_code == 400
    assert r.get_json()["error"] == "watch payload required"
    assert watch_db.list_watches() == []   # nothing persisted


def test_add_watch_same_trip_is_idempotent(client, watch_db):
    """Posting the SAME trip twice creates only ONE active row.

    The second call returns the existing watch's id with existing=True, and the
    list endpoint shows a single matching watch (no duplicate to re-price/alert).
    """
    r1 = client.post("/api/watch", json=_VALID_BODY)
    assert r1.status_code == 200
    first = r1.get_json()
    assert "existing" not in first

    r2 = client.post("/api/watch", json=_VALID_BODY)
    assert r2.status_code == 200
    second = r2.get_json()
    assert second["ok"] is True
    assert second["existing"] is True
    assert second["id"] == first["id"]

    rows = watch_db.list_watches()
    assert len(rows) == 1

    watches = client.get("/api/watch").get_json()["watches"]
    assert len(watches) == 1
    assert watches[0]["id"] == first["id"]


def test_add_watch_different_trip_creates_second_row(client, watch_db):
    """A trip differing on a key field (dates) creates a distinct second row."""
    r1 = client.post("/api/watch", json=_VALID_BODY)
    r2 = client.post("/api/watch", json={
        **_VALID_BODY, "dep_date": "2026-12-20", "ret_date": "2027-01-10",
    })
    assert r1.status_code == 200 and r2.status_code == 200
    assert "existing" not in r2.get_json()
    assert r2.get_json()["id"] != r1.get_json()["id"]
    assert len(watch_db.list_watches()) == 2


def test_add_watch_count_only_does_not_collide_with_adults_only(client, watch_db):
    """A count-only watch (children=2, child_ages=[]) saved out-of-band and an
    adults-only POST (children=0, child_ages=[]) for the SAME route/dates are
    different trips: the POST must create a distinct row, not dedup-hit the
    count-only one. The dedup key now includes the children COUNT."""
    # Seed a count-only active watch directly (e.g. created by another caller).
    count_only_id = watch_db.add_watch(
        origin="YYZ", dest_iata="PVG", dest_city="Shanghai",
        dep_date="2026-12-12", ret_date="2027-01-04",
        adults=2, children=2, threshold_pct=25.0,
    )
    # POST an adults-only watch for the same route/dates (no child_ages).
    body = {k: v for k, v in _VALID_BODY.items() if k != "child_ages"}
    r = client.post("/api/watch", json=body)
    assert r.status_code == 200
    assert "existing" not in r.get_json()              # NOT a dedup hit
    assert r.get_json()["id"] != count_only_id
    actives = watch_db.list_watches(active_only=True)
    assert len(actives) == 2
    assert {w["children"] for w in actives} == {0, 2}


def test_watch_db_helper_resolves_env(monkeypatch, tmp_path):
    """_watch_db() opens a WatchDB at the WATCH_DB env path (default fallback)."""
    db_path = tmp_path / "w.db"
    monkeypatch.setenv("WATCH_DB", str(db_path))
    db = appmod._watch_db()
    try:
        assert isinstance(db, watchmod.WatchDB)
        wid = db.add_watch(origin="YYZ", dest_iata="PVG", dest_city="Shanghai",
                           dep_date="2026-12-12", ret_date="2027-01-04")
        assert wid == 1
    finally:
        db.close()
    assert db_path.exists()


def test_add_watch_closes_db_on_error(client, monkeypatch):
    """If add_watch raises, the DB is still closed (try/finally)."""
    closed = {"v": False}

    class BoomDB:
        def list_watches(self, active_only=True):
            return []

        def add_watch(self, **kwargs):
            raise RuntimeError("boom")

        def close(self):
            closed["v"] = True

    monkeypatch.setattr(appmod, "_watch_db", lambda: BoomDB())
    with pytest.raises(RuntimeError):
        client.post("/api/watch", json=_VALID_BODY)
    assert closed["v"] is True
