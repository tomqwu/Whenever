"""E2E tests for #55 — destination autocomplete type-ahead.

These run the real Flask app (seed_live_server keeps the genuine China seed
expansion and the real /api/suggest route, both fully offline/deterministic).
"""
import requests as req


# --------------------------- API level ---------------------------

def test_suggest_api_country_and_city(seed_live_server):
    """GET /api/suggest returns both country and city matches for 'chi'."""
    r = req.get(f"{seed_live_server}/api/suggest", params={"q": "chi"})
    assert r.status_code == 200
    sug = r.json()["suggestions"]
    assert any(s["type"] == "country" and s["name"] == "China" for s in sug)
    assert any(s["type"] == "city" for s in sug)


def test_suggest_api_iata(seed_live_server):
    """An IATA query (hnd) returns the matching city."""
    sug = req.get(f"{seed_live_server}/api/suggest", params={"q": "hnd"}).json()["suggestions"]
    assert any(s.get("iata") == "HND" and s["city"] == "Tokyo" for s in sug)


def test_suggest_api_hong_kong_city_only(seed_live_server):
    """Hong Kong/Macau/Taiwan are city suggestions only, never countries (#55)."""
    hk = req.get(f"{seed_live_server}/api/suggest", params={"q": "hong"}).json()["suggestions"]
    assert not any(s["type"] == "country" for s in hk)
    assert any(s["type"] == "city" and s.get("iata") == "HKG" for s in hk)

    tw = req.get(f"{seed_live_server}/api/suggest", params={"q": "taiwan"}).json()["suggestions"]
    assert not any(s["type"] == "country" and s["name"] == "Taiwan" for s in tw)
    assert {s.get("iata") for s in tw if s["type"] == "city"} & {"TPE", "KHH"}


# --------------------------- browser ---------------------------

def test_typeahead_dropdown_appears(seed_live_server, page):
    """Typing in the destination field shows the suggestion dropdown."""
    page.goto(seed_live_server)
    page.fill("#country", "")
    page.type("#country", "chi")
    page.wait_for_selector("#suggestList li")
    items = page.query_selector_all("#suggestList li")
    assert len(items) >= 1


def test_pick_country_expands_chips(seed_live_server, page):
    """Clicking a COUNTRY suggestion expands it to its seed cities as chips."""
    page.goto(seed_live_server)
    page.fill("#country", "")
    page.type("#country", "china")
    page.wait_for_selector("#suggestList li")
    # Click the China country option.
    page.click("#suggestList li:has-text('country')")
    page.wait_for_selector(".chip")
    chips = page.inner_text("#cities")
    assert "Beijing" in chips
    assert "Shanghai" in chips


def test_pick_city_adds_single_chip(seed_live_server, page):
    """Clicking a CITY suggestion appends just that city as a chip."""
    page.goto(seed_live_server)
    page.fill("#country", "")
    page.type("#country", "hnd")
    page.wait_for_selector("#suggestList li")
    page.click("#suggestList li:has-text('HND')")
    page.wait_for_selector(".chip")
    chips = page.inner_text("#cities")
    assert "Tokyo" in chips and "HND" in chips
    assert page.query_selector_all(".chip:not(.hint)")  # at least one chip


def test_pick_multiple_cities(seed_live_server, page):
    """Picking two cities leaves BOTH chips present (multiple selection)."""
    page.goto(seed_live_server)
    page.fill("#country", "")
    page.type("#country", "hnd")
    page.wait_for_selector("#suggestList li")
    page.click("#suggestList li:has-text('HND')")
    page.wait_for_selector(".chip:has-text('Tokyo')")
    # input cleared after a city pick — add a second city
    page.type("#country", "kix")
    page.wait_for_selector("#suggestList li:has-text('KIX')")
    page.click("#suggestList li:has-text('KIX')")
    page.wait_for_selector(".chip:has-text('Osaka')")
    chips = page.inner_text("#cities")
    assert "Tokyo" in chips and "Osaka" in chips


def test_keyboard_arrow_enter_selects(seed_live_server, page):
    """ArrowDown + Enter selects the highlighted suggestion."""
    page.goto(seed_live_server)
    page.fill("#country", "")
    page.type("#country", "hnd")
    page.wait_for_selector("#suggestList li")
    page.press("#country", "ArrowDown")
    page.press("#country", "Enter")
    page.wait_for_selector(".chip:has-text('Tokyo')")
    assert "Tokyo" in page.inner_text("#cities")


def test_escape_closes_dropdown(seed_live_server, page):
    """Pressing Escape closes the suggestion dropdown."""
    page.goto(seed_live_server)
    page.fill("#country", "")
    page.type("#country", "chi")
    page.wait_for_selector("#suggestList li")
    page.press("#country", "Escape")
    page.wait_for_selector("#suggestList", state="hidden")
    assert not page.is_visible("#suggestList")


def test_suggestion_value_is_escaped(seed_live_server, page, monkeypatch):
    """An XSS-y suggestion value is rendered as text, never as live markup.

    We can't easily inject into the real dataset from the browser, so assert the
    rendering path uses escapeHtml by evaluating it directly in the page context.
    """
    page.goto(seed_live_server)
    # The page's escapeHtml is the single sanitizer used by renderSuggest().
    escaped = page.evaluate("escapeHtml('<img src=x onerror=alert(1)>')")
    assert "<img" not in escaped
    assert "&lt;img" in escaped
