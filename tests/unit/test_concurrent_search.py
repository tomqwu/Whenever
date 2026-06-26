"""TDD tests for concurrent fare-grid fetching in run_search.

Written BEFORE the production code (Red → Green → Refactor).

Tests:
1. Each cell is called exactly once — no double-fetching.
2. Grid assembles results in correct dep×ret position per dest.
3. results list preserves original dests order.
4. Concurrency: >1 distinct worker thread observed when SEARCH_CONCURRENCY > 1.
5. SEARCH_CONCURRENCY=1 still produces identical (correct) output.
6. SEARCH_CONCURRENCY config var exists on the module.
"""
import threading
import time
import app as appmod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fare_from_args(origin, dest, dep, ret, adults, children, compare=False,
                    nonstop_threshold=0.0):
    """Return a deterministic price derived from the call arguments so we can
    assert each grid position got the right value (proves no cell mismapping
    under parallel assembly)."""
    price = abs(hash((dest, dep, ret))) % 9000 + 500
    return {
        "cheapest_cad": price,
        "stops": 1,
        "nonstop_cad": None,
        "source": "stub",
        "book": None,
    }


# ---------------------------------------------------------------------------
# 1.  SEARCH_CONCURRENCY config var exists on the module
# ---------------------------------------------------------------------------
def test_search_concurrency_config_exists():
    """SEARCH_CONCURRENCY must be a module-level int attribute (default >= 1)."""
    assert hasattr(appmod, "SEARCH_CONCURRENCY"), (
        "appmod.SEARCH_CONCURRENCY not found — add it near the other config vars"
    )
    assert isinstance(appmod.SEARCH_CONCURRENCY, int)
    assert appmod.SEARCH_CONCURRENCY >= 1


# ---------------------------------------------------------------------------
# 2.  Each cell called exactly once — no double-fetching
# ---------------------------------------------------------------------------
def test_each_cell_called_exactly_once(monkeypatch):
    """get_fare must be invoked exactly once per (dest, dep, ret) triple."""
    call_log = []

    def counting_fare(origin, dest, dep, ret, adults, children, compare=False,
                      nonstop_threshold=0.0):
        call_log.append((dest, dep, ret))
        return _fare_from_args(origin, dest, dep, ret, adults, children)

    monkeypatch.setattr(appmod, "get_fare", counting_fare)
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "rec")

    dep_dates = ["2026-12-10", "2026-12-11"]
    ret_dates = ["2027-01-04", "2027-01-05"]
    dests = [{"city": "Shanghai", "iata": "PVG"}, {"city": "Beijing", "iata": "PEK"}]

    appmod.run_search(
        origin="YYZ",
        dests=dests,
        adults=2,
        child_ages=[],
        dep_dates=dep_dates,
        ret_dates=ret_dates,
    )

    expected_calls = len(dests) * len(dep_dates) * len(ret_dates)
    assert len(call_log) == expected_calls, (
        f"Expected {expected_calls} calls, got {len(call_log)}"
    )
    # No duplicate (dest, dep, ret) triples
    assert len(set(call_log)) == len(call_log), "Duplicate get_fare calls detected"


# ---------------------------------------------------------------------------
# 3.  Grid positions are correct (no cell mismapping under parallel assembly)
# ---------------------------------------------------------------------------
def test_grid_cells_placed_in_correct_position(monkeypatch):
    """Each grid[dep_idx][ret_idx] must contain the fare for that specific
    (dep, ret) pair. Uses a price derived from args so a swap would be caught."""
    monkeypatch.setattr(appmod, "get_fare", _fare_from_args)
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "rec")

    dep_dates = ["2026-12-10", "2026-12-11", "2026-12-12"]
    ret_dates = ["2027-01-04", "2027-01-05"]

    out = appmod.run_search(
        origin="YYZ",
        dests=[{"city": "Shanghai", "iata": "PVG"}],
        adults=2,
        child_ages=[],
        dep_dates=dep_dates,
        ret_dates=ret_dates,
    )

    grid = out["results"][0]["grid"]
    assert len(grid) == len(dep_dates), "Wrong number of rows"
    assert all(len(row) == len(ret_dates) for row in grid), "Wrong number of cols"

    for di, dep in enumerate(dep_dates):
        for ri, ret in enumerate(ret_dates):
            cell = grid[di][ri]
            assert cell["dep"] == dep, f"grid[{di}][{ri}].dep wrong: {cell['dep']}"
            assert cell["ret"] == ret, f"grid[{di}][{ri}].ret wrong: {cell['ret']}"
            expected_price = _fare_from_args("YYZ", "PVG", dep, ret, 2, 0)["cheapest_cad"]
            assert cell["cheapest_cad"] == expected_price, (
                f"grid[{di}][{ri}] price mismatch: got {cell['cheapest_cad']}, "
                f"want {expected_price}"
            )


# ---------------------------------------------------------------------------
# 4.  results list preserves original dests order
# ---------------------------------------------------------------------------
def test_results_order_matches_dests_order(monkeypatch):
    """results must be in the same order as the input dests list."""
    monkeypatch.setattr(appmod, "get_fare", _fare_from_args)
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "rec")

    dests = [
        {"city": "Shanghai", "iata": "PVG"},
        {"city": "Beijing", "iata": "PEK"},
        {"city": "Hong Kong", "iata": "HKG"},
        {"city": "Tokyo", "iata": "NRT"},
    ]

    out = appmod.run_search(
        origin="YYZ",
        dests=dests,
        adults=2,
        child_ages=[],
        dep_dates=["2026-12-10", "2026-12-11"],
        ret_dates=["2027-01-04"],
    )

    result_iatas = [r["iata"] for r in out["results"]]
    expected_iatas = [d["iata"] for d in dests]
    assert result_iatas == expected_iatas, (
        f"Ordering mismatch: got {result_iatas}, want {expected_iatas}"
    )


# ---------------------------------------------------------------------------
# 5.  Concurrency: >1 distinct worker thread observed
# ---------------------------------------------------------------------------
def test_calls_run_in_multiple_threads(monkeypatch):
    """When SEARCH_CONCURRENCY > 1, get_fare must run on >1 distinct threads,
    proving the calls are concurrent (not serialized).

    We use a tiny sleep (5 ms) inside the stub so threads have a chance to
    overlap, then assert >1 distinct thread names were seen.
    """
    monkeypatch.setattr(appmod, "SEARCH_CONCURRENCY", 4)
    thread_names = set()
    lock = threading.Lock()

    def threaded_fare(origin, dest, dep, ret, adults, children, compare=False,
                      nonstop_threshold=0.0):
        with lock:
            thread_names.add(threading.current_thread().name)
        time.sleep(0.005)  # 5 ms — small enough for fast tests
        return _fare_from_args(origin, dest, dep, ret, adults, children)

    monkeypatch.setattr(appmod, "get_fare", threaded_fare)
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "rec")

    dep_dates = ["2026-12-10", "2026-12-11", "2026-12-12", "2026-12-13"]
    ret_dates = ["2027-01-04", "2027-01-05", "2027-01-06", "2027-01-07"]

    appmod.run_search(
        origin="YYZ",
        dests=[{"city": "Shanghai", "iata": "PVG"}, {"city": "Beijing", "iata": "PEK"}],
        adults=2,
        child_ages=[],
        dep_dates=dep_dates,
        ret_dates=ret_dates,
    )

    assert len(thread_names) > 1, (
        f"Expected >1 distinct worker threads, got: {thread_names!r}. "
        "run_search may still be sequential."
    )


# ---------------------------------------------------------------------------
# 6.  SEARCH_CONCURRENCY=1 produces correct identical output
# ---------------------------------------------------------------------------
def test_concurrency_one_still_correct(monkeypatch):
    """With SEARCH_CONCURRENCY=1 the output must still be fully correct
    (same shape, correct cell values, correct ordering)."""
    monkeypatch.setattr(appmod, "SEARCH_CONCURRENCY", 1)
    monkeypatch.setattr(appmod, "get_fare", _fare_from_args)
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "rec")

    dep_dates = ["2026-12-10", "2026-12-11"]
    ret_dates = ["2027-01-04", "2027-01-05"]
    dests = [{"city": "Shanghai", "iata": "PVG"}, {"city": "Beijing", "iata": "PEK"}]

    out = appmod.run_search(
        origin="YYZ",
        dests=dests,
        adults=2,
        child_ages=[],
        dep_dates=dep_dates,
        ret_dates=ret_dates,
    )

    assert [r["iata"] for r in out["results"]] == ["PVG", "PEK"]
    for result in out["results"]:
        code = result["iata"]
        grid = result["grid"]
        assert len(grid) == len(dep_dates)
        for di, dep in enumerate(dep_dates):
            for ri, ret in enumerate(ret_dates):
                cell = grid[di][ri]
                assert cell["dep"] == dep
                assert cell["ret"] == ret
                expected = _fare_from_args("YYZ", code, dep, ret, 2, 0)["cheapest_cad"]
                assert cell["cheapest_cad"] == expected
