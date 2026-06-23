"""Unit tests for #16 — configurable country-seed list.

These tests monkeypatch ``appmod._SEED_CONFIG`` directly; no file I/O occurs.
The ``_reset_state`` autouse fixture from conftest.py already calls
``top_cities.cache_clear()`` before and after each test, but we clear again
locally wherever we mutate ``_SEED_CONFIG`` mid-test to be explicit.
"""
import app as appmod
import pytest
from unittest.mock import mock_open, patch

# ---------------------------------------------------------------------------
# Minimal China seed config that matches the real YAML schema
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
        ],
    }
}


@pytest.fixture(autouse=True)
def _clear_cache():
    appmod.top_cities.cache_clear()
    yield
    appmod.top_cities.cache_clear()


# ---------------------------------------------------------------------------
# Seed present — LLM must NOT be called
# ---------------------------------------------------------------------------

def test_seed_hit_skips_llm(monkeypatch):
    """When a seed exists, ollama_chat must never be called."""
    monkeypatch.setattr(appmod, "_SEED_CONFIG", CHINA_SEED)
    llm_called = []
    monkeypatch.setattr(appmod, "ollama_chat", lambda *a, **k: llm_called.append(1) or "[]")
    appmod.top_cities.cache_clear()

    result = appmod.top_cities("China", 6)

    assert llm_called == [], "ollama_chat must not be called when a seed entry exists"
    assert len(result) > 0


def test_seed_hit_returns_6_required_plus_all_optional(monkeypatch):
    """top_cities('China', 6) -> 6 required (priority 1–6) + 3 optional."""
    monkeypatch.setattr(appmod, "_SEED_CONFIG", CHINA_SEED)
    appmod.top_cities.cache_clear()

    result = appmod.top_cities("China", 6)

    required = [c for c in result if not c["optional"]]
    optional = [c for c in result if c["optional"]]

    assert len(required) == 6, f"Expected 6 required cities, got {len(required)}"
    assert len(optional) == 3, f"Expected 3 optional cities, got {len(optional)}"
    assert len(result) == 9


def test_seed_hit_required_cities_correct_order(monkeypatch):
    """Required cities must be sorted by priority (1→6)."""
    monkeypatch.setattr(appmod, "_SEED_CONFIG", CHINA_SEED)
    appmod.top_cities.cache_clear()

    result = appmod.top_cities("China", 6)
    required = [c for c in result if not c["optional"]]

    iatas = [c["iata"] for c in required]
    assert iatas == ["PEK", "PVG", "CAN", "SZX", "TFU", "XMN"]


def test_seed_hit_optional_cities_flagged_correctly(monkeypatch):
    """Each entry must have an 'optional' boolean; optional cities are True."""
    monkeypatch.setattr(appmod, "_SEED_CONFIG", CHINA_SEED)
    appmod.top_cities.cache_clear()

    result = appmod.top_cities("China", 6)

    for city in result:
        assert "optional" in city, f"Missing 'optional' key in {city}"
        assert isinstance(city["optional"], bool), f"'optional' must be bool in {city}"

    optional_iatas = {c["iata"] for c in result if c["optional"]}
    assert optional_iatas == {"HAK", "SYX", "SHE"}


def test_seed_hit_required_cities_flagged_not_optional(monkeypatch):
    """Required cities must have optional=False."""
    monkeypatch.setattr(appmod, "_SEED_CONFIG", CHINA_SEED)
    appmod.top_cities.cache_clear()

    result = appmod.top_cities("China", 6)
    required = [c for c in result if not c["optional"]]

    for city in required:
        assert city["optional"] is False, f"Required city {city['city']} must have optional=False"


# ---------------------------------------------------------------------------
# n-limit caps ONLY required cities; optional are always appended
# ---------------------------------------------------------------------------

def test_n_caps_required_only_n3(monkeypatch):
    """top_cities('China', 3) -> 3 required + all 3 optional (n caps required only)."""
    monkeypatch.setattr(appmod, "_SEED_CONFIG", CHINA_SEED)
    appmod.top_cities.cache_clear()

    result = appmod.top_cities("China", 3)

    required = [c for c in result if not c["optional"]]
    optional = [c for c in result if c["optional"]]

    assert len(required) == 3, f"Expected 3 required cities, got {len(required)}"
    assert len(optional) == 3, f"Expected all 3 optional cities, got {len(optional)}"

    iatas = [c["iata"] for c in required]
    assert iatas == ["PEK", "PVG", "CAN"]


def test_n_caps_required_only_n1(monkeypatch):
    """n=1 -> only 1 required city + all 3 optional."""
    monkeypatch.setattr(appmod, "_SEED_CONFIG", CHINA_SEED)
    appmod.top_cities.cache_clear()

    result = appmod.top_cities("China", 1)

    required = [c for c in result if not c["optional"]]
    optional = [c for c in result if c["optional"]]

    assert len(required) == 1
    assert len(optional) == 3
    assert required[0]["iata"] == "PEK"


def test_seed_entries_have_priority_field(monkeypatch):
    """Each returned entry must carry a 'priority' key."""
    monkeypatch.setattr(appmod, "_SEED_CONFIG", CHINA_SEED)
    appmod.top_cities.cache_clear()

    result = appmod.top_cities("China", 6)

    for city in result:
        assert "priority" in city, f"Missing 'priority' key in {city}"
        assert isinstance(city["priority"], int)


# ---------------------------------------------------------------------------
# Seed absent — LLM fallback
# ---------------------------------------------------------------------------

def test_seed_absent_llm_called(monkeypatch):
    """When no seed entry for 'Narnia', ollama_chat IS called."""
    monkeypatch.setattr(appmod, "_SEED_CONFIG", {})
    llm_called = []

    def fake_llm(prompt, *a, **k):
        llm_called.append(1)
        return '[{"city":"Narnia City","iata":"NAR"},{"city":"Cair Paravel","iata":"CAP"}]'

    monkeypatch.setattr(appmod, "ollama_chat", fake_llm)
    appmod.top_cities.cache_clear()

    result = appmod.top_cities("Narnia", 6)

    assert len(llm_called) >= 1, "ollama_chat must be called when no seed exists"
    assert len(result) <= 6


def test_seed_absent_result_truncated_to_n(monkeypatch):
    """LLM result is truncated to n entries."""
    monkeypatch.setattr(appmod, "_SEED_CONFIG", {})

    big_list = [{"city": f"City{i}", "iata": f"C{i:02d}"} for i in range(10)]
    import json
    monkeypatch.setattr(appmod, "ollama_chat", lambda *a, **k: json.dumps(big_list))
    appmod.top_cities.cache_clear()

    result = appmod.top_cities("Narnia", 6)

    assert len(result) == 6, f"Expected 6, got {len(result)}"


def test_seed_absent_entries_have_optional_false(monkeypatch):
    """LLM-sourced cities must have optional=False."""
    monkeypatch.setattr(appmod, "_SEED_CONFIG", {})

    monkeypatch.setattr(
        appmod, "ollama_chat",
        lambda *a, **k: '[{"city":"Narnia City","iata":"NAR"}]'
    )
    appmod.top_cities.cache_clear()

    result = appmod.top_cities("Narnia", 6)

    for city in result:
        assert "optional" in city
        assert city["optional"] is False, f"LLM city must have optional=False, got {city}"


# ---------------------------------------------------------------------------
# Missing-file / empty _SEED_CONFIG -> LLM path used
# ---------------------------------------------------------------------------

def test_empty_seed_config_falls_back_to_llm(monkeypatch):
    """With _SEED_CONFIG={}, the LLM path is taken for any country."""
    monkeypatch.setattr(appmod, "_SEED_CONFIG", {})
    called = []

    monkeypatch.setattr(
        appmod, "ollama_chat",
        lambda *a, **k: called.append(1) or '[{"city":"Test","iata":"TST"}]'
    )
    appmod.top_cities.cache_clear()

    result = appmod.top_cities("China", 6)

    assert len(called) >= 1
    assert result[0]["iata"] == "TST"


# ---------------------------------------------------------------------------
# api_top_cities route: optional/priority passthrough
# ---------------------------------------------------------------------------

def test_api_top_cities_passthrough_optional_and_priority(monkeypatch):
    """The /api/top-cities route must pass optional and priority fields through."""
    monkeypatch.setattr(appmod, "_SEED_CONFIG", CHINA_SEED)
    appmod.top_cities.cache_clear()

    appmod.app.config["TESTING"] = True
    client = appmod.app.test_client()

    r = client.post("/api/top-cities", json={"country": "China", "n": 6})
    assert r.status_code == 200
    cities = r.get_json()["cities"]

    # Route must return all 9 entries (6 required + 3 optional)
    assert len(cities) == 9

    optional_cities = [c for c in cities if c.get("optional") is True]
    required_cities = [c for c in cities if c.get("optional") is False]

    assert len(optional_cities) == 3, f"Expected 3 optional in response, got {len(optional_cities)}"
    assert len(required_cities) == 6, f"Expected 6 required in response, got {len(required_cities)}"

    # Every entry must have 'priority'
    for city in cities:
        assert "priority" in city, f"Missing 'priority' in route response: {city}"


def test_api_top_cities_optional_iata_present(monkeypatch):
    """Optional city Haikou must appear in the /api/top-cities response."""
    monkeypatch.setattr(appmod, "_SEED_CONFIG", CHINA_SEED)
    appmod.top_cities.cache_clear()

    appmod.app.config["TESTING"] = True
    client = appmod.app.test_client()

    r = client.post("/api/top-cities", json={"country": "China", "n": 6})
    cities = r.get_json()["cities"]

    iatas = {c["iata"] for c in cities}
    assert "HAK" in iatas, "Haikou (HAK) must be returned as an optional city"
    assert "SYX" in iatas
    assert "SHE" in iatas


# ---------------------------------------------------------------------------
# _load_seed_config() branch coverage: missing file, bad parse, non-dict result
# ---------------------------------------------------------------------------

def test_load_seed_config_file_not_found(tmp_path):
    """_load_seed_config returns {} when the YAML file does not exist."""
    import importlib, sys
    # Call _load_seed_config with a patched __file__ so it looks in a non-existent dir
    with patch("builtins.open", side_effect=FileNotFoundError("no such file")):
        result = appmod._load_seed_config()
    assert result == {}


def test_load_seed_config_parse_exception(tmp_path):
    """_load_seed_config returns {} when yaml.safe_load raises."""
    import yaml
    with patch("builtins.open", mock_open(read_data="!!invalid: [yaml")):
        with patch("yaml.safe_load", side_effect=yaml.YAMLError("bad yaml")):
            result = appmod._load_seed_config()
    assert result == {}


def test_load_seed_config_non_dict_yaml(tmp_path):
    """_load_seed_config returns {} when YAML parses to a non-dict (e.g. a list)."""
    import yaml
    with patch("builtins.open", mock_open(read_data="- item1\n- item2\n")):
        with patch("yaml.safe_load", return_value=["item1", "item2"]):
            result = appmod._load_seed_config()
    assert result == {}
