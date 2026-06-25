"""E2E tests for the shareable search link feature."""


def test_share_hash_written_after_search(live_server, page):
    page.goto(live_server)
    # Load cities
    page.click("#loadCities")
    page.wait_for_selector(".chip")
    # Run search
    page.click("#run")
    page.wait_for_selector("#summary .card")
    # URL hash must contain share param
    assert "#s=" in page.url
    # Copy link button must be enabled
    assert not page.is_disabled("#copyLink")


def test_load_from_hash_prefills_and_autoruns(live_server, page):
    import json, urllib.parse
    share = {
        "depCity": "Toronto", "depCode": "YYZ", "country": "China",
        "cities": [{"city": "Beijing", "iata": "PEK"}, {"city": "Shanghai", "iata": "PVG"}],
        "adults": 2, "child_ages": [11, 9], "families": 3,
        "dep_start": "2026-12-12", "dep_span": 2,
        "ret_start": "2027-01-04", "ret_span": 2,
        "nonstop_threshold": 25,
    }
    hash_val = "#s=" + urllib.parse.quote(json.dumps(share))
    page.goto(live_server + "/" + hash_val)
    # Form must be prefilled
    assert page.input_value("#depCode") == "YYZ"
    assert page.input_value("#country") == "China"
    # Chips must be rendered as selected (class 'on')
    page.wait_for_selector(".chip.on")
    chip_texts = [el.inner_text() for el in page.query_selector_all(".chip.on")]
    chip_text_joined = " ".join(chip_texts)
    assert "Beijing" in chip_text_joined
    # Auto-run must fire: #summary populates without user clicking
    page.wait_for_selector("#summary .card")
    summary_text = page.inner_text("#summary")
    assert "Beijing" in summary_text or "Shanghai" in summary_text


def test_malformed_hash_loads_default_form(live_server, page):
    page.goto(live_server + "/#s=not-json")
    page.wait_for_timeout(500)
    # Default depCode still YYZ (no crash)
    assert page.input_value("#depCode") == "YYZ"
    # No JS exceptions — page still works: can click run after loading chips
    page.click("#loadCities")
    page.wait_for_selector(".chip")
    assert page.query_selector(".chip") is not None
