import app as appmod


def test_app_imports():
    assert appmod.app is not None


def test_health_endpoint_smoke(client, monkeypatch):
    monkeypatch.setattr(appmod, "ollama_ok", lambda: False)
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.get_json()["model"] == appmod.OLLAMA_MODEL
