"""Unit tests for #55 — destination autocomplete (/api/suggest + dataset)."""
import json
import app as appmod


# --------------------------- dataset loading ---------------------------

def test_airports_dataset_loads():
    """The bundled dataset loads into a non-empty list of well-formed entries."""
    airports = appmod._load_airports()
    assert isinstance(airports, list)
    assert len(airports) > 100
    sample = airports[0]
    for key in ("iata", "city", "country", "country_code"):
        assert key in sample


def test_dataset_includes_all_seed_iatas():
    """Every IATA used by the China seed (incl. alt_iata) is in the dataset."""
    iatas = {a["iata"] for a in appmod._load_airports()}
    seed = {"PEK", "PKX", "PVG", "SHA", "CAN", "SZX", "TFU", "CTU",
            "XMN", "HAK", "SYX", "SHE", "HKG", "TPE", "HND", "NRT"}
    assert seed <= iatas, f"missing seed IATAs: {seed - iatas}"


def test_load_airports_missing_file(monkeypatch, tmp_path):
    """A missing airports.json yields an empty list (graceful degrade)."""
    monkeypatch.setattr(appmod.os.path, "dirname", lambda _: str(tmp_path))
    assert appmod._load_airports() == []


def test_load_airports_non_list(monkeypatch, tmp_path):
    """A JSON object (not a list) yields an empty list."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "airports.json").write_text('{"not": "a list"}')
    monkeypatch.setattr(appmod.os.path, "dirname", lambda _: str(tmp_path))
    assert appmod._load_airports() == []


def test_load_airports_malformed_json(monkeypatch, tmp_path):
    """Malformed JSON yields an empty list rather than raising."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "airports.json").write_text("{not json")
    monkeypatch.setattr(appmod.os.path, "dirname", lambda _: str(tmp_path))
    assert appmod._load_airports() == []


def test_load_airports_skips_incomplete_entries(monkeypatch, tmp_path):
    """Entries missing iata/city/country (or non-dicts) are dropped."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "airports.json").write_text(json.dumps([
        {"iata": "AAA", "city": "Alpha", "country": "Wonderland", "country_code": "WL"},
        {"iata": "", "city": "NoCode", "country": "X"},      # missing iata
        {"city": "NoIata", "country": "Y"},                   # missing iata key
        "not-a-dict",
    ]))
    monkeypatch.setattr(appmod.os.path, "dirname", lambda _: str(tmp_path))
    out = appmod._load_airports()
    assert len(out) == 1 and out[0]["iata"] == "AAA"


# --------------------------- suggest_destinations ---------------------------

def test_suggest_country_and_cities_for_chi():
    """q='chi' surfaces China (country) and matching cities."""
    out = appmod.suggest_destinations("chi")
    countries = [s for s in out if s["type"] == "country"]
    assert any(c["name"] == "China" for c in countries)
    # at least one city should match (e.g. Chengdu, or a city in a *chi* country)
    assert any(s["type"] == "city" for s in out)


def test_suggest_by_iata():
    """q='hnd' (IATA) returns Tokyo as an exact-IATA city match."""
    out = appmod.suggest_destinations("hnd")
    tokyo = [s for s in out if s["type"] == "city" and s["iata"] == "HND"]
    assert tokyo, f"HND not in {out}"
    assert tokyo[0]["city"] == "Tokyo"


def test_suggest_by_city_name():
    """q='tok' returns Tokyo."""
    out = appmod.suggest_destinations("tok")
    assert any(s["type"] == "city" and s["city"] == "Tokyo" for s in out)


def test_suggest_ranks_country_and_prefix_first():
    """For q='china', the China country suggestion ranks ahead of substring city hits."""
    out = appmod.suggest_destinations("china")
    assert out[0]["type"] == "country" and out[0]["name"] == "China"


def test_suggest_exact_iata_ranked_high():
    """An exact IATA match ranks above generic substring matches."""
    out = appmod.suggest_destinations("hnd")
    # The exact-IATA Tokyo entry should be at (or near) the very top.
    assert out[0]["type"] == "city" and out[0]["iata"] == "HND"


def test_suggest_caps_at_ten():
    """A broad query is capped at 10 results."""
    out = appmod.suggest_destinations("a")
    assert len(out) <= 10


def test_suggest_empty_and_blank_query():
    """Empty or whitespace-only queries return no suggestions."""
    assert appmod.suggest_destinations("") == []
    assert appmod.suggest_destinations("   ") == []
    assert appmod.suggest_destinations(None) == []


def test_suggest_no_match_returns_empty():
    """A query matching nothing returns an empty list."""
    assert appmod.suggest_destinations("zzzzzznope") == []


def test_build_suggest_index_folds_seed_country(monkeypatch):
    """A seed country absent from the dataset is still suggested (for expansion)."""
    airports = [{"iata": "AAA", "city": "Alpha", "country": "Wonderland",
                 "country_code": "WL"}]
    seed = {"narnia": {"display_name": "Narnia"}}
    countries, cities = appmod._build_suggest_index(airports, seed)
    names = {c["name"] for c in countries}
    assert "Wonderland" in names and "Narnia" in names
    assert len(cities) == 1


def test_build_suggest_index_seed_without_display_name():
    """A seed entry lacking display_name falls back to a title-cased key."""
    countries, _ = appmod._build_suggest_index([], {"france": {}})
    assert any(c["name"] == "France" for c in countries)


# --------------------------- route ---------------------------

def test_suggest_route_returns_matches(client):
    """GET /api/suggest?q=chi returns a suggestions list with China."""
    r = client.get("/api/suggest?q=chi")
    assert r.status_code == 200
    sug = r.get_json()["suggestions"]
    assert any(s["type"] == "country" and s["name"] == "China" for s in sug)


def test_suggest_route_iata(client):
    """GET /api/suggest?q=hnd returns Tokyo."""
    sug = client.get("/api/suggest?q=hnd").get_json()["suggestions"]
    assert any(s.get("iata") == "HND" for s in sug)


def test_suggest_route_empty_query(client):
    """GET /api/suggest with no q returns an empty list."""
    assert client.get("/api/suggest").get_json()["suggestions"] == []
    assert client.get("/api/suggest?q=").get_json()["suggestions"] == []


def test_suggest_route_caps(client):
    """The route caps at <=10 results."""
    sug = client.get("/api/suggest?q=a").get_json()["suggestions"]
    assert len(sug) <= 10
