"""Tests for streaming search: _build_cell helper and /api/search/stream endpoint."""
import json
import app as appmod


# ---------------------------------------------------------------------------
# _build_cell helper
# ---------------------------------------------------------------------------

def test_build_cell_picks_nonstop_within_threshold():
    fare = {"cheapest_cad": 1000, "stops": 1, "nonstop_cad": 1100, "source": "test", "book": None}
    cell = appmod._build_cell("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, [11], fare, 0.25)
    assert cell["chosen"] == "nonstop"          # 1100 <= 1000 * 1.25
    assert cell["chosen_cad"] == 1100
    assert cell["dep"] == "2026-12-12"
    assert cell["ret"] == "2027-01-04"
    assert cell["cheapest_cad"] == 1000
    assert cell["nonstop_cad"] == 1100
    assert cell["source"] == "test"
    # book falls back to kayak because fare["book"] is None
    assert cell["book"].startswith("https://www.kayak.com")


def test_build_cell_picks_cheapest_when_nonstop_too_pricey():
    fare = {"cheapest_cad": 1000, "stops": 1, "nonstop_cad": 2000, "source": "test", "book": "https://b"}
    cell = appmod._build_cell("YYZ", "XXX", "2026-12-12", "2027-01-04", 2, [], fare, 0.10)
    assert cell["chosen"] == "cheapest"         # 2000 > 1000 * 1.10
    assert cell["chosen_cad"] == 1000
    assert cell["book"] == "https://b"          # provider link kept


def test_build_cell_no_data():
    fare = {"cheapest_cad": None, "stops": None, "nonstop_cad": None, "source": "no-data"}
    cell = appmod._build_cell("YYZ", "XXX", "2026-12-12", "2027-01-04", 1, [], fare, 0.25)
    assert cell["cheapest_cad"] is None
    assert cell["chosen_cad"] is None
    assert cell["source"] == "no-data"
    # kayak fallback link must still be present
    assert cell["book"].startswith("https://www.kayak.com")
