"""E2E tests for the Export PDF / Export CSV buttons in the UI.

Uses Playwright's page.expect_download() to assert a file download is triggered
when the user clicks the export buttons after running a search.
"""


def _run_search(page, base_url):
    """Navigate and run a full search flow, returning after stream completes."""
    page.goto(base_url)
    page.click("#loadCities")
    page.wait_for_selector(".chip")
    page.click("#run")
    # Wait for stream to complete (recommendation visible)
    page.wait_for_selector("#rec", state="visible", timeout=15000)
    # Export buttons are armed on the later `done` event, not when #rec appears.
    # Wait for them to actually become enabled before returning, otherwise a
    # follow-up click can race a still-disabled button and time out.
    page.wait_for_selector("#exportPdf:not([disabled])", timeout=15000)
    page.wait_for_selector("#exportCsv:not([disabled])", timeout=15000)


def test_export_buttons_disabled_before_search(seed_live_server, page):
    """Export buttons must be disabled when the page first loads (no search yet)."""
    page.goto(seed_live_server)
    pdf_disabled = page.get_attribute("#exportPdf", "disabled")
    csv_disabled = page.get_attribute("#exportCsv", "disabled")
    assert pdf_disabled is not None, "Export PDF button should be disabled before a search"
    assert csv_disabled is not None, "Export CSV button should be disabled before a search"


def test_export_buttons_enabled_after_search(seed_live_server, page):
    """Export buttons must be enabled after a successful search completes."""
    _run_search(page, seed_live_server)
    pdf_disabled = page.get_attribute("#exportPdf", "disabled")
    csv_disabled = page.get_attribute("#exportCsv", "disabled")
    assert pdf_disabled is None, "Export PDF button should be enabled after search"
    assert csv_disabled is None, "Export CSV button should be enabled after search"


def test_export_csv_triggers_download(seed_live_server, page):
    """Clicking Export CSV after a search must trigger a file download ending in .csv."""
    _run_search(page, seed_live_server)
    with page.expect_download(timeout=15000) as dl_info:
        page.click("#exportCsv")
    download = dl_info.value
    assert download.suggested_filename.endswith(".csv"), (
        f"Expected CSV download, got: {download.suggested_filename!r}"
    )


def test_export_pdf_triggers_download(seed_live_server, page):
    """Clicking Export PDF after a search must trigger a file download ending in .pdf."""
    _run_search(page, seed_live_server)
    with page.expect_download(timeout=15000) as dl_info:
        page.click("#exportPdf")
    download = dl_info.value
    assert download.suggested_filename.endswith(".pdf"), (
        f"Expected PDF download, got: {download.suggested_filename!r}"
    )


def test_export_buttons_disabled_when_second_search_fails(seed_live_server, page):
    """A failed NEW search must clear stale export state.

    After a successful first search the export buttons are enabled. When a
    second search starts the buttons are disabled immediately and are only
    re-armed on the `done` event. If that second search fails (here: forced
    400), `done` never fires, so the buttons must stay disabled — the page must
    not offer PDF/CSV export of the previous, now-invalidated result.
    """
    _run_search(page, seed_live_server)
    # Sanity: first search succeeded, buttons enabled.
    assert page.get_attribute("#exportPdf", "disabled") is None
    assert page.get_attribute("#exportCsv", "disabled") is None

    # Force the next stream request to fail with a 400.
    page.route(
        "**/api/search/stream",
        lambda route: route.fulfill(
            status=400,
            content_type="application/json",
            body='{"error": "forced failure"}',
        ),
    )
    page.on("dialog", lambda d: d.dismiss())  # dismiss the "Search failed" alert

    page.click("#run")
    # The export buttons must become disabled (cleared at search start) and stay
    # disabled because the failed search never emits `done`.
    page.wait_for_selector("#exportPdf[disabled]", timeout=15000)
    page.wait_for_selector("#exportCsv[disabled]", timeout=15000)
    assert page.get_attribute("#exportPdf", "disabled") is not None
    assert page.get_attribute("#exportCsv", "disabled") is not None


def test_export_button_stays_disabled_when_newer_search_invalidates_payload(
    seed_live_server, page
):
    """Race: export in flight, then a NEW search clears LAST_PAYLOAD.

    The user starts an export, then — before the export request resolves —
    starts a new search. The search handler clears LAST_PAYLOAD and disables
    both export buttons. When the (old) export request finally completes, its
    `finally` must NOT unconditionally re-enable the button: because
    LAST_PAYLOAD is now null, the button must stay disabled (clicking it would
    silently no-op).

    Determinism note: this models the race entirely inside the page (no route
    handler held open across page actions, which deadlocks Playwright's sync
    dispatcher). We launch the real export, then drive a real new search via
    the production click handler before the export resolves. A forced-400 on
    the stream means that new search clears LAST_PAYLOAD and never re-arms the
    buttons, so the only thing that could re-enable the button is the export's
    `finally` — which is exactly the guard under test.
    """
    _run_search(page, seed_live_server)
    assert page.get_attribute("#exportCsv", "disabled") is None

    # Force the new search's stream to fail so it never re-arms (emits `done`)
    # the buttons: LAST_PAYLOAD stays null for the duration of the assertion,
    # isolating the export `finally` behaviour under test.
    page.route(
        "**/api/search/stream",
        lambda route: route.fulfill(
            status=400,
            content_type="application/json",
            body='{"error": "forced failure"}',
        ),
    )
    page.on("dialog", lambda d: d.dismiss())  # dismiss any alert defensively

    # Inside the page: start a real export (its try/finally executes against a
    # real network round-trip), then immediately fire the production new-search
    # click handler. The search synchronously clears LAST_PAYLOAD and disables
    # the buttons before the export's awaited fetch resolves. We then await the
    # export so its `finally` has definitely run by the time evaluate returns.
    payload_when_done = page.evaluate(
        """async () => {
            const p = triggerExport('/api/export/csv', 'whenever-matrix.csv',
                document.querySelector('#exportCsv'), 'Preparing CSV…');
            document.querySelector('#run').click();  // real new search clears LAST_PAYLOAD
            await p;
            return LAST_PAYLOAD;
        }"""
    )
    assert payload_when_done is None, (
        "A new search should have cleared LAST_PAYLOAD before the export resolved"
    )
    assert page.get_attribute("#exportCsv", "disabled") is not None, (
        "Export CSV must stay disabled when a newer search cleared LAST_PAYLOAD"
    )


def test_export_finally_keeps_button_disabled_when_payload_nulled_mid_flight(
    seed_live_server, page
):
    """Focused: the `finally` must key re-enable off LAST_PAYLOAD, not run blind.

    Awaits a real triggerExport call (so the try/finally executes) while nulling
    LAST_PAYLOAD before the request resolves — exactly what a concurrent new
    search does. The `finally` must leave the button disabled because
    LAST_PAYLOAD is null.
    """
    _run_search(page, seed_live_server)
    assert page.get_attribute("#exportCsv", "disabled") is None

    # Start the export and null LAST_PAYLOAD while it is in flight (microtask
    # after kickoff), then await completion — all inside the page so the
    # finally has definitely run by the time evaluate resolves.
    page.evaluate(
        """async () => {
            const p = triggerExport('/api/export/csv', 'whenever-matrix.csv',
                document.querySelector('#exportCsv'), 'Preparing CSV…');
            LAST_PAYLOAD = null;  // a newer search invalidated the payload
            await p;
        }"""
    )
    assert page.get_attribute("#exportCsv", "disabled") is not None, (
        "Export CSV must stay disabled when LAST_PAYLOAD was nulled mid-flight"
    )


def test_export_csv_filename_is_whenever_matrix(seed_live_server, page):
    """The CSV download filename should be whenever-matrix.csv."""
    _run_search(page, seed_live_server)
    with page.expect_download(timeout=15000) as dl_info:
        page.click("#exportCsv")
    download = dl_info.value
    assert download.suggested_filename == "whenever-matrix.csv", (
        f"Unexpected CSV filename: {download.suggested_filename!r}"
    )


def test_export_pdf_filename_is_whenever_matrix(seed_live_server, page):
    """The PDF download filename should be whenever-matrix.pdf."""
    _run_search(page, seed_live_server)
    with page.expect_download(timeout=15000) as dl_info:
        page.click("#exportPdf")
    download = dl_info.value
    assert download.suggested_filename == "whenever-matrix.pdf", (
        f"Unexpected PDF filename: {download.suggested_filename!r}"
    )
