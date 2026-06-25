"""E2E tests for the Watch-this-trip UI (issue #38).

Drives the browser through a streaming search, clicks a city's Watch button,
and asserts the button flips to the watched state and the trip is persisted
(verified through GET /api/watch, which the page's "Watched trips" list reads).
The watch DB is a throwaway temp file (WATCH_DB set in the e2e fixture).
"""


def _run_search(page, base_url):
    page.goto(base_url)
    page.click("#loadCities")
    page.wait_for_selector(".chip")
    page.click("#run")
    page.wait_for_selector("#rec", state="visible", timeout=15000)
    # Watch buttons are revealed on the `done` event, like the export buttons.
    page.wait_for_selector("#card-watch-0:not([style*='display: none'])", timeout=15000)


def test_watch_button_appears_for_priced_city(seed_live_server, page):
    """A city with a priced best cell must show a Watch button after search."""
    _run_search(page, seed_live_server)
    btn = page.query_selector("#card-watch-0")
    assert btn is not None
    assert btn.is_visible(), "Watch button should be visible for a priced city"
    assert "Watch" in btn.inner_text()


def test_watch_button_saves_trip(seed_live_server, page):
    """Clicking Watch saves the city's best trip and flips to the watched state.

    Persistence is confirmed by the 'Watched trips' list, which is populated from
    GET /api/watch — proving the POST reached the backend and was stored.
    """
    _run_search(page, seed_live_server)

    page.click("#card-watch-0")

    # Button flips to the disabled 'Watching ✓' state.
    page.wait_for_function(
        "() => { const b = document.getElementById('card-watch-0'); "
        "return b && b.disabled && b.classList.contains('watched'); }",
        timeout=15000,
    )
    assert "Watching" in page.inner_text("#card-watch-0")

    # The Watched trips list must now contain one item (loaded via GET /api/watch).
    page.wait_for_selector("#watchedList .watched-item", timeout=15000)
    items = page.query_selector_all("#watchedList .watched-item")
    assert len(items) == 1
    trip_text = page.inner_text("#watchedList .watched-item")
    # card-0 is the first China-seed city (Beijing/PEK); its best trip is watched.
    assert "PEK" in trip_text and "Beijing" in trip_text


def test_watch_baseline_is_derived_server_side(seed_live_server, page):
    """The baseline is derived SERVER-SIDE from get_fare, never from the client.

    Per the REAL-DATA-ONLY guardrail the POST body no longer carries a client
    last_price/last_source — the route re-derives the baseline by calling
    get_fare itself. The mocked backend's get_fare returns cheapest_cad=8000 /
    source="test", so the persisted record (GET /api/watch) reflects 8000/test
    even though the client sends no price.
    """
    _run_search(page, seed_live_server)

    posted = {}

    def _capture(route, request):
        # The glob matches both the POST (save) and the GET (list refresh);
        # only the POST carries the body we want, so ignore the GET.
        if request.method == "POST":
            try:
                posted["body"] = request.post_data_json
            except Exception:
                posted["body"] = None
        route.continue_()

    page.route("**/api/watch", _capture)
    try:
        page.click("#card-watch-0")
        page.wait_for_selector("#watchedList .watched-item", timeout=15000)
    finally:
        page.unroute("**/api/watch", _capture)

    # The client must NOT send a baseline — it is server-derived now.
    assert posted.get("body") is not None, "POST /api/watch body was not captured"
    assert "last_price" not in posted["body"]
    assert "last_source" not in posted["body"]

    # The persisted baseline comes from the server-side get_fare (8000/test).
    data = page.evaluate("async () => (await (await fetch('/api/watch')).json())")
    watches = data["watches"]
    assert len(watches) == 1
    assert watches[0]["last_price"] == 8000
    assert watches[0]["last_source"] == "test"


def test_watch_remove_button(seed_live_server, page):
    """The ✕ remove button DELETEs a watched trip and clears it from the list."""
    _run_search(page, seed_live_server)
    page.click("#card-watch-0")
    page.wait_for_selector("#watchedList .watched-item", timeout=15000)

    page.click("#watchedList .watched-item button.rm")

    # The list must become empty (shows the empty-state hint).
    page.wait_for_selector("#watchedList .watched-empty", timeout=15000)
    items = page.query_selector_all("#watchedList .watched-item")
    assert len(items) == 0
