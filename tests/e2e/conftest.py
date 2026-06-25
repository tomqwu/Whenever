import threading
import os
import pytest
from werkzeug.serving import make_server
import app as appmod


@pytest.fixture(autouse=True)
def _watch_db_tmp(monkeypatch, tmp_path):
    """Point the watch DB at a throwaway temp file for EVERY e2e test.

    Any page load fires GET /api/watch (the "Watched trips" list), which opens
    the WATCH_DB SQLite file; without this the default whenever_watches.db would
    be created in the repo. tmp_path is auto-cleaned by pytest, so nothing leaks.
    """
    monkeypatch.setenv("WATCH_DB", str(tmp_path / "watches.db"))


def select_chips(page, limit=None):
    """Turn ON destination chips after a country expansion.

    A country expansion now starts with EVERY chip UNCHECKED (opt-in UX), so any
    e2e test that wants to run a search must explicitly select at least one chip
    first. drawChips() rebuilds the whole chip DOM on each toggle, detaching the
    other element handles, so select chips by re-querying for the next unchecked
    chip after every click.

    ``limit`` caps how many chips are turned on (None = all). The seed China
    expansion yields ~12 cities; selecting all of them ×2×2 dates exceeds
    CONFIRM_CELLS(40) and would trip the quota confirm() dialog. Tests that just
    need a normal search to run pass a small limit (default 4) via
    ``select_some_chips`` to stay under that threshold; tests that need every
    chip on (quota guard) use ``select_all_chips``.
    """
    page.wait_for_selector(".chip")
    count = 0
    while limit is None or count < limit:
        chip = page.query_selector(".chip:not(.hint):not(.on)")
        if chip is None:
            break
        chip.click()
        count += 1
    page.wait_for_selector(".chip.on")


def select_all_chips(page):
    """Turn ON every destination chip (see select_chips)."""
    select_chips(page, limit=None)


def select_some_chips(page, limit=4):
    """Turn ON up to ``limit`` chips — enough to run a search without tripping the
    quota confirm() dialog on a large seed expansion (see select_chips)."""
    select_chips(page, limit=limit)


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
        "source": "test", "book": "https://example.com/book", "duration_min": 875,
        # nonstop chosen (8500 within 25% of 8000): the cheapest line shows
        # duration_min and the summary/nonstop line uses nonstop_duration_min;
        # both are 875 here so '14h 35m' renders in cell + card alike.
        "nonstop_duration_min": 875,
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


@pytest.fixture
def markdown_live_server(monkeypatch):
    """Server where build_recommendation returns markdown (bold + newlines).
    Used to assert the UI renders <strong> elements and no literal ** remain."""
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        appmod, "build_recommendation",
        lambda *a, **k: "**Best value:** Shanghai (PVG) – CAD 4,443\nGreat choice for families.",
    )
    monkeypatch.setattr(appmod, "top_cities",
                        lambda country, n=6: [{"city": "Shanghai", "iata": "PVG"}])

    srv, thread = _start_server()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        thread.join()


@pytest.fixture
def xss_live_server(monkeypatch):
    """Server where build_recommendation returns HTML/script injection attempt.
    Used to assert the UI escapes model output and no injected elements appear."""
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        appmod, "build_recommendation",
        lambda *a, **k: "<script>alert(1)</script> <b>injected</b> Best value: Shanghai",
    )
    monkeypatch.setattr(appmod, "top_cities",
                        lambda country, n=6: [{"city": "Shanghai", "iata": "PVG"}])

    srv, thread = _start_server()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        thread.join()


@pytest.fixture
def nofare_live_server(monkeypatch):
    """Server where ONE city (Beijing/PEK) returns no fares for every cell;
    all other cities get a normal price. Used to assert the UI finalizes a
    no-fare city card to '— / no fares / —' (not the '…' placeholder)."""
    _patch_common(monkeypatch)

    def partial_fare(origin, dest, dep, ret, adults, children):
        if dest == "PEK":
            return {"cheapest_cad": None, "stops": None, "nonstop_cad": None,
                    "source": "no-data"}
        return {"cheapest_cad": 8000, "stops": 1, "nonstop_cad": 8500,
                "source": "test", "book": "https://example.com/book"}

    monkeypatch.setattr(appmod, "get_fare", partial_fare)
    # Two cities so one has fares (PVG) and one does not (PEK).
    monkeypatch.setattr(appmod, "top_cities", lambda country, n=6: [
        {"city": "Shanghai", "iata": "PVG"},
        {"city": "Beijing", "iata": "PEK"},
    ])

    srv, thread = _start_server()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        thread.join()
