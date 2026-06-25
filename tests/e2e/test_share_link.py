"""E2E tests for the shareable search link feature."""


def test_share_hash_written_after_search(live_server, page):
    page.goto(live_server)
    # Load cities
    page.click("#loadCities")
    page.wait_for_selector(".chip")
    # Run search
    page.click("#run")
    page.wait_for_selector("#summary .card")
    # The URL hash + Copy-link state are written on the later `done` event, not
    # on `meta` (which fires the first card). Wait for the done-driven state
    # (Copy-link enabled) before asserting the hash, or we race replaceState.
    page.wait_for_selector("#copyLink:not([disabled])")
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
    # Date inputs must be prefilled to the shared values. Before the
    # timezone-safe validator this round-tripped through local→UTC and got
    # rejected in UTC+ zones, leaving the inputs at their defaults.
    assert page.input_value("#depStart") == "2026-12-12"
    assert page.input_value("#retStart") == "2027-01-04"
    # Auto-run must fire: #summary populates without user clicking
    page.wait_for_selector("#summary .card")
    summary_text = page.inner_text("#summary")
    assert "Beijing" in summary_text or "Shanghai" in summary_text


def test_share_date_validator_is_timezone_safe(browser, live_server):
    """Loading a share hash with custom dates under a UTC+ timezone must still
    prefill the date inputs. With the old `new Date(s+'T00:00:00').toISOString()`
    round-trip, local midnight in Asia/Tokyo shifts to the previous UTC day and
    the valid date was wrongly rejected (inputs stayed at defaults)."""
    import json, urllib.parse
    context = browser.new_context(timezone_id="Asia/Tokyo")
    page = context.new_page()
    try:
        share = {
            "depCity": "Toronto", "depCode": "YYZ", "country": "China",
            "cities": [{"city": "Beijing", "iata": "PEK"}],
            "adults": 2, "child_ages": [], "families": 1,
            "dep_start": "2026-12-12", "dep_span": 2,
            "ret_start": "2027-01-04", "ret_span": 2,
            "nonstop_threshold": 25,
        }
        hash_val = "#s=" + urllib.parse.quote(json.dumps(share))
        page.goto(live_server + "/" + hash_val)
        # Dates restored despite the UTC+ timezone.
        assert page.input_value("#depStart") == "2026-12-12"
        assert page.input_value("#retStart") == "2027-01-04"
        # The validator accepts valid dates and rejects impossible ones,
        # regardless of timezone (direct check of the in-page helper).
        assert page.evaluate("isValidIsoDate('2026-12-12')") is True
        assert page.evaluate("isValidIsoDate('2027-01-04')") is True
        assert page.evaluate("isValidIsoDate('2026-13-40')") is False
        assert page.evaluate("isValidIsoDate('2026-02-30')") is False
        assert page.evaluate("isValidIsoDate('not-a-date')") is False
    finally:
        context.close()


def test_copy_link_disabled_until_new_search_done(live_server, page):
    """Copy-link must follow LAST_PAYLOAD like the export buttons: a new search
    disables it at the start and only re-enables it on the fresh `done`."""
    page.goto(live_server)
    page.click("#loadCities")
    page.wait_for_selector(".chip")
    # First search: Copy-link becomes enabled on done.
    page.click("#run")
    page.wait_for_selector("#summary .card")
    page.wait_for_selector("#copyLink:not([disabled])")
    assert not page.is_disabled("#copyLink")
    # Start a second search: Copy-link must be disabled immediately and stay
    # disabled until the new done re-enables it.
    page.click("#run")
    assert page.is_disabled("#copyLink")
    page.wait_for_selector("#copyLink:not([disabled])")
    assert not page.is_disabled("#copyLink")


def test_malformed_hash_loads_default_form(live_server, page):
    page.goto(live_server + "/#s=not-json")
    page.wait_for_timeout(500)
    # Default depCode still YYZ (no crash)
    assert page.input_value("#depCode") == "YYZ"
    # No JS exceptions — page still works: can click run after loading chips
    page.click("#loadCities")
    page.wait_for_selector(".chip")
    assert page.query_selector(".chip") is not None


def test_oversized_hash_falls_back_gracefully(live_server, page):
    import json, urllib.parse
    # Oversized / malformed payload: junk types everywhere, huge city name,
    # bad IATA. Must drop the bad cities and keep the page usable.
    share = {
        "depCity": "X" * 100000,
        "depCode": "not-a-code",
        "country": {"nope": True},
        "cities": [
            {"city": "Bad", "iata": "TOOLONG"},
            {"city": "Bad2", "iata": 123},
            "not-an-object",
        ],
        "adults": "lots",
        "dep_start": "2026-13-99",
    }
    hash_val = "#s=" + urllib.parse.quote(json.dumps(share))
    page.goto(live_server + "/" + hash_val)
    page.wait_for_timeout(500)
    # Invalid depCode dropped → default YYZ retained.
    assert page.input_value("#depCode") == "YYZ"
    # All cities had invalid IATA → none restored → no auto-run / no crash.
    # Page still works: load real chips.
    page.click("#loadCities")
    page.wait_for_selector(".chip")
    assert page.query_selector(".chip") is not None


def test_share_hash_city_xss_is_neutralized(live_server, page):
    """A crafted share link with an <img onerror> city must NOT execute."""
    import json, urllib.parse
    payload = "<img src=x onerror=window.__xss=1>"
    share = {
        "depCity": "Toronto", "depCode": "YYZ", "country": "China",
        # Valid IATA so the city is restored + auto-run renders it into the grid.
        "cities": [{"city": payload, "iata": "PEK"}],
        "adults": 2, "child_ages": [11, 9], "families": 1,
        "dep_start": "2026-12-12", "dep_span": 2,
        "ret_start": "2027-01-04", "ret_span": 2,
        "nonstop_threshold": 25,
    }
    hash_val = "#s=" + urllib.parse.quote(json.dumps(share))
    page.goto(live_server + "/" + hash_val)
    # Auto-run renders the malicious city into summary card + <h2> heading.
    page.wait_for_selector("#summary .card")
    page.wait_for_timeout(500)
    # 1. No script executed: the onerror handler never fired.
    assert not page.evaluate("window.__xss")
    # 2. No live <img> element was injected from the payload.
    assert page.query_selector('img[src="x"]') is None
    assert page.query_selector("#summary img") is None
    assert page.query_selector("#grids img") is None
    # 3. The value renders as inert text — the literal string is present in the
    #    rendered card/heading text content.
    summary_text = page.inner_text("#summary")
    grids_text = page.inner_text("#grids")
    assert payload in summary_text or payload in grids_text
