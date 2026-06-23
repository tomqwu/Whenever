import threading
import pytest
from werkzeug.serving import make_server
import app as appmod


@pytest.fixture
def live_server(monkeypatch):
    monkeypatch.setattr(appmod, "ollama_ok", lambda: True)
    monkeypatch.setattr(appmod, "top_cities",
                        lambda country, n=6: [{"city": "Shanghai", "iata": "PVG"}])
    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: {
        "cheapest_cad": 8000, "stops": 1, "nonstop_cad": 8500,
        "source": "test", "book": "https://example.com/book",
    })
    monkeypatch.setattr(appmod, "build_recommendation",
                        lambda *a, **k: "Best value: Shanghai (PVG)")

    srv = make_server("127.0.0.1", 5099, appmod.app)
    thread = threading.Thread(target=srv.serve_forever)
    thread.start()
    try:
        yield "http://127.0.0.1:5099"
    finally:
        srv.shutdown()
        thread.join()
