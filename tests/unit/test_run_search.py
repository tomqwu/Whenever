"""Tests for app.run_search — the pure-Python core extracted from api_search."""
import app as appmod


DEST_PVG = {"city": "Shanghai", "iata": "PVG"}
DEST_XXX = {"city": "NoWhere", "iata": "XXX"}


def _fake_fare(cheapest, stops=1, nonstop=None, source="test", book=None):
    return {
        "cheapest_cad": cheapest,
        "stops": stops,
        "nonstop_cad": nonstop,
        "source": source,
        "book": book,
    }


# ---------------------------------------------------------------------------
# Structural contract: run_search returns the expected top-level keys
# ---------------------------------------------------------------------------
def test_run_search_returns_expected_keys(monkeypatch):
    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: _fake_fare(1000))
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "Best pick")

    out = appmod.run_search(
        origin="YYZ",
        dests=[DEST_PVG],
        adults=2,
        child_ages=[11],
        dep_dates=["2026-12-12"],
        ret_dates=["2027-01-04"],
    )
    for key in ("origin", "adults", "child_ages", "families", "dep_dates", "ret_dates",
                "results", "recommendation", "providers"):
        assert key in out, f"missing key: {key}"


# ---------------------------------------------------------------------------
# Nonstop-within-threshold rule: picks nonstop when premium <= threshold_pct
# ---------------------------------------------------------------------------
def test_run_search_picks_nonstop_within_threshold(monkeypatch):
    monkeypatch.setattr(appmod, "get_fare",
                        lambda *a, **k: _fake_fare(1000, stops=1, nonstop=1100))
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "rec")

    out = appmod.run_search(
        origin="YYZ",
        dests=[DEST_PVG],
        adults=2,
        child_ages=[],
        dep_dates=["2026-12-12"],
        ret_dates=["2027-01-04"],
        threshold_pct=25,
    )
    cell = out["results"][0]["grid"][0][0]
    assert cell["chosen"] == "nonstop"   # 1100 <= 1000 * 1.25
    assert cell["chosen_cad"] == 1100


# ---------------------------------------------------------------------------
# Threshold rule: sticks with cheapest when nonstop premium > threshold_pct
# ---------------------------------------------------------------------------
def test_run_search_picks_cheapest_when_nonstop_too_pricey(monkeypatch):
    monkeypatch.setattr(appmod, "get_fare",
                        lambda *a, **k: _fake_fare(1000, stops=1, nonstop=2000))
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "rec")

    out = appmod.run_search(
        origin="YYZ",
        dests=[DEST_PVG],
        adults=2,
        child_ages=[],
        dep_dates=["2026-12-12"],
        ret_dates=["2027-01-04"],
        threshold_pct=10,
    )
    cell = out["results"][0]["grid"][0][0]
    assert cell["chosen"] == "cheapest"  # 2000 > 1000 * 1.10
    assert cell["chosen_cad"] == 1000


# ---------------------------------------------------------------------------
# No-data cells: best is None, book falls back to kayak link
# ---------------------------------------------------------------------------
def test_run_search_no_data_best_is_none(monkeypatch):
    monkeypatch.setattr(appmod, "get_fare",
                        lambda *a, **k: _fake_fare(None, stops=None, nonstop=None,
                                                   source="no-data"))
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "rec")

    out = appmod.run_search(
        origin="YYZ",
        dests=[DEST_PVG],
        adults=2,
        child_ages=[],
        dep_dates=["2026-12-12"],
        ret_dates=["2027-01-04"],
    )
    result = out["results"][0]
    assert result["best"] is None
    assert out["results"][0]["grid"][0][0]["book"].startswith("https://www.kayak.com")


# ---------------------------------------------------------------------------
# Multi-city: each city gets its own grid, best is per city
# ---------------------------------------------------------------------------
def test_run_search_multi_city_best_per_city(monkeypatch):
    prices = {"PVG": 800, "PEK": 1200}

    def fake_fare(origin, dest, dep, ret, adults, children):
        return _fake_fare(prices[dest])

    monkeypatch.setattr(appmod, "get_fare", fake_fare)
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "rec")

    out = appmod.run_search(
        origin="YYZ",
        dests=[{"city": "Shanghai", "iata": "PVG"}, {"city": "Beijing", "iata": "PEK"}],
        adults=2,
        child_ages=[],
        dep_dates=["2026-12-12"],
        ret_dates=["2027-01-04"],
    )
    assert len(out["results"]) == 2
    pvg = next(r for r in out["results"] if r["iata"] == "PVG")
    pek = next(r for r in out["results"] if r["iata"] == "PEK")
    assert pvg["best"]["chosen_cad"] == 800
    assert pek["best"]["chosen_cad"] == 1200


# ---------------------------------------------------------------------------
# build_recommendation is called with the right arguments
# ---------------------------------------------------------------------------
def test_run_search_calls_build_recommendation_with_results(monkeypatch):
    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: _fake_fare(500))
    calls = []

    def capture_rec(origin, results, adults, child_ages, families):
        calls.append({"origin": origin, "results": results, "adults": adults,
                       "child_ages": child_ages, "families": families})
        return "AI pick"

    monkeypatch.setattr(appmod, "build_recommendation", capture_rec)

    out = appmod.run_search(
        origin="YYZ",
        dests=[DEST_PVG],
        adults=3,
        child_ages=[9, 12],
        dep_dates=["2026-12-12"],
        ret_dates=["2027-01-04"],
        families=2,
    )
    assert out["recommendation"] == "AI pick"
    assert calls[0]["origin"] == "YYZ"
    assert calls[0]["adults"] == 3
    assert calls[0]["child_ages"] == [9, 12]
    assert calls[0]["families"] == 2


# ---------------------------------------------------------------------------
# Result shape includes the city/iata passthrough
# ---------------------------------------------------------------------------
def test_run_search_result_preserves_city_and_iata(monkeypatch):
    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: _fake_fare(600))
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "")

    out = appmod.run_search(
        origin="YYZ",
        dests=[DEST_PVG],
        adults=2,
        child_ages=[],
        dep_dates=["2026-12-12"],
        ret_dates=["2027-01-04"],
    )
    r = out["results"][0]
    assert r["city"] == "Shanghai"
    assert r["iata"] == "PVG"
