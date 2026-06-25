"""Tests for app.run_search — the pure-Python core extracted from api_search."""
import app as appmod


DEST_PVG = {"city": "Shanghai", "iata": "PVG"}
DEST_XXX = {"city": "NoWhere", "iata": "XXX"}


def _fake_fare(cheapest, stops=1, nonstop=None, source="test", book=None, duration_min=None):
    return {
        "cheapest_cad": cheapest,
        "stops": stops,
        "nonstop_cad": nonstop,
        "source": source,
        "book": book,
        "duration_min": duration_min,
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


# ---------------------------------------------------------------------------
# duration_min carries from the fare → cell → best (#53)
# ---------------------------------------------------------------------------
def test_build_cell_includes_duration_min():
    """_build_cell copies duration_min from the fare into the cell dict."""
    fare = _fake_fare(8000, stops=1, duration_min=875)
    cell = appmod._build_cell("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, [], fare, 0.25)
    assert cell["duration_min"] == 875
    # all prior keys remain present
    for k in ("dep", "ret", "cheapest_cad", "stops", "nonstop_cad",
              "chosen", "chosen_cad", "source", "book"):
        assert k in cell


def test_build_cell_duration_min_none_when_absent():
    """A fare without duration_min yields cell duration_min None (no fabrication)."""
    fare = {"cheapest_cad": 8000, "stops": 1, "nonstop_cad": None, "source": "x"}
    cell = appmod._build_cell("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, [], fare, 0.25)
    assert cell["duration_min"] is None


def test_run_search_best_carries_duration_min(monkeypatch):
    monkeypatch.setattr(appmod, "get_fare",
                        lambda *a, **k: _fake_fare(1000, duration_min=600))
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "rec")
    out = appmod.run_search(
        origin="YYZ", dests=[DEST_PVG], adults=2, child_ages=[],
        dep_dates=["2026-12-12"], ret_dates=["2027-01-04"],
    )
    assert out["results"][0]["best"]["duration_min"] == 600


# ---------------------------------------------------------------------------
# build_recommendation: summary + prompt factor in total duration (#53)
# ---------------------------------------------------------------------------
def _result_with_best(duration_min):
    return [{
        "city": "Shanghai", "iata": "PVG",
        "best": {"chosen_cad": 8000, "dep": "2026-12-12", "ret": "2027-01-04",
                 "chosen": "cheapest", "stops": 1, "duration_min": duration_min},
    }]


def test_build_recommendation_summary_includes_duration(monkeypatch):
    captured = {}

    def fake_chat(prompt, *a, **k):
        captured["prompt"] = prompt
        return "AI says go to Shanghai."

    monkeypatch.setattr(appmod, "ollama_chat", fake_chat)
    out = appmod.build_recommendation("YYZ", _result_with_best(875), 2, [], 1)
    assert out == "AI says go to Shanghai."
    p = captured["prompt"]
    # human + machine duration both present in the JSON summary
    assert "14h 35m" in p
    assert "875" in p
    assert "duration_min" in p
    # prompt instructs the model to balance/avoid much-longer flights
    assert "duration" in p.lower()
    assert "2x" in p or "2×" in p


def test_build_recommendation_duration_none_in_summary(monkeypatch):
    captured = {}
    monkeypatch.setattr(appmod, "ollama_chat",
                        lambda prompt, *a, **k: captured.setdefault("prompt", prompt) or "ok")
    appmod.build_recommendation("YYZ", _result_with_best(None), 2, [], 1)
    # null duration is serialized, not fabricated
    assert '"duration": null' in captured["prompt"]
    assert '"duration_min": null' in captured["prompt"]


def test_build_recommendation_fallback_still_works(monkeypatch):
    """When ollama fails, the deterministic price fallback still returns a pick."""
    def boom(*a, **k):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(appmod, "ollama_chat", boom)
    out = appmod.build_recommendation("YYZ", _result_with_best(875), 2, [], 1)
    assert "Shanghai" in out
    assert "8,000" in out


def test_fmt_duration_helper():
    assert appmod._fmt_duration(875) == "14h 35m"
    assert appmod._fmt_duration(0) == "0h 0m"
    assert appmod._fmt_duration(None) is None
