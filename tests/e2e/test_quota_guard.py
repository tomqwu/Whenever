"""E2E tests for the quota guard feature (issue #37).

Frontend: a confirm() dialog is shown when cells >= CONFIRM_CELLS.
Backend: MAX_SEARCH_CELLS hard cap returns 400 when exceeded.
"""
import threading
import pytest
from werkzeug.serving import make_server
import app as appmod

from tests.e2e.conftest import select_all_chips


# ---------------------------------------------------------------------------
# Fixtures: a server with many cities so cells exceed CONFIRM_CELLS (40)
# ---------------------------------------------------------------------------

def _start_server():
    srv = make_server("127.0.0.1", 0, appmod.app)
    thread = threading.Thread(target=srv.serve_forever)
    thread.daemon = True
    thread.start()
    return srv, thread


@pytest.fixture
def quota_live_server(monkeypatch):
    """Server with 5 cities × dep_span=3 × ret_span=3 = 45 cells > CONFIRM_CELLS(40).

    get_fare is mocked so no real API calls are made.
    top_cities returns 5 cities so the user can click run without being prompted
    for destinations.
    """
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
    # 5 cities (so cells = 5 × dep_span × ret_span)
    monkeypatch.setattr(appmod, "top_cities", lambda country, n=6: [
        {"city": "Shanghai", "iata": "PVG"},
        {"city": "Beijing", "iata": "PEK"},
        {"city": "Guangzhou", "iata": "CAN"},
        {"city": "Chengdu", "iata": "CTU"},
        {"city": "Shenzhen", "iata": "SZX"},
    ])

    srv, thread = _start_server()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        thread.join()


@pytest.fixture
def small_quota_live_server(monkeypatch):
    """Server with 1 city × dep_span=2 × ret_span=2 = 4 cells < CONFIRM_CELLS(40).

    No confirm() dialog should appear.
    """
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
    monkeypatch.setattr(appmod, "top_cities", lambda country, n=6: [
        {"city": "Shanghai", "iata": "PVG"},
    ])

    srv, thread = _start_server()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        thread.join()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_large_search_triggers_confirm_and_dismiss_aborts(quota_live_server, page):
    """Clicking run on a search > CONFIRM_CELLS shows a confirm dialog.
    Dismissing it (cancel) must abort the search — no network request is made.
    """
    page.goto(quota_live_server)
    # Load the 5 cities
    page.click("#loadCities")
    select_all_chips(page)

    # Set dep_span=3 and ret_span=3 so cells = 5×3×3 = 45 > CONFIRM_CELLS(40)
    page.fill("#depSpan", "3")
    page.fill("#retSpan", "3")

    # Track search requests
    search_hits = []
    page.on("request", lambda req: search_hits.append(req.url)
            if "/api/search/stream" in req.url else None)

    # Set up dialog handler to DISMISS (cancel)
    dialog_messages = []

    def handle_dismiss(dialog):
        dialog_messages.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", handle_dismiss)
    page.click("#run")

    # Wait briefly — no search should run
    page.wait_for_timeout(700)

    # A dialog was shown
    assert len(dialog_messages) == 1, f"Expected 1 dialog, got {len(dialog_messages)}"
    # Dialog message must mention cell count
    assert "45" in dialog_messages[0] or "search" in dialog_messages[0].lower(), (
        f"Dialog message should mention cell count or 'search': {dialog_messages[0]}"
    )
    # No search request was fired
    assert search_hits == [], f"Expected no search requests after dismiss, got: {search_hits}"
    # Grid stayed empty
    assert page.query_selector("#summary .card") is None


def test_large_search_triggers_confirm_and_accept_proceeds(quota_live_server, page):
    """Clicking run on a search > CONFIRM_CELLS shows a confirm dialog.
    Accepting it must proceed with the search and populate the grid.
    """
    page.goto(quota_live_server)
    # Load the 5 cities
    page.click("#loadCities")
    select_all_chips(page)

    # Set dep_span=3 and ret_span=3 so cells = 5×3×3 = 45 > CONFIRM_CELLS(40)
    page.fill("#depSpan", "3")
    page.fill("#retSpan", "3")

    # Set up dialog handler to ACCEPT
    def handle_accept(dialog):
        dialog.accept()

    page.on("dialog", handle_accept)
    page.click("#run")

    # Search should proceed and populate the grid
    page.wait_for_selector("#summary .card", timeout=10000)
    summary_text = page.inner_text("#summary")
    assert "Shanghai" in summary_text or "Beijing" in summary_text


def test_small_search_no_confirm_needed(small_quota_live_server, page):
    """Clicking run on a search <= CONFIRM_CELLS must NOT show a confirm dialog
    and must proceed directly to populate the grid.
    """
    page.goto(small_quota_live_server)
    # Load the 1 city
    page.click("#loadCities")
    select_all_chips(page)

    # dep_span=2, ret_span=2 → cells = 1×2×2 = 4 < CONFIRM_CELLS(40)
    page.fill("#depSpan", "2")
    page.fill("#retSpan", "2")

    # If a dialog appeared unexpectedly, the test would hang/fail
    dialog_fired = []
    page.on("dialog", lambda d: (dialog_fired.append(True), d.accept()))

    page.click("#run")
    page.wait_for_selector("#summary .card", timeout=10000)

    # No dialog should have appeared
    assert dialog_fired == [], "No confirm dialog expected for small search"
    summary_text = page.inner_text("#summary")
    assert "Shanghai" in summary_text
