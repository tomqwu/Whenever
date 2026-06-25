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


def test_card_watch_button_reenabled_after_remove(seed_live_server, page):
    """Removing a watched trip re-enables the matching card Watch button so the
    same trip can be re-watched without re-running the search (codex review)."""
    _run_search(page, seed_live_server)

    # Watch card-0's best trip -> button flips to the disabled 'Watching ✓' state.
    page.click("#card-watch-0")
    page.wait_for_function(
        "() => { const b = document.getElementById('card-watch-0'); "
        "return b && b.disabled && b.classList.contains('watched'); }",
        timeout=15000,
    )
    page.wait_for_selector("#watchedList .watched-item", timeout=15000)

    # Remove it from the Watched trips list.
    page.click("#watchedList .watched-item button.rm")

    # The matching card Watch button returns to the enabled '☆ Watch' state.
    page.wait_for_function(
        "() => { const b = document.getElementById('card-watch-0'); "
        "return b && !b.disabled && !b.classList.contains('watched') "
        "&& b.textContent.indexOf('Watch') !== -1 "
        "&& b.textContent.indexOf('Watching') === -1; }",
        timeout=15000,
    )

    # And it is re-watchable: clicking again flips it back to 'Watching ✓' and
    # re-creates the watched-trips item.
    page.click("#card-watch-0")
    page.wait_for_function(
        "() => { const b = document.getElementById('card-watch-0'); "
        "return b && b.disabled && b.classList.contains('watched'); }",
        timeout=15000,
    )
    page.wait_for_selector("#watchedList .watched-item", timeout=15000)


def test_card_button_not_reenabled_by_unrelated_watch_remove(seed_live_server, page):
    """Removing a DIFFERENT saved watch that shares dest+dates but has a different
    origin must NOT re-enable the visible card button — re-enable matches the FULL
    trip key (origin + dest + dates + party), not just dest+dates (codex review)."""
    _run_search(page, seed_live_server)

    # Watch card-0's best trip (YYZ -> PEK on some dates) -> 'Watching ✓'.
    page.click("#card-watch-0")
    page.wait_for_function(
        "() => { const b = document.getElementById('card-watch-0'); "
        "return b && b.disabled && b.classList.contains('watched'); }",
        timeout=15000,
    )
    page.wait_for_selector("#watchedList .watched-item", timeout=15000)

    # Read the card's own watch so we can clone its dest+dates but change origin.
    own = page.evaluate(
        "async () => (await (await fetch('/api/watch')).json()).watches[0]"
    )

    # Seed an UNRELATED watch: same dest + dates, DIFFERENT origin/party.
    page.evaluate(
        """async (own) => {
            const body = {
                origin: 'JFK',
                dest_iata: own.dest_iata,
                dest_city: own.dest_city,
                dep_date: own.dep_date,
                ret_date: own.ret_date,
                adults: 1,
                child_ages: [],
            };
            await fetch('/api/watch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            // Re-render the Watched trips list so the new row appears in the DOM.
            await loadWatches();
        }""",
        own,
    )
    # Two distinct watches now exist.
    page.wait_for_function(
        "() => document.querySelectorAll('#watchedList .watched-item').length === 2",
        timeout=15000,
    )

    # Remove the UNRELATED (JFK) watch — its row is the one whose text has 'JFK'.
    page.evaluate(
        """() => {
            const items = [...document.querySelectorAll('#watchedList .watched-item')];
            const jfk = items.find(i => i.innerText.indexOf('JFK') !== -1);
            jfk.querySelector('button.rm').click();
        }"""
    )
    # Wait until only the card's own watch remains.
    page.wait_for_function(
        "() => document.querySelectorAll('#watchedList .watched-item').length === 1",
        timeout=15000,
    )

    # The card button must STILL be disabled/'Watching ✓' — the removed watch was
    # a different trip key, so the visible card must not be wrongly re-enabled.
    assert page.evaluate(
        "() => { const b = document.getElementById('card-watch-0'); "
        "return b.disabled && b.classList.contains('watched'); }"
    )

    # Now remove the MATCHING (card's own) watch -> the card button re-enables.
    page.click("#watchedList .watched-item button.rm")
    page.wait_for_function(
        "() => { const b = document.getElementById('card-watch-0'); "
        "return b && !b.disabled && !b.classList.contains('watched') "
        "&& b.textContent.indexOf('Watching') === -1; }",
        timeout=15000,
    )


def test_watched_list_shows_passenger_party(seed_live_server, page):
    """The watched-trips list shows passenger info using the `children` COUNT so a
    family party is visible (and distinct from adults-only) (codex review)."""
    _run_search(page, seed_live_server)
    page.click("#card-watch-0")
    page.wait_for_selector("#watchedList .watched-item", timeout=15000)

    trip_text = page.inner_text("#watchedList .watched-item")
    # The default search is 2 adults + 1 child, so the party must be shown.
    assert "adults" in trip_text
    data = page.evaluate("async () => (await (await fetch('/api/watch')).json())")
    w = data["watches"][0]
    assert "children" in w
    if w["children"]:
        label = "child" if w["children"] == 1 else "children"
        assert label in trip_text
