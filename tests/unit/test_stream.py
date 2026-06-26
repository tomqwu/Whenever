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


def test_stream_cells_map_to_correct_dep_ret(client, monkeypatch):
    """Each cell line must carry the price assigned to its exact (dep, ret) pair.

    The mock returns a distinct cheapest_cad for every (dep, ret) combination by
    encoding the dep/ret dates into the price.  This proves that out-of-order
    completion does not mix up cells across grid slots.
    """
    # 1 destination, 2 dep_dates x 2 ret_dates => 4 distinct cells
    dep_dates = ["2026-12-10", "2026-12-11"]
    ret_dates = ["2027-01-03", "2027-01-04"]

    # Build an expected price table: price = 1000 + dep_day*10 + ret_day
    # dep_day: 10 or 11; ret_day: 3 or 4 -> prices 1103, 1104, 1113, 1114
    def _price_for(dep, ret):
        dep_day = int(dep.split("-")[2])   # 10 or 11
        ret_day = int(ret.split("-")[2])   # 3 or 4
        return 1000 + dep_day * 10 + ret_day

    def fake_get_fare(origin, dest, dep, ret, adults, children, compare=False,
                      nonstop_threshold=0.0):
        price = _price_for(dep, ret)
        return {
            "cheapest_cad": price,
            "stops": 1,
            "nonstop_cad": None,
            "source": "test",
            "book": None,
        }

    monkeypatch.setattr(appmod, "get_fare", fake_get_fare)
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "rec")

    payload = {
        "origin": "YYZ",
        "destinations": [{"city": "Tokyo", "iata": "NRT"}],
        "dep_dates": dep_dates,
        "ret_dates": ret_dates,
    }
    resp, lines = _stream_lines(client, payload)
    assert resp.status_code == 200

    cell_lines = [l for l in lines if l.get("type") == "cell"]
    assert len(cell_lines) == 4

    # Verify each cell carries the price that was assigned to its (dep, ret)
    for cell in cell_lines:
        expected_price = _price_for(cell["dep"], cell["ret"])
        assert cell["cheapest_cad"] == expected_price, (
            f"Cell dep={cell['dep']} ret={cell['ret']} expected cheapest_cad="
            f"{expected_price} but got {cell['cheapest_cad']}"
        )


# ---------------------------------------------------------------------------
# Resilience: per-cell get_fare failure must NOT truncate the stream
# ---------------------------------------------------------------------------

def test_stream_tolerates_get_fare_exception(client, monkeypatch):
    """If get_fare raises for every cell, the stream must still emit all cell
    lines (with no-data sentinel values) AND end with recommendation + done.

    This test verifies the fix introduced for the robustness finding: a raising
    get_fare inside the ThreadPoolExecutor worker must not propagate through
    fut.result() and truncate the stream mid-flight.
    """

    def always_raises(origin, dest, dep, ret, adults, children, compare=False):
        raise RuntimeError("provider down")

    monkeypatch.setattr(appmod, "get_fare", always_raises)
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "fallback rec")

    payload = {
        "origin": "YYZ",
        "destinations": [{"city": "Shanghai", "iata": "PVG"}],
        "dep_dates": ["2026-12-12"],
        "ret_dates": ["2027-01-04"],
    }
    # 1 dest × 1 dep × 1 ret = 1 cell
    resp, lines = _stream_lines(client, payload)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

    types = [l["type"] for l in lines]

    # Stream must end with recommendation then done — never truncated
    assert "done" in types, f"Stream truncated — no 'done' line. types={types}"
    assert "recommendation" in types, f"No 'recommendation' line. types={types}"
    assert types[-1] == "done"
    assert types[-2] == "recommendation"

    # Must still emit exactly 1 cell line
    cell_lines = [l for l in lines if l["type"] == "cell"]
    assert len(cell_lines) == 1, f"Expected 1 cell line, got {len(cell_lines)}"

    # Failing cell must carry no-data sentinel values
    cell = cell_lines[0]
    assert cell["cheapest_cad"] is None, f"Expected None cheapest_cad, got {cell['cheapest_cad']}"
    assert cell["source"] == "no-data", f"Expected source='no-data', got {cell['source']}"

    # Recommendation must still have been emitted with the monkeypatched text
    rec = next(l for l in lines if l["type"] == "recommendation")
    assert rec["text"] == "fallback rec"


# ---------------------------------------------------------------------------
# Determinism: streamed recommendation picks the SAME best cell as run_search
# when prices are TIED across different dep/ret positions.
# ---------------------------------------------------------------------------

# A fare table with TIED cheapest_cad (700) in TWO distinct dep/ret positions.
# For PVG the tie is at (dep0, ret1) and (dep1, ret0); for run_search the
# original-order min() resolves to the FIRST in dep×ret order: (dep0, ret1).
_TIE_PRICES = {
    ("PVG", "2026-12-12", "2027-01-04"): 900,
    ("PVG", "2026-12-12", "2027-01-05"): 700,   # tie A (earlier in order)
    ("PVG", "2026-12-13", "2027-01-04"): 700,   # tie B (later in order)
    ("PVG", "2026-12-13", "2027-01-05"): 950,
    ("PEK", "2026-12-12", "2027-01-04"): 1200,
    ("PEK", "2026-12-12", "2027-01-05"): 1200,
    ("PEK", "2026-12-13", "2027-01-04"): 1200,
    ("PEK", "2026-12-13", "2027-01-05"): 1200,
}


def _tie_fare(origin, dest, dep, ret, adults, children, compare=False,
              nonstop_threshold=0.0):
    return {
        "cheapest_cad": _TIE_PRICES[(dest, dep, ret)],
        "stops": 1, "nonstop_cad": None, "source": "test", "book": None,
    }


def _capture_best(monkeypatch):
    """Patch build_recommendation to record the per-city `best` cells it sees;
    return the recorder list (filled when build_recommendation is invoked)."""
    captured = []

    def recorder(origin, results, adults, child_ages, families):
        captured.append([
            None if r["best"] is None
            else {"city": r["city"], "dep": r["best"]["dep"], "ret": r["best"]["ret"],
                  "chosen_cad": r["best"]["chosen_cad"]}
            for r in results
        ])
        return "rec"

    monkeypatch.setattr(appmod, "build_recommendation", recorder)
    return captured


def test_streamed_best_matches_run_search_on_ties(client, monkeypatch):
    """For tied prices in different dep/ret positions, the streamed best (used
    for the recommendation) must resolve to the SAME cell run_search picks.

    Because cells arrive in as_completed (nondeterministic) order, the stream
    must rebuild the grid in ORIGINAL dep×ret order before computing
    min(chosen_cad), so the tie resolves identically to /api/search.
    """
    monkeypatch.setattr(appmod, "get_fare", _tie_fare)

    # 1) Capture what run_search picks.
    rs_captured = _capture_best(monkeypatch)
    appmod.run_search(
        origin="YYZ",
        dests=[{"city": "Shanghai", "iata": "PVG"}, {"city": "Beijing", "iata": "PEK"}],
        adults=2, child_ages=[],
        dep_dates=["2026-12-12", "2026-12-13"],
        ret_dates=["2027-01-04", "2027-01-05"],
    )
    run_search_best = rs_captured[-1]

    # 2) Capture what the streaming endpoint picks.
    stream_captured = _capture_best(monkeypatch)
    resp, _ = _stream_lines(client, {
        "origin": "YYZ",
        "destinations": [{"city": "Shanghai", "iata": "PVG"},
                         {"city": "Beijing", "iata": "PEK"}],
        "dep_dates": ["2026-12-12", "2026-12-13"],
        "ret_dates": ["2027-01-04", "2027-01-05"],
    })
    assert resp.status_code == 200
    stream_best = stream_captured[-1]

    # The streamed best must be IDENTICAL to run_search's best, city-by-city.
    assert stream_best == run_search_best
    # And the PVG tie must resolve to the FIRST cell in dep×ret order.
    pvg_best = next(b for b in stream_best if b and b["city"] == "Shanghai")
    assert pvg_best["chosen_cad"] == 700
    assert pvg_best["dep"] == "2026-12-12"
    assert pvg_best["ret"] == "2027-01-05"
