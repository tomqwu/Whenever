import app as appmod


def test_ollama_chat_strips_think_and_uses_system(monkeypatch, fake_resp):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        captured["headers"] = headers
        return fake_resp({"message": {"content": "<think>reason</think>  HELLO "}})

    monkeypatch.setattr(appmod, "OLLAMA_API_KEY", None)
    monkeypatch.setattr(appmod.requests, "post", fake_post)
    out = appmod.ollama_chat("hi", system="be terse")
    assert out == "HELLO"
    assert captured["json"]["messages"][0]["role"] == "system"
    # No API key set — no auth header
    assert captured["headers"] == {}


def test_ollama_chat_without_system(monkeypatch, fake_resp):
    def fake_post(url, json=None, headers=None, timeout=None):
        return fake_resp({"message": {"content": "  world  "}})

    monkeypatch.setattr(appmod, "OLLAMA_API_KEY", None)
    monkeypatch.setattr(appmod.requests, "post", fake_post)
    out = appmod.ollama_chat("hi")
    assert out == "world"


def test_ollama_ok_true(monkeypatch, fake_resp):
    monkeypatch.setattr(appmod, "OLLAMA_API_KEY", None)
    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: fake_resp({}))
    assert appmod.ollama_ok() is True


def test_ollama_ok_false(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(appmod, "OLLAMA_API_KEY", None)
    monkeypatch.setattr(appmod.requests, "get", boom)
    assert appmod.ollama_ok() is False


def test_ollama_ok_false_on_bad_status(monkeypatch, fake_resp):
    monkeypatch.setattr(appmod, "OLLAMA_API_KEY", None)
    monkeypatch.setattr(
        appmod.requests,
        "get",
        lambda *a, **k: fake_resp({}, status=500, raise_exc=RuntimeError("bad status")),
    )
    assert appmod.ollama_ok() is False


# --- OLLAMA_API_KEY (cloud Bearer auth) tests ---

def test_ollama_chat_sends_bearer_when_api_key_set(monkeypatch, fake_resp):
    """When OLLAMA_API_KEY is set, requests.post receives Authorization: Bearer <key>."""
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["headers"] = headers
        return fake_resp({"message": {"content": "ok"}})

    monkeypatch.setattr(appmod, "OLLAMA_API_KEY", "test-key-abc")
    monkeypatch.setattr(appmod.requests, "post", fake_post)
    appmod.ollama_chat("hello")
    assert captured["headers"].get("Authorization") == "Bearer test-key-abc"


def test_ollama_chat_no_auth_header_when_api_key_unset(monkeypatch, fake_resp):
    """When OLLAMA_API_KEY is None, no Authorization header is sent."""
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["headers"] = headers
        return fake_resp({"message": {"content": "ok"}})

    monkeypatch.setattr(appmod, "OLLAMA_API_KEY", None)
    monkeypatch.setattr(appmod.requests, "post", fake_post)
    appmod.ollama_chat("hello")
    assert "Authorization" not in captured["headers"]


def test_ollama_ok_sends_bearer_when_api_key_set(monkeypatch, fake_resp):
    """When OLLAMA_API_KEY is set, requests.get receives Authorization: Bearer <key>."""
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["headers"] = headers
        return fake_resp({})

    monkeypatch.setattr(appmod, "OLLAMA_API_KEY", "cloud-key-xyz")
    monkeypatch.setattr(appmod.requests, "get", fake_get)
    result = appmod.ollama_ok()
    assert result is True
    assert captured["headers"].get("Authorization") == "Bearer cloud-key-xyz"


def test_ollama_ok_no_auth_header_when_api_key_unset(monkeypatch, fake_resp):
    """When OLLAMA_API_KEY is None, no Authorization header is sent to /api/tags."""
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["headers"] = headers
        return fake_resp({})

    monkeypatch.setattr(appmod, "OLLAMA_API_KEY", None)
    monkeypatch.setattr(appmod.requests, "get", fake_get)
    result = appmod.ollama_ok()
    assert result is True
    assert "Authorization" not in captured["headers"]


def test_ollama_headers_helper_with_key(monkeypatch):
    """_ollama_headers() returns the correct dict when a key is set."""
    monkeypatch.setattr(appmod, "OLLAMA_API_KEY", "mykey")
    assert appmod._ollama_headers() == {"Authorization": "Bearer mykey"}


def test_ollama_headers_helper_without_key(monkeypatch):
    """_ollama_headers() returns empty dict when no key is set."""
    monkeypatch.setattr(appmod, "OLLAMA_API_KEY", None)
    assert appmod._ollama_headers() == {}


def test_top_cities_filters_invalid_items(monkeypatch):
    # Use a country not in _SEED_CONFIG to exercise the LLM fallback path.
    monkeypatch.setattr(appmod, "_SEED_CONFIG", {})
    payload = [
        {"city": "Alpha City", "iata": "alp"},
        {"city": "NoCode"},          # dropped: no iata
        "garbage",                    # dropped: not a dict
        {"city": "Beta City", "iata": "BETXX"},
    ]
    monkeypatch.setattr(appmod, "ollama_chat", lambda *a, **k: __import__("json").dumps(payload))
    appmod.top_cities.cache_clear()
    out = appmod.top_cities("Narnia", 6)
    # LLM path now annotates each entry with optional=False
    assert out == [
        {"city": "Alpha City", "iata": "ALP", "optional": False},
        {"city": "Beta City",  "iata": "BET", "optional": False},
    ]


def test_resolve_airport_success(monkeypatch):
    monkeypatch.setattr(appmod, "ollama_chat", lambda *a, **k: '{"iata":"yyz"}')
    appmod.resolve_airport.cache_clear()
    assert appmod.resolve_airport("Toronto") == "YYZ"


def test_resolve_airport_handles_error(monkeypatch):
    def boom(*a, **k):
        raise ValueError("no json")

    monkeypatch.setattr(appmod, "ollama_chat", boom)
    appmod.resolve_airport.cache_clear()
    assert appmod.resolve_airport("Nowhere") == ""
