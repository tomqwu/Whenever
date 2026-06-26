"""E2E tests for opt-in, clearly-labeled demo/sample mode (#44).

With DEMO_MODE on (set before the live server boots, like the real env flag), the
page must show the prominent persistent "DEMO DATA … NOT real prices" banner, the
grid must render clearly-labeled sample prices, and the recommendation must be
labeled demo. With DEMO off (default), the banner must be absent.
"""
import threading

import pytest
from werkzeug.serving import make_server

import app as appmod


def _boom(*a, **k):
    raise AssertionError("real provider called in demo mode")


@pytest.fixture
def demo_live_server(monkeypatch):
    """Live server with DEMO_MODE on and every real provider booby-trapped.

    DEMO_MODE is patched BEFORE the server thread starts, mirroring the env flag
    being set before boot. get_fare/build_recommendation are NOT stubbed — the
    real demo path runs, proving the end-to-end demo wiring.
    """
    monkeypatch.setattr(appmod, "DEMO_MODE", True)
    for name in ("skyscanner_fare", "serpapi_fare", "amadeus_fare",
                 "travelpayouts_fare", "kiwi_fare"):
        monkeypatch.setattr(appmod, name, _boom)
    monkeypatch.setattr(appmod, "ollama_ok", lambda: True)
    # Avoid a real LLM call: the demo prefix is applied around ollama_chat.
    monkeypatch.setattr(appmod, "ollama_chat",
                        lambda *a, **k: "Best value: Shanghai (PVG)")
    monkeypatch.setattr(appmod, "top_cities",
                        lambda country, n=6: [{"city": "Shanghai", "iata": "PVG"}])

    srv = make_server("127.0.0.1", 0, appmod.app)
    thread = threading.Thread(target=srv.serve_forever)
    thread.daemon = True
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        thread.join()


def _run_search(page):
    page.click("#loadCities")
    page.wait_for_selector(".chip:not(.hint)")
    chip = page.query_selector(".chip:not(.hint):not(.on)")
    chip.click()
    page.wait_for_selector(".chip.on")
    page.click("#run")


def test_demo_banner_visible_and_cells_labeled(demo_live_server, page):
    page.goto(demo_live_server)

    # Prominent persistent banner visible on load (driven by /api/health).
    page.wait_for_selector("#demoBanner", state="visible", timeout=10000)
    banner_text = page.inner_text("#demoBanner")
    assert "DEMO DATA" in banner_text
    assert "NOT real prices" in banner_text

    _run_search(page)

    # Stream completes -> recommendation visible and labeled demo.
    page.wait_for_selector("#rec", state="visible", timeout=15000)
    rec_text = page.inner_text("#rec")
    assert "DEMO" in rec_text, f"recommendation not labeled demo: {rec_text!r}"

    # A grid cell renders a sample price with the inline "demo" tag.
    page.wait_for_function(
        "() => document.querySelector('td a.price') !== null", timeout=15000)
    assert page.query_selector("td .demo-tag") is not None, \
        "expected a per-cell demo tag"
    price_text = page.query_selector("td a.price").inner_text()
    assert "$" in price_text

    # The no-provider banner (#39) must be hidden in demo mode.
    assert not page.is_visible("#noProviderBanner")

    # The footer must NOT claim "Live flight data"/"real fares" in demo mode — it
    # must say sample/demo (#44), agreeing with the banner.
    foot_text = page.inner_text("#foot")
    assert "Live flight data" not in foot_text
    assert "real fares" not in foot_text
    assert "Sample fares" in foot_text or "DEMO" in foot_text


def test_demo_banner_absent_by_default(live_server, page):
    """DEMO off (default): the demo banner is never shown."""
    page.goto(live_server)
    # Give health() time to run.
    page.wait_for_function(
        "() => document.querySelector('#status').textContent !== 'Checking local model…'",
        timeout=10000,
    )
    assert not page.is_visible("#demoBanner")
