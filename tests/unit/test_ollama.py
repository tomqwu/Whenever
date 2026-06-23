import app as appmod


def test_ollama_chat_strips_think_and_uses_system(monkeypatch, fake_resp):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["json"] = json
        return fake_resp({"message": {"content": "<think>reason</think>  HELLO "}})

    monkeypatch.setattr(appmod.requests, "post", fake_post)
    out = appmod.ollama_chat("hi", system="be terse")
    assert out == "HELLO"
    assert captured["json"]["messages"][0]["role"] == "system"


def test_ollama_chat_without_system(monkeypatch, fake_resp):
    def fake_post(url, json=None, timeout=None):
        return fake_resp({"message": {"content": "  world  "}})

    monkeypatch.setattr(appmod.requests, "post", fake_post)
    out = appmod.ollama_chat("hi")
    assert out == "world"


def test_ollama_ok_true(monkeypatch, fake_resp):
    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: fake_resp({}))
    assert appmod.ollama_ok() is True


def test_ollama_ok_false(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(appmod.requests, "get", boom)
    assert appmod.ollama_ok() is False


def test_ollama_ok_false_on_bad_status(monkeypatch, fake_resp):
    monkeypatch.setattr(
        appmod.requests,
        "get",
        lambda *a, **k: fake_resp({}, status=500, raise_exc=RuntimeError("bad status")),
    )
    assert appmod.ollama_ok() is False


def test_top_cities_filters_invalid_items(monkeypatch):
    payload = [
        {"city": "Beijing", "iata": "pek"},
        {"city": "NoCode"},          # dropped: no iata
        "garbage",                    # dropped: not a dict
        {"city": "Shanghai", "iata": "PVGXX"},
    ]
    monkeypatch.setattr(appmod, "ollama_chat", lambda *a, **k: __import__("json").dumps(payload))
    appmod.top_cities.cache_clear()
    out = appmod.top_cities("China", 6)
    assert out == [
        {"city": "Beijing", "iata": "PEK"},
        {"city": "Shanghai", "iata": "PVG"},
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
