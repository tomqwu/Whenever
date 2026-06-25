"""Tests for streaming search: _build_cell helper and /api/search/stream endpoint."""
import json
import app as appmod


# ---------------------------------------------------------------------------
# _build_cell helper
# ---------------------------------------------------------------------------

def test_build_cell_picks_nonstop_within_threshold():
    fare = {"cheapest_cad": 1000, "stops": 1, "nonstop_cad": 1100, "source": "test", "book": None}
    cell = appmod._build_cell("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, [11], fare, 0.25)
    assert cell["chosen"] == "nonstop"          # 1100 <= 1000 * 1.25
    assert cell["chosen_cad"] == 1100
    assert cell["dep"] == "2026-12-12"
    assert cell["ret"] == "2027-01-04"
    assert cell["cheapest_cad"] == 1000
    assert cell["nonstop_cad"] == 1100
    assert cell["source"] == "test"
    # book falls back to kayak because fare["book"] is None
    assert cell["book"].startswith("https://www.kayak.com")


def test_build_cell_picks_cheapest_when_nonstop_too_pricey():
    fare = {"cheapest_cad": 1000, "stops": 1, "nonstop_cad": 2000, "source": "test", "book": "https://b"}
    cell = appmod._build_cell("YYZ", "XXX", "2026-12-12", "2027-01-04", 2, [], fare, 0.10)
    assert cell["chosen"] == "cheapest"         # 2000 > 1000 * 1.10
    assert cell["chosen_cad"] == 1000
    assert cell["book"] == "https://b"          # provider link kept


def test_build_cell_no_data():
    fare = {"cheapest_cad": None, "stops": None, "nonstop_cad": None, "source": "no-data"}
    cell = appmod._build_cell("YYZ", "XXX", "2026-12-12", "2027-01-04", 1, [], fare, 0.25)
    assert cell["cheapest_cad"] is None
    assert cell["chosen_cad"] is None
    assert cell["source"] == "no-data"
    # kayak fallback link must still be present
    assert cell["book"].startswith("https://www.kayak.com")


# ---------------------------------------------------------------------------
# /api/search/stream endpoint
# ---------------------------------------------------------------------------

def _stream_lines(client, payload):
    """POST to /api/search/stream and return parsed NDJSON lines as a list of dicts."""
    resp = client.post("/api/search/stream", json=payload)
    return resp, [json.loads(line) for line in resp.data.split(b"\n") if line.strip()]


_STREAM_PAYLOAD = {
    "origin": "YYZ",
    "destinations": [
        {"city": "Shanghai", "iata": "PVG"},
        {"city": "Beijing", "iata": "PEK"},
    ],
    "dep_dates": ["2026-12-12", "2026-12-13"],
    "ret_dates": ["2027-01-04", "2027-01-05"],
}

_FAKE_FARE = {
    "cheapest_cad": 1000, "stops": 1, "nonstop_cad": 1100,
    "source": "test", "book": "https://example.com",
}


def test_stream_400_on_missing_origin(client):
    resp = client.post("/api/search/stream", json={
        "origin": "", "destinations": [],
        "dep_dates": ["2026-12-12"], "ret_dates": ["2027-01-04"],
    })
    assert resp.status_code == 400
    body = resp.get_json()
    assert "error" in body
    # Must NOT be streaming — content type must be JSON, not ndjson
    assert "application/json" in resp.content_type


def test_stream_400_on_missing_dates(client):
    resp = client.post("/api/search/stream", json={
        "origin": "YYZ",
        "destinations": [{"city": "X", "iata": "XXX"}],
    })
    assert resp.status_code == 400


def test_stream_first_line_is_meta(client, monkeypatch):
    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: _FAKE_FARE)
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "Best value: test")

    resp, lines = _stream_lines(client, _STREAM_PAYLOAD)
    assert resp.status_code == 200
    assert "application/x-ndjson" in resp.content_type

    meta = lines[0]
    assert meta["type"] == "meta"
    assert meta["origin"] == "YYZ"
    # 2 dests × 2 dep × 2 ret = 8 cells
    assert meta["total_cells"] == 8
    assert len(meta["results"]) == 2
    assert meta["results"][0] == {"city": "Shanghai", "iata": "PVG"}
    assert meta["results"][1] == {"city": "Beijing", "iata": "PEK"}
    assert set(meta["dep_dates"]) == {"2026-12-12", "2026-12-13"}
    assert set(meta["ret_dates"]) == {"2027-01-04", "2027-01-05"}


def test_stream_cell_count_and_shape(client, monkeypatch):
    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: _FAKE_FARE)
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "Best value: test")

    resp, lines = _stream_lines(client, _STREAM_PAYLOAD)
    cell_lines = [l for l in lines if l.get("type") == "cell"]
    assert len(cell_lines) == 8   # total_cells == 8

    for c in cell_lines:
        assert c["type"] == "cell"
        assert c["dest_index"] in (0, 1)
        assert "dep" in c
        assert "ret" in c
        assert "cheapest_cad" in c
        assert "chosen" in c
        assert "book" in c


def test_stream_recommendation_and_done(client, monkeypatch):
    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: _FAKE_FARE)
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "Best value: test")

    resp, lines = _stream_lines(client, _STREAM_PAYLOAD)
    types = [l["type"] for l in lines]
    assert types[0] == "meta"
    assert types[-1] == "done"
    assert "recommendation" in types

    rec = next(l for l in lines if l["type"] == "recommendation")
    assert rec["text"] == "Best value: test"


def test_stream_line_order(client, monkeypatch):
    """meta must come first, done last, cells in between."""
    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: _FAKE_FARE)
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "rec")

    resp, lines = _stream_lines(client, _STREAM_PAYLOAD)
    types = [l["type"] for l in lines]
    assert types[0] == "meta"
    assert types[-1] == "done"
    assert types[-2] == "recommendation"
    assert all(t == "cell" for t in types[1:-2])
