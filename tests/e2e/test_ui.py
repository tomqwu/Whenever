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


def test_toronto_china_full_flow(seed_live_server, page):
    """Canonical full-flow e2e: Toronto (YYZ) → China using real offline seed cities."""
    page.goto(seed_live_server)

    # Assert default form values — canonical Toronto→China scenario
    assert page.input_value("#depCode") == "YYZ"
    assert page.input_value("#country") == "China"

    # Click "Get top cities" — triggers /api/top-cities with country=China.
    # The real seed path (config/country_seeds.yaml) returns cities without LLM.
    page.click("#loadCities")

    # Wait for chips to appear
    page.wait_for_selector(".chip")

    # Collect all chip texts
    chip_texts = [el.inner_text() for el in page.query_selector_all(".chip")]
    chip_text_joined = " ".join(chip_texts)

    # Required seed cities must appear
    assert "Beijing (PEK)" in chip_text_joined, f"Beijing not found in chips: {chip_texts}"
    assert "Shanghai (PVG)" in chip_text_joined, f"Shanghai not found in chips: {chip_texts}"

    # A country expansion now starts with EVERY chip UNCHECKED (opt-in UX): no
    # chip may carry class 'on' until the user explicitly clicks one.
    assert page.query_selector(".chip.on") is None, \
        "No chip should be pre-selected after a country expansion (all unchecked)"

    # Explicitly select Beijing before running (expansion no longer auto-selects).
    # drawChips() rebuilds the chip DOM on toggle, so re-query after clicking.
    beijing_chip = next(
        el for el in page.query_selector_all(".chip")
        if "Beijing" in el.inner_text()
    )
    beijing_chip.click()
    page.wait_for_selector(".chip.on:has-text('Beijing')")
    assert page.query_selector(".chip.on:has-text('Beijing')") is not None, \
        "Beijing chip should be .on after the user clicks it"

    # Click Run — triggers /api/search with the user-selected city
    page.click("#run")
    page.wait_for_selector("#summary .card")

    # Grid and summary must render for the China cities
    assert page.query_selector("table") is not None, "Fare grid table should be rendered"
    summary_text = page.inner_text("#summary")
    assert any(city in summary_text for city in ("Beijing", "Shanghai", "Guangzhou")), \
        f"Expected a required China city in #summary, got: {summary_text!r}"

    # Recommendation text must appear
    rec_text = page.inner_text("#rec")
    assert rec_text.strip(), "Recommendation (#rec) should not be empty"
    assert "Best value" in rec_text, f"Expected 'Best value' in #rec, got: {rec_text!r}"
