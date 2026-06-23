import app as appmod


def test_search_missing_dates_returns_400_e2e(live_server, page):
    resp = page.request.post(
        f"{live_server}/api/search",
        data={"origin": "YYZ", "destinations": [{"city": "X", "iata": "XXX"}]},
    )
    assert resp.status == 400


def test_page_loads_and_health_renders(live_server, page):
    page.goto(live_server)
    model = appmod.OLLAMA_MODEL
    page.wait_for_function(
        "(m) => { const el = document.querySelector('#status'); return el && el.textContent.includes(m); }",
        arg=model,
    )
    assert model in page.inner_text("#status")   # model name from /api/health


def test_full_search_flow(live_server, page):
    page.goto(live_server)
    page.click("#loadCities")
    page.wait_for_selector(".chip.on")             # Shanghai chip, toggled on
    page.click("#run")
    page.wait_for_selector("#summary .card")
    assert "Shanghai" in page.inner_text("#summary")
    assert "Best value: Shanghai" in page.inner_text("#rec")
    assert page.query_selector("table") is not None
