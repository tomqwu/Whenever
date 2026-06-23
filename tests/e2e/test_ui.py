def test_page_loads_and_health_renders(live_server, page):
    page.goto(live_server)
    page.wait_for_function(
        "() => document.querySelector('#status') && "
        "document.querySelector('#status').textContent.toLowerCase().includes('deepseek')"
    )
    status = page.inner_text("#status")
    assert "deepseek" in status.lower()           # model name from /api/health


def test_full_search_flow(live_server, page):
    page.goto(live_server)
    page.click("#loadCities")
    page.wait_for_selector(".chip.on")             # Shanghai chip, toggled on
    page.click("#run")
    page.wait_for_selector("#summary .card")
    assert "Shanghai" in page.inner_text("#summary")
    assert "Best value: Shanghai" in page.inner_text("#rec")
    assert page.query_selector("table") is not None
