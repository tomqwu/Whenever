"""E2E tests for #16 — configurable country-seed list.

These tests run the Flask app in a real thread. The ``live_server`` fixture
stubs out ``top_cities``, so for seed-path tests we use a separate fixture
(``seed_live_server``) that restores the real function and injects
``_SEED_CONFIG`` directly — no file I/O, no LLM calls needed.
"""
import threading
import pytest
import requests as req
from werkzeug.serving import make_server
import app as appmod

# ---------------------------------------------------------------------------
# Minimal China seed config identical to the real YAML content
# ---------------------------------------------------------------------------
CHINA_SEED = {
    "china": {
        "display_name": "China",
        "candidates": [
            {"city": "Beijing",  "iata": "PEK", "alt_iata": ["PKX"], "priority": 1},
            {"city": "Shanghai", "iata": "PVG", "alt_iata": ["SHA"], "priority": 2},
            {"city": "Guangzhou","iata": "CAN", "priority": 3},
            {"city": "Shenzhen", "iata": "SZX", "priority": 4},
            {"city": "Chengdu",  "iata": "TFU", "alt_iata": ["CTU"], "priority": 5},
            {"city": "Xiamen",   "iata": "XMN", "priority": 6},
            {"city": "Haikou",   "iata": "HAK", "priority": 7, "optional": True},
            {"city": "Sanya",    "iata": "SYX", "priority": 7, "optional": True},
            {"city": "Shenyang", "iata": "SHE", "priority": 8, "optional": True},
            {"city": "Hong Kong","iata": "HKG", "priority": 9, "optional": True,
             "notes": "Nearby hub (Hong Kong SAR); separate entry/visa from mainland"},
            {"city": "Taipei",   "iata": "TPE", "priority": 10, "optional": True,
             "notes": "Taiwan; nearby alternative, separate entry"},
            {"city": "Tokyo",    "iata": "HND", "alt_iata": ["NRT"], "priority": 11, "optional": True,
             "notes": "Japan; nearby hub, separate country"},
        ],
    }
}


@pytest.fixture
def seed_live_server(monkeypatch):
    """A live Flask server with provider env vars cleared and _SEED_CONFIG injected.

    Unlike the default live_server, top_cities is NOT replaced — the real
    function runs so seed lookup is exercised.  The LLM (ollama_chat) is
    patched to raise, ensuring any successful response comes from the seed.
    """
    monkeypatch.setattr(appmod, "KIWI_API_KEY", None)
    monkeypatch.setattr(appmod, "AMADEUS_ID", None)
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", None)
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", None)
    monkeypatch.setattr(appmod, "ollama_ok", lambda: True)
    monkeypatch.setattr(appmod, "_SEED_CONFIG", CHINA_SEED)
    monkeypatch.setattr(
        appmod, "ollama_chat",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError(
            "ollama_chat must not be called when a seed is present"
        ))
    )
    appmod.top_cities.cache_clear()

    srv = make_server("127.0.0.1", 0, appmod.app)
    thread = threading.Thread(target=srv.serve_forever)
    thread.daemon = True
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        thread.join()
        appmod.top_cities.cache_clear()


# ---------------------------------------------------------------------------
# API-level assertions: seed cities returned without LLM call
# ---------------------------------------------------------------------------

def test_top_cities_api_returns_seed_cities_without_llm(seed_live_server):
    """POST /api/top-cities for China returns seed cities without calling the LLM."""
    resp = req.post(
        f"{seed_live_server}/api/top-cities",
        json={"country": "China", "n": 6},
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    cities = resp.json()["cities"]

    city_names = [c["city"] for c in cities]
    assert "Beijing" in city_names, f"Beijing missing from {city_names}"
    assert "Shanghai" in city_names, f"Shanghai missing from {city_names}"

    optional_cities = [c for c in cities if c.get("optional") is True]
    assert len(optional_cities) >= 1, "At least one optional city must be returned"

    haikou = next((c for c in cities if c["iata"] == "HAK"), None)
    assert haikou is not None, "Haikou (HAK) must be in the response"
    assert haikou["optional"] is True, "Haikou must be optional=true"


def test_top_cities_api_optional_and_priority_in_response(seed_live_server):
    """Every city in the /api/top-cities response has optional and priority fields."""
    resp = req.post(
        f"{seed_live_server}/api/top-cities",
        json={"country": "China", "n": 6},
    )
    assert resp.status_code == 200
    cities = resp.json()["cities"]

    for city in cities:
        assert "optional" in city, f"'optional' missing in {city}"
        assert "priority" in city, f"'priority' missing in {city}"

    # Exactly 6 required + 6 optional
    required = [c for c in cities if not c["optional"]]
    optional = [c for c in cities if c["optional"]]
    assert len(required) == 6
    assert len(optional) == 6


# ---------------------------------------------------------------------------
# Browser assertion: a country expansion starts with EVERY chip UNCHECKED
# ---------------------------------------------------------------------------

def test_expanded_country_chips_all_unchecked(seed_live_server, page):
    """After expanding a COUNTRY, no chip is .on — required AND optional alike.

    Validates the templates/index.html change: expandCountry maps every city to
    on:false (opt-in UX), regardless of the `optional` flag. The user explicitly
    clicks a chip to select it.
    """
    page.goto(seed_live_server)
    page.click("#loadCities")
    # Wait until at least one chip appears
    page.wait_for_selector(".chip")

    # No chip may be pre-selected after a country expansion.
    assert page.query_selector(".chip.on") is None, \
        "No chip should have class 'on' after a country expansion (all unchecked)"

    # Required city Beijing must still be RENDERED — just unchecked.
    beijing_chip = page.query_selector(".chip:has-text('Beijing')")
    assert beijing_chip is not None, "Beijing chip must be rendered (unchecked)"
    assert "on" not in (beijing_chip.get_attribute("class") or ""), \
        "Beijing (required) chip must NOT be pre-selected anymore"

    # Optional city Haikou must also be rendered, unchecked.
    haikou_chip = page.query_selector(".chip:has-text('Haikou')")
    assert haikou_chip is not None, "Haikou chip must be rendered (unchecked)"
    assert "on" not in (haikou_chip.get_attribute("class") or ""), \
        "Haikou (optional) chip must NOT have class 'on'"

    # Hong Kong (nearby hub) must also be rendered UNCHECKED (#46).
    hkg_chip = page.query_selector(".chip:has-text('Hong Kong')")
    assert hkg_chip is not None, "Hong Kong chip must be rendered (unchecked)"
    assert "on" not in (hkg_chip.get_attribute("class") or ""), \
        "Hong Kong (nearby optional) chip must NOT have class 'on'"

    # Clicking a chip selects it (toggle UX unchanged). drawChips() rebuilds the
    # chip DOM on every toggle, so re-query for the fresh element afterward.
    beijing_chip.click()
    page.wait_for_selector(".chip.on:has-text('Beijing')")
    assert page.query_selector(".chip.on:has-text('Beijing')") is not None, \
        "Clicking the Beijing chip must turn it .on"


def test_run_with_all_unchecked_expansion_alerts(seed_live_server, page):
    """After a country expansion (all chips unchecked) the user clicking Run with
    nothing selected must still hit the existing 'pick at least one destination'
    alert — never a broken/empty search."""
    page.goto(seed_live_server)
    page.click("#loadCities")
    page.wait_for_selector(".chip")
    # Nothing selected after expansion.
    assert page.query_selector(".chip.on") is None

    # Capture the alert and assert no search request goes out.
    dialog_messages = []
    page.on("dialog", lambda d: (dialog_messages.append(d.message), d.dismiss()))
    search_hits = []
    page.on("request", lambda req: search_hits.append(req.url)
            if "/api/search" in req.url else None)

    page.click("#run")
    page.wait_for_timeout(400)

    assert len(dialog_messages) == 1, f"Expected the alert, got {dialog_messages!r}"
    assert "at least one" in dialog_messages[0].lower(), \
        f"Expected 'pick at least one' alert, got: {dialog_messages[0]!r}"
    assert search_hits == [], f"No search should run with nothing selected, got {search_hits!r}"


# ---------------------------------------------------------------------------
# #46 — API-level: HKG, TPE, HND present as optional in China response
# ---------------------------------------------------------------------------

def test_nearby_hubs_in_api_response(seed_live_server):
    """HKG, TPE, HND must appear as optional=true in /api/top-cities China response."""
    resp = req.post(
        f"{seed_live_server}/api/top-cities",
        json={"country": "China", "n": 6},
    )
    assert resp.status_code == 200
    cities = resp.json()["cities"]
    iata_map = {c["iata"]: c for c in cities}

    for iata, label in [("HKG", "Hong Kong"), ("TPE", "Taipei"), ("HND", "Tokyo")]:
        assert iata in iata_map, f"{label} ({iata}) must be in /api/top-cities response"
        assert iata_map[iata]["optional"] is True, f"{label} ({iata}) must be optional=true"


def test_nearby_hubs_not_in_required_set(seed_live_server):
    """HKG/TPE/HND must NOT be in the required (optional=false) set."""
    resp = req.post(
        f"{seed_live_server}/api/top-cities",
        json={"country": "China", "n": 6},
    )
    assert resp.status_code == 200
    cities = resp.json()["cities"]
    required_iatas = {c["iata"] for c in cities if not c["optional"]}

    assert "HKG" not in required_iatas, "HKG must not be a required city"
    assert "TPE" not in required_iatas, "TPE must not be a required city"
    assert "HND" not in required_iatas, "HND must not be a required city"
    assert len(required_iatas) == 6, f"Required set must still be 6, got {len(required_iatas)}"
