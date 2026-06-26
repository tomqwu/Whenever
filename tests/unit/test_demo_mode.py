"""Unit tests for opt-in, clearly-labeled demo/sample mode (#44).

Demo mode is the SOLE sanctioned exception to the real-data-only guardrail, and
ONLY because it is explicit (DEMO_MODE flag) and unmistakably labeled. These tests
pin the guarantees that make that safe:
  - demo_fare is a valid normalized dict, tagged source="demo", deterministic,
    varies by route/date, and is never None-priced.
  - With DEMO_MODE on, get_fare/run_search return demo cells and NEVER call a real
    provider; demo data is NEVER written to the real fare cache.
  - With DEMO_MODE off, no demo data ever appears (real path unchanged) and a
    provider failure yields the no-data sentinel — demo is NEVER a silent fallback.
  - /api/health reports demo mode.
"""
import json

import pytest

import app as appmod
import watch as watchmod
import scheduler as schedmod


# Every real provider — patched to BLOW UP if demo mode ever calls one.
_REAL_PROVIDERS = (
    "skyscanner_fare", "serpapi_fare", "amadeus_fare",
    "travelpayouts_fare", "kiwi_fare",
)


def _boom(*a, **k):
    raise AssertionError("real provider called in demo mode")


@pytest.fixture
def no_real_providers(monkeypatch):
    """Make every real provider raise if invoked, so any call is caught."""
    for name in _REAL_PROVIDERS:
        monkeypatch.setattr(appmod, name, _boom)
    return monkeypatch


# --------------------------- demo_fare shape/determinism ---------------------------

def test_demo_fare_normalized_shape_and_tag():
    f = appmod.demo_fare("YYZ", "PVG", "2026-07-01", "2026-07-15", 2, 1)
    assert f["source"] == "demo"
    # All normalized keys present.
    for key in ("cheapest_cad", "stops", "nonstop_cad", "duration_min",
                "nonstop_duration_min", "airlines", "layovers",
                "nonstop_airlines", "book"):
        assert key in f, key
    # Never None-priced.
    assert isinstance(f["cheapest_cad"], int) and f["cheapest_cad"] > 0
    assert isinstance(f["nonstop_cad"], int) and f["nonstop_cad"] > 0
    # Obviously-sample carrier.
    assert f["airlines"] == ["DemoAir"]
    assert f["nonstop_airlines"] == ["DemoAir"]
    assert isinstance(f["stops"], int) and 0 <= f["stops"] <= 2
    assert isinstance(f["duration_min"], int) and f["duration_min"] > 0


def test_demo_fare_deterministic_same_inputs():
    a = appmod.demo_fare("YYZ", "PVG", "2026-07-01", "2026-07-15", 2, 1)
    b = appmod.demo_fare("YYZ", "PVG", "2026-07-01", "2026-07-15", 2, 1)
    assert a == b


def test_demo_fare_varies_by_route():
    a = appmod.demo_fare("YYZ", "PVG", "2026-07-01", "2026-07-15", 2, 1)
    b = appmod.demo_fare("YYZ", "PEK", "2026-07-01", "2026-07-15", 2, 1)
    assert a["cheapest_cad"] != b["cheapest_cad"]


def test_demo_fare_varies_by_date():
    a = appmod.demo_fare("YYZ", "PVG", "2026-07-01", "2026-07-15", 2, 1)
    b = appmod.demo_fare("YYZ", "PVG", "2026-08-01", "2026-08-15", 2, 1)
    assert a["cheapest_cad"] != b["cheapest_cad"]


def test_demo_fare_layovers_consistent_with_stops():
    """A non-nonstop sample has a clearly-fake layover; a nonstop sample has none."""
    # Search a few cells to exercise both branches deterministically.
    saw_stops = saw_nonstop = False
    for dest in ("PVG", "PEK", "CAN", "HKG", "TPE", "NRT", "ICN", "BKK"):
        f = appmod.demo_fare("YYZ", dest, "2026-07-01", "2026-07-15", 2, 1)
        if f["stops"]:
            saw_stops = True
            assert f["layovers"] and f["layovers"][0]["iata"] in {"DMO", "FAK", "SMP", "XXX"}
        else:
            saw_nonstop = True
            assert f["layovers"] == []
    assert saw_stops and saw_nonstop


def test_demo_fare_party_size_changes_price():
    solo = appmod.demo_fare("YYZ", "PVG", "2026-07-01", "2026-07-15", 1, 0)
    family = appmod.demo_fare("YYZ", "PVG", "2026-07-01", "2026-07-15", 2, 2)
    assert family["cheapest_cad"] != solo["cheapest_cad"]


# --------------------------- get_fare wiring ---------------------------

def test_get_fare_returns_demo_when_demo_on(monkeypatch, no_real_providers):
    monkeypatch.setattr(appmod, "DEMO_MODE", True)
    f = appmod.get_fare("YYZ", "PVG", "2026-07-01", "2026-07-15", 2, 1)
    assert f["source"] == "demo"
    assert f["cheapest_cad"] > 0


def test_get_fare_demo_with_compare_is_harmless(monkeypatch, no_real_providers):
    """Compare flag in demo mode just returns the demo result (no real compare)."""
    monkeypatch.setattr(appmod, "DEMO_MODE", True)
    f = appmod.get_fare("YYZ", "PVG", "2026-07-01", "2026-07-15", 2, 1,
                        compare=True, nonstop_threshold=0.25)
    assert f["source"] == "demo"
    assert f["cheapest_cad"] > 0


def test_uncached_demo_does_not_call_providers(monkeypatch, no_real_providers):
    monkeypatch.setattr(appmod, "DEMO_MODE", True)
    f = appmod._get_fare_uncached("YYZ", "PVG", "2026-07-01", "2026-07-15", 2, 1)
    assert f["source"] == "demo"


def test_demo_not_written_to_real_cache(monkeypatch, tmp_path, no_real_providers):
    """Demo fares must NEVER land in the real persistent fare cache."""
    cache_path = tmp_path / "fare_cache.json"
    monkeypatch.setattr(appmod, "DEMO_MODE", True)
    monkeypatch.setattr(appmod, "FARE_CACHE_PATH", str(cache_path))
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", 3600)
    appmod._fare_cache.clear()
    f = appmod.get_fare("YYZ", "PVG", "2026-07-01", "2026-07-15", 2, 1)
    assert f["source"] == "demo"
    # Nothing persisted to disk and nothing in the in-memory real cache.
    assert not cache_path.exists()
    assert appmod._fare_cache == {}


def test_run_search_demo_cells(monkeypatch, no_real_providers):
    monkeypatch.setattr(appmod, "DEMO_MODE", True)
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "(DEMO) ok")
    out = appmod.run_search(
        "YYZ", [{"city": "Shanghai", "iata": "PVG"}], 2, [5],
        ["2026-07-01"], ["2026-07-15"],
    )
    cell = out["results"][0]["grid"][0][0]
    assert cell["source"] == "demo"
    assert cell["chosen_cad"] > 0
    assert out["providers"] == ["demo"]


# --------------------------- demo OFF: real path unchanged ---------------------------

def test_demo_off_no_demo_data(monkeypatch):
    """DEMO_MODE off: a configured provider's real result flows through unchanged."""
    monkeypatch.setattr(appmod, "DEMO_MODE", False)
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod, "skyscanner_fare",
                        lambda *a: {"cheapest_cad": 1234, "source": "skyscanner"})
    f = appmod.get_fare("YYZ", "PVG", "2026-07-01", "2026-07-15", 2, 1)
    assert f["source"] == "skyscanner"
    assert f["source"] != "demo"


def test_demo_off_provider_failure_is_no_data_not_demo(monkeypatch):
    """A provider failure with demo OFF yields the no-data sentinel — NEVER demo.

    Demo is opt-in only and must never be a silent fallback.
    """
    monkeypatch.setattr(appmod, "DEMO_MODE", False)
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod, "skyscanner_fare", lambda *a: None)
    f = appmod.get_fare("YYZ", "PVG", "2026-07-01", "2026-07-15", 2, 1)
    assert f["source"] == "no-data"
    assert f["cheapest_cad"] is None


def test_demo_off_no_providers_is_no_data_not_demo(monkeypatch):
    """No providers configured + demo OFF -> no-data sentinel, never demo."""
    monkeypatch.setattr(appmod, "DEMO_MODE", False)
    f = appmod.get_fare("YYZ", "PVG", "2026-07-01", "2026-07-15", 2, 1)
    assert f["source"] == "no-data"


# --------------------------- providers_configured / health ---------------------------

def test_providers_configured_demo(monkeypatch):
    monkeypatch.setattr(appmod, "DEMO_MODE", True)
    assert appmod.providers_configured() == ["demo"]


def test_health_reports_demo_on(client, monkeypatch):
    monkeypatch.setattr(appmod, "DEMO_MODE", True)
    monkeypatch.setattr(appmod, "ollama_ok", lambda: False)
    resp = client.get("/api/health")
    data = resp.get_json()
    assert data["demo"] is True
    assert data["providers"] == ["demo"]


def test_health_reports_demo_off(client, monkeypatch):
    monkeypatch.setattr(appmod, "DEMO_MODE", False)
    monkeypatch.setattr(appmod, "ollama_ok", lambda: False)
    resp = client.get("/api/health")
    data = resp.get_json()
    assert data["demo"] is False
    assert "demo" not in data["providers"]


# --------------------------- recommendation labeling ---------------------------

def test_recommendation_labeled_demo_llm(monkeypatch):
    """Even the AI summary is prefixed (DEMO ...) in demo mode."""
    monkeypatch.setattr(appmod, "DEMO_MODE", True)
    monkeypatch.setattr(appmod, "ollama_chat", lambda *a, **k: "Best value: PVG")
    results = [{"city": "Shanghai", "iata": "PVG", "best": {
        "chosen_cad": 3363, "dep": "2026-07-01", "ret": "2026-07-15",
        "chosen": "cheapest", "chosen_stops": 0, "chosen_duration_min": 800,
        "chosen_airlines": ["DemoAir"], "chosen_layovers": []}}]
    text = appmod.build_recommendation("YYZ", results, 2, [5], 1)
    assert text.startswith("(DEMO")
    assert "Best value: PVG" in text


def test_recommendation_prompt_does_not_claim_live_apis_in_demo(monkeypatch):
    """The LLM prompt must NOT claim demo data came from live flight APIs (#44).

    Even though the returned text is prefixed (DEMO …), the prompt itself must
    describe locally generated SAMPLE data so the model never analyzes demo
    numbers as if they were real fares.
    """
    captured = {}

    def fake_chat(prompt, *a, **k):
        captured["prompt"] = prompt
        return "Best value: PVG"

    monkeypatch.setattr(appmod, "DEMO_MODE", True)
    monkeypatch.setattr(appmod, "ollama_chat", fake_chat)
    results = [{"city": "Shanghai", "iata": "PVG", "best": {
        "chosen_cad": 3363, "dep": "2026-07-01", "ret": "2026-07-15",
        "chosen": "cheapest", "chosen_stops": 0, "chosen_duration_min": 800,
        "chosen_airlines": ["DemoAir"], "chosen_layovers": []}}]
    appmod.build_recommendation("YYZ", results, 2, [5], 1)
    assert "COLLECTED FROM LIVE FLIGHT APIs" not in captured["prompt"]
    assert "SAMPLE DATA" in captured["prompt"]


def test_recommendation_prompt_claims_live_apis_when_not_demo(monkeypatch):
    """Real mode keeps the live-API provenance wording (unchanged path)."""
    captured = {}

    def fake_chat(prompt, *a, **k):
        captured["prompt"] = prompt
        return "Best value: PVG"

    monkeypatch.setattr(appmod, "DEMO_MODE", False)
    monkeypatch.setattr(appmod, "ollama_chat", fake_chat)
    results = [{"city": "Shanghai", "iata": "PVG", "best": {
        "chosen_cad": 3363, "dep": "2026-07-01", "ret": "2026-07-15",
        "chosen": "cheapest", "chosen_stops": 0, "chosen_duration_min": 800,
        "chosen_airlines": ["AC"], "chosen_layovers": []}}]
    appmod.build_recommendation("YYZ", results, 2, [5], 1)
    assert "COLLECTED FROM LIVE FLIGHT APIs" in captured["prompt"]


def test_recommendation_labeled_demo_fallback(monkeypatch):
    """When ollama is unavailable, the fallback summary is still labeled demo."""
    monkeypatch.setattr(appmod, "DEMO_MODE", True)

    def boom(*a, **k):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(appmod, "ollama_chat", boom)
    results = [{"city": "Shanghai", "iata": "PVG", "best": {
        "chosen_cad": 3363, "dep": "2026-07-01", "ret": "2026-07-15",
        "chosen": "cheapest", "chosen_stops": 0, "chosen_duration_min": 800,
        "chosen_airlines": ["DemoAir"], "chosen_layovers": []}}]
    text = appmod.build_recommendation("YYZ", results, 2, [5], 1)
    assert text.startswith("(DEMO")
    assert "Best value: Shanghai" in text


def test_recommendation_demo_fallback_no_priceable(monkeypatch):
    """Demo fallback with no priceable options is still labeled demo."""
    monkeypatch.setattr(appmod, "DEMO_MODE", True)

    def boom(*a, **k):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(appmod, "ollama_chat", boom)
    results = [{"city": "Shanghai", "iata": "PVG", "best": None}]
    text = appmod.build_recommendation("YYZ", results, 2, [5], 1)
    assert text.startswith("(DEMO")
    assert "No priceable options" in text


# --------------------------- end-to-end api_search in demo mode ---------------------------

def test_api_search_demo_mode(client, monkeypatch, no_real_providers):
    monkeypatch.setattr(appmod, "DEMO_MODE", True)
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "(DEMO) ok")
    resp = client.post("/api/search", json={
        "origin": "YYZ",
        "destinations": [{"city": "Shanghai", "iata": "PVG"}],
        "adults": 2, "child_ages": [5],
        "dep_dates": ["2026-07-01"], "ret_dates": ["2026-07-15"],
    })
    data = resp.get_json()
    assert data["providers"] == ["demo"]
    assert data["results"][0]["grid"][0][0]["source"] == "demo"


# --------------------------- demo never persisted into the watch DB ---------------------------

def test_add_watch_in_demo_mode_stores_no_demo_baseline(client, monkeypatch):
    """A demo fare must NEVER be persisted as a watch baseline (#44).

    Demo data is clearly-labeled SAMPLE data; persisting it into WATCH_DB would
    leak demo data out of the demo path and drive bogus scheduler drop alerts.
    In demo mode /api/watch must skip the fare lookup entirely and create the
    watch with NO baseline (last_price/last_source unset).
    """
    monkeypatch.setattr(appmod, "DEMO_MODE", True)
    # get_fare must NOT be called for the baseline in demo mode.
    monkeypatch.setattr(appmod, "get_fare", _boom)

    db = watchmod.WatchDB(":memory:")
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(appmod, "_watch_db", lambda: db)
    try:
        resp = client.post("/api/watch", json={
            "origin": "YYZ", "dest_iata": "PVG", "dest_city": "Shanghai",
            "dep_date": "2026-12-12", "ret_date": "2027-01-04",
            "adults": 2, "child_ages": [11, 9], "threshold_pct": 25.0,
            # A tampered client baseline must also be ignored.
            "last_price": 1, "last_source": "client-spoofed",
        })
        assert resp.status_code == 200
        wid = resp.get_json()["id"]
        row = next(w for w in db.list_watches() if w["id"] == wid)
        # No baseline persisted, and certainly no demo price/source.
        assert row.get("last_price") is None
        assert row.get("last_source") in (None, "")
    finally:
        db._conn.close()


# --------------------------- scheduler refuses to run in demo mode ---------------------------

def test_scheduler_refuses_in_demo_mode(monkeypatch, capsys):
    """The price-watch scheduler must NOT run in demo mode (#44).

    scheduler.main persists each re-priced fare into WATCH_DB; in demo mode
    get_fare returns sample fares, so running it would leak demo prices into the
    real watch DB and emit bogus drop alerts. It must refuse without touching the
    DB or get_fare.
    """
    monkeypatch.setattr(appmod, "DEMO_MODE", True)
    # If the scheduler tried to price anything, these would fire.
    monkeypatch.setattr(appmod, "get_fare", _boom)

    def no_db(*a, **k):
        raise AssertionError("scheduler opened the watch DB in demo mode")

    monkeypatch.setattr(schedmod, "WatchDB", no_db)
    rc = schedmod.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DEMO_MODE" in out and "refusing" in out.lower()


def test_scheduler_runs_when_demo_off(monkeypatch, tmp_path, capsys):
    """With demo off, the scheduler runs normally (real path unchanged)."""
    monkeypatch.setattr(appmod, "DEMO_MODE", False)
    monkeypatch.setenv("WATCH_DB", str(tmp_path / "w.db"))
    monkeypatch.delenv("WATCH_WEBHOOK_URL", raising=False)
    rc = schedmod.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[SUMMARY]" in out
