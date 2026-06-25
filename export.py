"""Export utilities for Whenever - PDF and CSV rendering of run_search output.

This module is pure-Python (fpdf2 + stdlib csv) with no system library
dependencies. Both functions consume the dict returned by run_search().
"""
import csv
import io
from fpdf import FPDF
from fpdf.enums import XPos, YPos

# CSV column order matches the roadmap spec exactly
_CSV_COLUMNS = [
    "city", "iata", "dep_date", "ret_date",
    "cheapest_cad", "stops", "duration_min", "nonstop_cad",
    "chosen", "chosen_cad", "source", "book",
]


def _cell_to_row(city, iata, cell):
    """Convert a grid cell dict to a CSV row list. None values become ''."""
    def _s(v):
        return "" if v is None else str(v)

    return [
        city,
        iata,
        _s(cell.get("dep")),
        _s(cell.get("ret")),
        _s(cell.get("cheapest_cad")),
        _s(cell.get("stops")),
        _s(cell.get("duration_min")),
        _s(cell.get("nonstop_cad")),
        _s(cell.get("chosen")),
        _s(cell.get("chosen_cad")),
        _s(cell.get("source")),
        _s(cell.get("book")),
    ]


def render_csv(result: dict) -> str:
    """Render run_search output as a CSV string.

    Columns: city, iata, dep_date, ret_date, cheapest_cad, stops,
             duration_min, nonstop_cad, chosen, chosen_cad, source, book.
    One row per (city, dep_date, ret_date) grid cell.
    None/no-data cells render as empty strings and never crash.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_COLUMNS)

    for dest in result.get("results", []):
        city = dest.get("city", "")
        iata = dest.get("iata", "")
        for row in dest.get("grid", []):
            for cell in row:
                writer.writerow(_cell_to_row(city, iata, cell))

    return buf.getvalue()


# ---------------------------------------------------------------------------
# PDF helpers
# ---------------------------------------------------------------------------

_PAGE_W = 210  # A4 mm
_MARGIN = 12
_BODY_W = _PAGE_W - 2 * _MARGIN


def _fmt_dur(minutes):
    """Render total minutes as "Xh Ym", or "" when minutes is None/unparseable."""
    if minutes is None:
        return ""
    try:
        h, m = divmod(int(minutes), 60)
    except (TypeError, ValueError):
        return ""
    return f"{h}h {m}m"


def _pdf_safe(s):
    """Coerce any value to a Latin-1-safe string for fpdf2's built-in fonts.

    fpdf2's bundled Helvetica is Latin-1 only, so non-Latin-1 characters
    (e.g. in "Łódź" or "東京") would raise FPDFUnicodeEncodingException.
    Unsupported characters become "?". A future enhancement is to bundle a
    Unicode TTF font for proper non-Latin rendering.
    """
    return str(s).encode("latin-1", "replace").decode("latin-1")


def render_pdf(result: dict) -> bytes:
    """Render run_search output as a PDF using fpdf2.

    Includes:
    - Title with origin, passenger summary, currency.
    - Recommendation text.
    - Per-city best summary line.
    - Dep × ret matrix table per city.

    Returns bytes starting with b'%PDF'.
    """
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=10)
    pdf.add_page()
    pdf.set_margins(_MARGIN, 10, _MARGIN)

    origin = result.get("origin", "")
    adults = result.get("adults", 0)
    child_ages = result.get("child_ages") or []
    families = result.get("families", 1)
    recommendation = result.get("recommendation", "")
    results = result.get("results") or []

    # ---- Title ----
    kids_str = f", {len(child_ages)} child{'ren' if len(child_ages) != 1 else ''}" if child_ages else ""
    pax_str = f"{adults} adult{'s' if adults != 1 else ''}{kids_str}"
    fam_str = f", {families} {'families' if families != 1 else 'family'}"
    title = f"Whenever Flight Matrix - From {origin} | {pax_str}{fam_str} | Currency: CAD"

    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, _pdf_safe(title), new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    pdf.ln(3)

    # ---- Recommendation ----
    if recommendation:
        pdf.set_font("Helvetica", "I", 10)
        pdf.multi_cell(0, 6, _pdf_safe(f"Recommendation: {recommendation}"))
        pdf.ln(4)

    # ---- Per-city sections ----
    for dest in results:
        city = dest.get("city", "") or ""
        iata = dest.get("iata", "") or ""
        best = dest.get("best")
        grid = dest.get("grid") or []

        # City heading
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 7, _pdf_safe(f"{city} ({iata})"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        # Best summary line
        if best:
            best_price = best.get("chosen_cad")
            best_dep = best.get("dep", "") or ""
            best_ret = best.get("ret", "") or ""
            best_chosen = best.get("chosen", "") or ""
            best_stops = best.get("stops")
            stops_label = (
                "nonstop" if best_stops == 0
                else f"{best_stops} stop{'s' if best_stops != 1 else ''}"
            )
            best_dur = _fmt_dur(best.get("duration_min"))
            dur_label = f", {best_dur}" if best_dur else ""
            summary = (
                f"  Best: CA${best_price:,} {best_chosen} ({stops_label}{dur_label}), "
                f"dep {best_dep} ret {best_ret}"
            ) if best_price else "  Best: no priceable options"
        else:
            summary = "  Best: no priceable options"

        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 5, _pdf_safe(summary), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(1)

        if not grid:
            pdf.ln(2)
            continue

        # Build matrix: rows = dep dates, cols = ret dates
        # Collect unique dep/ret dates from the grid
        dep_dates = []
        ret_dates = []
        for r_row in grid:
            if r_row:
                dep_val = r_row[0].get("dep", "")
                if dep_val not in dep_dates:
                    dep_dates.append(dep_val)
        if grid and grid[0]:
            for cell in grid[0]:
                ret_val = cell.get("ret", "")
                if ret_val not in ret_dates:
                    ret_dates.append(ret_val)

        # Determine column width
        n_cols = len(ret_dates) + 1  # +1 for the dep-date label column
        page_w = 297 - 2 * _MARGIN  # landscape A4
        col_w = page_w / n_cols
        row_h = 6

        # Header row (ret dates)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(220, 220, 220)
        pdf.cell(col_w, row_h, _pdf_safe("dep \\ ret"), border=1, fill=True)
        for rd in ret_dates:
            pdf.cell(col_w, row_h, _pdf_safe(rd), border=1, fill=True)
        pdf.ln()

        # Data rows
        pdf.set_font("Helvetica", "", 8)
        for r_idx, r_row in enumerate(grid):
            dep_label = dep_dates[r_idx] if r_idx < len(dep_dates) else ""
            pdf.cell(col_w, row_h, _pdf_safe(dep_label), border=1)
            for cell in r_row:
                cheap = cell.get("cheapest_cad")
                stops = cell.get("stops")
                chosen = cell.get("chosen", "")
                chosen_cad = cell.get("chosen_cad")
                if cheap is None:
                    label = "-"
                else:
                    ns_mark = "*" if chosen == "nonstop" else ""
                    stops_s = str(stops) if stops is not None else "?"
                    dur = _fmt_dur(cell.get("duration_min"))
                    dur_s = f" {dur}" if dur else ""
                    label = f"CA${chosen_cad:,}{ns_mark} ({stops_s}st){dur_s}"
                pdf.cell(col_w, row_h, _pdf_safe(label), border=1)
            pdf.ln()

        pdf.ln(4)

    return bytes(pdf.output())
