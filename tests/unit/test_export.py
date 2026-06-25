"""Unit tests for export.py — render_csv and render_pdf.

Follows strict TDD: tests were written before the implementation module exists.
All tests are offline and deterministic.
"""
import csv
import io
import pytest


# ---------------------------------------------------------------------------
# Shared fixture: a hand-crafted run_search-shaped result dict
# ---------------------------------------------------------------------------

def _make_result():
    """Build a minimal but representative run_search output dict."""
    return {
        "origin": "YYZ",
        "adults": 2,
        "child_ages": [5, 8],
        "families": 1,
        "dep_dates": ["2026-12-12", "2026-12-19"],
        "ret_dates": ["2027-01-04", "2027-01-11"],
        "recommendation": "Best value is Shanghai at CA$8,000.",
        "providers": ["travelpayouts"],
        "results": [
            {
                "city": "Shanghai",
                "iata": "PVG",
                "grid": [
                    # dep 2026-12-12
                    [
                        {
                            "dep": "2026-12-12", "ret": "2027-01-04",
                            "cheapest_cad": 8000, "stops": 1,
                            "duration_min": 875,
                            "nonstop_cad": 8500, "nonstop_duration_min": 600,
                            "chosen": "cheapest", "chosen_stops": 1,
                            "chosen_cad": 8000, "chosen_duration_min": 875,
                            "airlines": ["Air Canada", "ANA"],
                            "layovers": [{"iata": "NRT", "duration_min": 80}],
                            "source": "travelpayouts",
                            "book": "https://www.aviasales.com/link1",
                        },
                        {
                            "dep": "2026-12-12", "ret": "2027-01-11",
                            "cheapest_cad": None, "stops": None,
                            "nonstop_cad": None, "chosen": "cheapest",
                            "chosen_cad": None,  # no-data cell
                            "source": "no-data",
                            "book": "https://www.kayak.com/fallback",
                        },
                    ],
                    # dep 2026-12-19
                    [
                        {
                            "dep": "2026-12-19", "ret": "2027-01-04",
                            "cheapest_cad": 9000, "stops": 0,
                            "duration_min": 700,
                            "nonstop_cad": 9000, "nonstop_duration_min": 700,
                            "chosen": "nonstop", "chosen_stops": 0,
                            "chosen_cad": 9000, "chosen_duration_min": 700,
                            "source": "travelpayouts",
                            "book": "https://www.aviasales.com/link2",
                        },
                        {
                            "dep": "2026-12-19", "ret": "2027-01-11",
                            "cheapest_cad": 7500, "stops": 2,
                            "nonstop_cad": None, "chosen": "cheapest",
                            "chosen_cad": 7500,
                            "source": "travelpayouts",
                            "book": "https://www.aviasales.com/link3",
                        },
                    ],
                ],
                "best": {
                    "dep": "2026-12-19", "ret": "2027-01-11",
                    "cheapest_cad": 7500, "chosen_cad": 7500,
                    "chosen": "cheapest", "stops": 2, "chosen_stops": 2,
                    "duration_min": 950, "chosen_duration_min": 950,
                    "airlines": ["Air Canada", "ANA"],
                    "chosen_layovers": [{"iata": "NRT", "duration_min": 80}],
                    "source": "travelpayouts",
                },
            },
            {
                "city": "Beijing",
                "iata": "PEK",
                "grid": [
                    [
                        {
                            "dep": "2026-12-12", "ret": "2027-01-04",
                            "cheapest_cad": 7800, "stops": 1,
                            "nonstop_cad": None, "chosen": "cheapest",
                            "chosen_cad": 7800,
                            "source": "travelpayouts",
                            "book": "https://www.aviasales.com/link4",
                        },
                    ],
                ],
                "best": {
                    "dep": "2026-12-12", "ret": "2027-01-04",
                    "cheapest_cad": 7800, "chosen_cad": 7800,
                    "chosen": "cheapest", "stops": 1, "chosen_stops": 1,
                    "duration_min": 820, "chosen_duration_min": 820,
                    "source": "travelpayouts",
                },
            },
        ],
    }


def _make_empty_result():
    """run_search output with no priceable cells at all."""
    return {
        "origin": "YYZ",
        "adults": 2,
        "child_ages": [],
        "families": 1,
        "dep_dates": ["2026-12-12"],
        "ret_dates": ["2027-01-04"],
        "recommendation": "No priceable options found.",
        "providers": [],
        "results": [
            {
                "city": "Nowhere",
                "iata": "XXX",
                "grid": [
                    [
                        {
                            "dep": "2026-12-12", "ret": "2027-01-04",
                            "cheapest_cad": None, "stops": None,
                            "nonstop_cad": None, "chosen": "cheapest",
                            "chosen_cad": None,
                            "source": "no-data",
                            "book": "https://www.kayak.com/fallback",
                        }
                    ]
                ],
                "best": None,
            }
        ],
    }


# ---------------------------------------------------------------------------
# render_csv tests
# ---------------------------------------------------------------------------

class TestRenderCsv:
    def test_imports_without_error(self):
        import export  # noqa: F401

    def test_returns_string(self):
        from export import render_csv
        out = render_csv(_make_result())
        assert isinstance(out, str)

    def test_header_row_exact_columns(self):
        from export import render_csv
        out = render_csv(_make_result())
        reader = csv.reader(io.StringIO(out))
        header = next(reader)
        assert header == [
            "city", "iata", "dep_date", "ret_date",
            "cheapest_cad", "stops", "duration_min",
            "nonstop_cad", "nonstop_duration_min",
            "chosen", "chosen_cad", "chosen_stops", "chosen_duration_min",
            "airlines", "layovers", "source", "book",
        ]

    def test_data_rows_count(self):
        """2 cities × 2×2 grid + 1 city × 1×1 grid = 4 + 1 = 5 data rows."""
        from export import render_csv
        out = render_csv(_make_result())
        rows = list(csv.reader(io.StringIO(out)))
        # 1 header + 5 data rows
        assert len(rows) == 6

    def test_data_row_field_mapping(self):
        from export import render_csv
        out = render_csv(_make_result())
        rows = list(csv.reader(io.StringIO(out)))
        # First data row: Shanghai, dep 2026-12-12, ret 2027-01-04
        row = rows[1]
        assert row[0] == "Shanghai"   # city
        assert row[1] == "PVG"        # iata
        assert row[2] == "2026-12-12" # dep_date
        assert row[3] == "2027-01-04" # ret_date
        assert row[4] == "8000"       # cheapest_cad
        assert row[5] == "1"          # stops
        assert row[6] == "875"        # duration_min (cheapest itinerary)
        assert row[7] == "8500"       # nonstop_cad
        assert row[8] == "600"        # nonstop_duration_min (nonstop itinerary)
        assert row[9] == "cheapest"   # chosen
        assert row[10] == "8000"      # chosen_cad
        assert row[11] == "1"         # chosen_stops (== stops, cheapest chosen)
        assert row[12] == "875"       # chosen_duration_min (== duration_min here)
        assert row[13] == "Air Canada, ANA"  # airlines (joined)
        assert row[14] == "NRT 1h20m"        # layovers (compact)
        assert row[15] == "travelpayouts" # source
        assert row[16] == "https://www.aviasales.com/link1"  # book

    def test_no_data_cell_renders_empty_strings_not_crash(self):
        """A cell with cheapest_cad=None must render empty strings, never crash."""
        from export import render_csv
        out = render_csv(_make_result())
        rows = list(csv.reader(io.StringIO(out)))
        # Second data row: Shanghai, dep 2026-12-12, ret 2027-01-11 (no-data)
        row = rows[2]
        assert row[0] == "Shanghai"
        assert row[2] == "2026-12-12"
        assert row[3] == "2027-01-11"
        assert row[4] == ""    # cheapest_cad is None → empty
        assert row[6] == ""    # duration_min is None → empty
        assert row[8] == ""    # nonstop_duration_min is None → empty
        assert row[10] == ""   # chosen_cad is None → empty
        assert row[11] == ""   # chosen_stops is None → empty
        assert row[12] == ""   # chosen_duration_min is None → empty
        assert row[13] == ""   # airlines absent → empty
        assert row[14] == ""   # layovers absent → empty

    def test_airlines_layovers_helpers(self):
        from export import _fmt_airlines, _fmt_layovers, _fmt_layover_dur
        assert _fmt_airlines(None) == ""
        assert _fmt_airlines([]) == ""
        assert _fmt_airlines(["AC", "NH"]) == "AC, NH"
        assert _fmt_layovers(None) == ""
        assert _fmt_layovers([]) == ""
        assert _fmt_layovers(
            [{"iata": "PEK", "duration_min": 80},
             {"iata": "NRT", "duration_min": None},
             {"iata": None, "duration_min": 30}]) == "PEK 1h20m, NRT, ? 0h30m"
        assert _fmt_layover_dur(None) == ""
        assert _fmt_layover_dur("bad") == ""
        assert _fmt_layover_dur(80) == "1h20m"

    def test_nonstop_chosen_cell(self):
        from export import render_csv
        out = render_csv(_make_result())
        rows = list(csv.reader(io.StringIO(out)))
        # Third data row: Shanghai, dep 2026-12-19, ret 2027-01-04 (nonstop)
        row = rows[3]
        assert row[9] == "nonstop"
        assert row[10] == "9000"     # chosen_cad
        assert row[5] == "0"         # stops
        assert row[7] == "9000"      # nonstop_cad
        assert row[8] == "700"       # nonstop_duration_min
        assert row[11] == "0"        # chosen_stops (0 for nonstop)
        assert row[12] == "700"      # chosen_duration_min (== nonstop here)

    def test_empty_results_still_produces_header(self):
        from export import render_csv
        out = render_csv(_make_empty_result())
        rows = list(csv.reader(io.StringIO(out)))
        assert len(rows) >= 1
        assert rows[0][0] == "city"

    def test_empty_results_no_data_cell_does_not_crash(self):
        """An all-empty result dict must not crash render_csv."""
        from export import render_csv
        out = render_csv(_make_empty_result())
        assert isinstance(out, str)

    def test_fully_empty_result_dict(self):
        """Passing {} must not raise — renders only the header."""
        from export import render_csv
        out = render_csv({})
        rows = list(csv.reader(io.StringIO(out)))
        assert rows[0][0] == "city"  # still has header


# ---------------------------------------------------------------------------
# render_pdf tests
# ---------------------------------------------------------------------------

class TestRenderPdf:
    def test_returns_bytes(self):
        from export import render_pdf
        out = render_pdf(_make_result())
        assert isinstance(out, bytes)

    def test_fmt_dur_helper(self):
        from export import _fmt_dur
        assert _fmt_dur(875) == "14h 35m"
        assert _fmt_dur(0) == "0h 0m"
        assert _fmt_dur(None) == ""
        # non-numeric / unparseable → "" (never crashes)
        assert _fmt_dur("nope") == ""
        assert _fmt_dur(object()) == ""

    def test_starts_with_pdf_magic(self):
        from export import render_pdf
        out = render_pdf(_make_result())
        assert out[:4] == b"%PDF"

    def test_empty_results_does_not_crash(self):
        from export import render_pdf
        out = render_pdf(_make_empty_result())
        assert isinstance(out, bytes)
        assert out[:4] == b"%PDF"

    def test_fully_empty_dict_does_not_crash(self):
        from export import render_pdf
        out = render_pdf({})
        assert isinstance(out, bytes)
        assert out[:4] == b"%PDF"

    def test_pdf_size_is_non_trivial(self):
        """PDF should be more than a 100-byte stub."""
        from export import render_pdf
        out = render_pdf(_make_result())
        assert len(out) > 100

    def test_city_with_empty_grid_does_not_crash(self):
        """A city result with grid=[] must not crash render_pdf."""
        from export import render_pdf
        result = {
            "origin": "YYZ",
            "adults": 2,
            "child_ages": [],
            "families": 1,
            "dep_dates": [],
            "ret_dates": [],
            "recommendation": "Test",
            "providers": [],
            "results": [
                {
                    "city": "Nowhere",
                    "iata": "XXX",
                    "grid": [],   # empty grid — exercises the `if not grid` branch
                    "best": None,
                }
            ],
        }
        out = render_pdf(result)
        assert isinstance(out, bytes)
        assert out[:4] == b"%PDF"

    def test_pdf_includes_duration_text(self):
        """The PDF must render total flight duration (e.g. '14h 35m') somewhere.

        fpdf2 deflates content streams, so decompress them and assert the
        human-readable duration appears (the Shanghai cell has duration_min=875).
        """
        import re
        import zlib
        from export import render_pdf
        out = render_pdf(_make_result())
        blob = b""
        for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", out, re.S):
            try:
                blob += zlib.decompress(m.group(1))
            except Exception:
                pass
        assert b"14h 35m" in blob

    def test_pdf_best_summary_uses_chosen_stops(self):
        """The 'Best:' summary must pair the CHOSEN stop count with its duration —
        a nonstop best renders '(nonstop, ...)' / '(0 stops...)', never the
        cheapest itinerary's stop count (codex P2: no 'nonstop (1 stop,...)')."""
        import re
        import zlib
        from export import render_pdf
        result = _make_result()
        # Make the first city's best a nonstop whose cheapest line had 2 stops.
        result["results"][0]["best"] = {
            "dep": "2026-12-19", "ret": "2027-01-04",
            "cheapest_cad": 9000, "chosen_cad": 9000,
            "chosen": "nonstop", "stops": 2, "chosen_stops": 0,
            "duration_min": 875, "chosen_duration_min": 700,
            "source": "travelpayouts",
        }
        out = render_pdf(result)
        blob = b""
        for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", out, re.S):
            try:
                blob += zlib.decompress(m.group(1))
            except Exception:
                pass
        # PDF escapes '(' as '\\('; the summary pairs the chosen 'nonstop' label
        # (chosen_stops==0) with the nonstop's 11h 40m duration.
        assert rb"nonstop \(nonstop, 11h 40m\)" in blob
        assert b"2 stop" not in blob         # the cheapest fare's count must not leak

    def test_pdf_best_summary_includes_airlines_and_layovers(self):
        """The 'Best:' summary must include the airline(s) and a 'via ...' layover
        summary when the best cell carries them (Shanghai best has Air Canada/ANA
        and a NRT 80-min connection)."""
        import re
        import zlib
        from export import render_pdf
        out = render_pdf(_make_result())
        blob = b""
        for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", out, re.S):
            try:
                blob += zlib.decompress(m.group(1))
            except Exception:
                pass
        assert b"Air Canada, ANA" in blob
        assert b"via NRT 1h20m" in blob

    def test_non_latin1_city_does_not_crash(self):
        """Non-Latin-1 city names (e.g. 'Łódź', '東京') must not raise.

        fpdf2's built-in Helvetica is Latin-1 only; all PDF text is sanitized
        so unsupported characters become '?' rather than crashing.
        """
        from export import render_pdf
        result = _make_result()
        result["results"][0]["city"] = "Łódź"
        result["results"][1]["city"] = "東京"
        out = render_pdf(result)
        assert isinstance(out, bytes)
        assert out[:4] == b"%PDF"
