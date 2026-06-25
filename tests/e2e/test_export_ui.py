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
