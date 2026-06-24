import threading
import pytest
from werkzeug.serving import make_server
import app as appmod


def _patch_common(monkeypatch):
    """Patch credentials and deterministic stubs shared by both server fixtures."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", None)
    monkeypatch.setattr(appmod, "AMADEUS_ID", None)
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", None)
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", None)
    monkeypatch.setattr(appmod, "SERPAPI_KEY", None)
    monkeypatch.setattr(appmod, "ollama_ok", lambda: True)
    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: {
        "cheapest_cad": 8000, "stops": 1, "nonstop_cad": 8500,
        "source": "test", "book": "https://example.com/book",
    })
    monkeypatch.setattr(appmod, "build_recommendation",
                        lambda *a, **k: "Best value: test recommendation")


def _start_server():
    srv = make_server("127.0.0.1", 0, appmod.app)
    thread = threading.Thread(target=srv.serve_forever)
    thread.daemon = True
    thread.start()
    return srv, thread


@pytest.fixture
def live_server(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(appmod, "top_cities",
                        lambda country, n=6: [{"city": "Shanghai", "iata": "PVG"}])

    srv, thread = _start_server()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        thread.join()


@pytest.fixture
def seed_live_server(monkeypatch):
    """Like live_server but does NOT override top_cities.

    The real China seed expansion in app.py runs offline from
    config/country_seeds.yaml — fully deterministic, no LLM or network call.
    """
    _patch_common(monkeypatch)
    # top_cities is NOT patched — the real seed path runs for China

    srv, thread = _start_server()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        thread.join()
