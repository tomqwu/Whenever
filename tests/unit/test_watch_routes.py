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
        def add_watch(self, **kwargs):
            raise RuntimeError("boom")

        def close(self):
            closed["v"] = True

    monkeypatch.setattr(appmod, "_watch_db", lambda: BoomDB())
    with pytest.raises(RuntimeError):
        client.post("/api/watch", json=_VALID_BODY)
    assert closed["v"] is True
