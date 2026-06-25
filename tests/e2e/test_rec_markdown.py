"""E2E tests for issue #34: recommendation markdown rendering.

The AI recommendation may contain markdown (e.g. **bold** and newlines).
These tests assert:
  1. Bold markdown (**text**) renders as <strong> elements (not literal **).
  2. No literal ** remain visible in #rec after rendering.
  3. HTML/script injection in model output is escaped — no injected elements.
"""

from tests.e2e.conftest import select_all_chips


def test_rec_bold_renders_as_strong(markdown_live_server, page):
    """**bold** in recommendation must render as a <strong> element."""
    page.goto(markdown_live_server)
    page.click("#loadCities")
    select_all_chips(page)
    page.click("#run")

    # Wait for recommendation to appear
    page.wait_for_selector("#rec", state="visible", timeout=15000)

    # Must contain a <strong> element (bold markdown was converted)
    strong_el = page.query_selector("#rec strong")
    assert strong_el is not None, (
        "Expected a <strong> element inside #rec when recommendation contains **bold**"
    )

    # The <strong> text must contain the bolded content
    strong_text = strong_el.inner_text()
    assert "Best value:" in strong_text, (
        f"Expected 'Best value:' in <strong> text, got: {strong_text!r}"
    )


def test_rec_no_literal_asterisks(markdown_live_server, page):
    """No literal ** must remain visible in #rec after markdown rendering."""
    page.goto(markdown_live_server)
    page.click("#loadCities")
    select_all_chips(page)
    page.click("#run")

    page.wait_for_selector("#rec", state="visible", timeout=15000)

    rec_text = page.inner_text("#rec")
    assert "**" not in rec_text, (
        f"Literal ** found in #rec text after markdown rendering: {rec_text!r}"
    )


def test_rec_newline_renders_as_line_break(markdown_live_server, page):
    """Newlines in recommendation must produce visible line-break separation."""
    page.goto(markdown_live_server)
    page.click("#loadCities")
    select_all_chips(page)
    page.click("#run")

    page.wait_for_selector("#rec", state="visible", timeout=15000)

    # A <br> element should be present inside #rec (newline was converted)
    br_count = page.eval_on_selector("#rec", "el => el.querySelectorAll('br').length")
    assert br_count >= 1, (
        f"Expected at least one <br> in #rec when recommendation contains newlines, "
        f"got {br_count}"
    )


def test_rec_xss_script_is_escaped(xss_live_server, page):
    """Model output containing <script> must be HTML-escaped, not executed."""
    page.goto(xss_live_server)
    page.click("#loadCities")
    select_all_chips(page)
    page.click("#run")

    page.wait_for_selector("#rec", state="visible", timeout=15000)

    # No injected <script> element must exist inside #rec
    script_el = page.query_selector("#rec script")
    assert script_el is None, (
        "A <script> element was injected into #rec — XSS escaping failed"
    )

    # The angle brackets must appear as literal text (escaped), not as HTML
    rec_text = page.inner_text("#rec")
    # The raw text content should NOT contain the executed script tag
    # but the escaped version (&lt;script&gt;) renders as text visible to user
    # inner_text gives visible text — it will show "<script>" as text if escaped
    assert "Best value" in rec_text, (
        f"Expected 'Best value' text in #rec after XSS escape, got: {rec_text!r}"
    )


def test_rec_xss_injected_b_tag_not_rendered(xss_live_server, page):
    """Model output containing <b>injected</b> must not create a real <b> element."""
    page.goto(xss_live_server)
    page.click("#loadCities")
    select_all_chips(page)
    page.click("#run")

    page.wait_for_selector("#rec", state="visible", timeout=15000)

    # The injected <b> tag should not become a real DOM element inside #rec
    # (it gets escaped to &lt;b&gt; so it shows as text, not as a bold element)
    b_elements = page.eval_on_selector_all("#rec b", "els => els.length")
    assert b_elements == 0, (
        f"Found {b_elements} injected <b> element(s) in #rec — HTML was not escaped"
    )
