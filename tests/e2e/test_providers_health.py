"""E2E tests: provider health endpoint reflects Kiwi/SerpApi when keys are set."""
import threading
import pytest
from werkzeug.serving import make_server
import app as appmod


@pytest.fixture
def live_server_kiwi(monkeypatch):
    """Live server with KIWI_API_KEY set so providers_configured() includes 'kiwi'."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "test-kiwi-key")
    monkeypatch.setattr(appmod, "AMADEUS_ID", None)
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", None)
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", None)
    monkeypatch.setattr(appmod, "ollama_ok", lambda: False)

    srv = make_server("127.0.0.1", 0, appmod.app)
    thread = threading.Thread(target=srv.serve_forever)
    thread.daemon = True
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        thread.join()


def test_health_includes_kiwi_when_key_set(live_server_kiwi, page):
    """GET /api/health returns 'kiwi' in providers when KIWI_API_KEY is configured."""
    resp = page.request.get(f"{live_server_kiwi}/api/health")
    assert resp.status == 200
    data = resp.json()
    assert "kiwi" in data["providers"]


def test_health_excludes_kiwi_when_key_unset(live_server, page):
    """GET /api/health does NOT include 'kiwi' when KIWI_API_KEY is not configured."""
    resp = page.request.get(f"{live_server}/api/health")
    assert resp.status == 200
    data = resp.json()
    assert "kiwi" not in data["providers"]


@pytest.fixture
def live_server_serpapi(monkeypatch):
    """Live server with SERPAPI_KEY set so providers_configured() includes 'serpapi'."""
    monkeypatch.setattr(appmod, "SERPAPI_KEY", "test-serpapi-key")
    monkeypatch.setattr(appmod, "KIWI_API_KEY", None)
    monkeypatch.setattr(appmod, "AMADEUS_ID", None)
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", None)
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", None)
    monkeypatch.setattr(appmod, "ollama_ok", lambda: False)

    srv = make_server("127.0.0.1", 0, appmod.app)
    thread = threading.Thread(target=srv.serve_forever)
    thread.daemon = True
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        thread.join()


def test_health_includes_serpapi_when_key_set(live_server_serpapi, page):
    """GET /api/health returns 'serpapi' in providers when SERPAPI_KEY is configured."""
    resp = page.request.get(f"{live_server_serpapi}/api/health")
    assert resp.status == 200
    data = resp.json()
    assert "serpapi" in data["providers"]


def test_health_excludes_serpapi_when_key_unset(live_server, page):
    """GET /api/health does NOT include 'serpapi' when SERPAPI_KEY is not configured."""
    resp = page.request.get(f"{live_server}/api/health")
    assert resp.status == 200
    data = resp.json()
    assert "serpapi" not in data["providers"]


@pytest.fixture
def live_server_skyscanner(monkeypatch):
    """Live server with RAPIDAPI_KEY set so providers_configured() includes 'skyscanner'."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "test-rapidapi-key")
    monkeypatch.setattr(appmod, "SERPAPI_KEY", None)
    monkeypatch.setattr(appmod, "KIWI_API_KEY", None)
    monkeypatch.setattr(appmod, "AMADEUS_ID", None)
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", None)
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", None)
    monkeypatch.setattr(appmod, "ollama_ok", lambda: False)

    srv = make_server("127.0.0.1", 0, appmod.app)
    thread = threading.Thread(target=srv.serve_forever)
    thread.daemon = True
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        thread.join()


def test_health_includes_skyscanner_when_key_set(live_server_skyscanner, page):
    """GET /api/health returns 'skyscanner' in providers when RAPIDAPI_KEY is configured."""
    resp = page.request.get(f"{live_server_skyscanner}/api/health")
    assert resp.status == 200
    data = resp.json()
    assert "skyscanner" in data["providers"]


def test_health_excludes_skyscanner_when_key_unset(live_server, page):
    """GET /api/health does NOT include 'skyscanner' when RAPIDAPI_KEY is not configured."""
    resp = page.request.get(f"{live_server}/api/health")
    assert resp.status == 200
    data = resp.json()
    assert "skyscanner" not in data["providers"]
