"""Tests for cli.main() — the whenever CLI entry point."""
import sys
import pytest
import app as appmod
import cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_run_search(origin, dests, adults, child_ages, dep_dates, ret_dates,
                     threshold_pct=25, families=1):
    """Returns a minimal run_search result that cli.main() can render."""
    grid = [[{
        "dep": d, "ret": r,
        "cheapest_cad": 1000, "stops": 1,
        "nonstop_cad": 1100, "chosen": "nonstop", "chosen_cad": 1100,
        "source": "test", "book": "https://kayak.com/x",
    } for r in ret_dates] for d in dep_dates]
    results = []
    for dest in dests:
        flat = [c for row in grid for c in row]
        best = min(flat, key=lambda c: c["chosen_cad"]) if flat else None
        results.append({
            "city": dest["city"], "iata": dest["iata"],
            "grid": grid, "best": best,
        })
    return {
        "origin": origin, "adults": adults, "child_ages": child_ages,
        "families": families, "dep_dates": dep_dates, "ret_dates": ret_dates,
        "results": results, "recommendation": "Best pick: go to TestCity",
        "providers": [],
    }


# ---------------------------------------------------------------------------
# Happy path: --country expansion via top_cities
# ---------------------------------------------------------------------------
def test_cli_country_expansion(monkeypatch, capsys):
    monkeypatch.setattr(appmod, "resolve_airport", lambda city: "YYZ")
    monkeypatch.setattr(appmod, "top_cities",
                        lambda country, n: [{"city": "Shanghai", "iata": "PVG"},
                                            {"city": "Beijing", "iata": "PEK"}])
    monkeypatch.setattr(appmod, "run_search", _fake_run_search)

    rc = cli.main([
        "--from", "Toronto",
        "--country", "China",
        "--dep-start", "2026-12-12",
        "--ret-start", "2027-01-04",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Shanghai" in out
    assert "Best pick" in out


# ---------------------------------------------------------------------------
# Happy path: --city resolves a single destination
# ---------------------------------------------------------------------------
def test_cli_single_city(monkeypatch, capsys):
    monkeypatch.setattr(appmod, "resolve_airport", lambda city: "PVG" if "Shanghai" in city else "YYZ")
    monkeypatch.setattr(appmod, "run_search", _fake_run_search)

    rc = cli.main([
        "--from", "Toronto",
        "--city", "Shanghai",
        "--dep-start", "2026-12-12",
        "--ret-start", "2027-01-04",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Shanghai" in out


# ---------------------------------------------------------------------------
# IATA origin: 3-letter code bypasses resolve_airport
# ---------------------------------------------------------------------------
def test_cli_iata_origin_skips_resolve(monkeypatch, capsys):
    resolve_calls = []

    def spy_resolve(city):
        resolve_calls.append(city)
        return "PVG"

    monkeypatch.setattr(appmod, "resolve_airport", spy_resolve)
    monkeypatch.setattr(appmod, "run_search", _fake_run_search)
    monkeypatch.setattr(appmod, "top_cities",
                        lambda c, n: [{"city": "TestCity", "iata": "TST"}])

    rc = cli.main([
        "--from", "YYZ",
        "--country", "Testland",
        "--dep-start", "2026-12-12",
        "--ret-start", "2027-01-04",
    ])
    assert rc == 0
    # resolve_airport should NOT be called for the origin since "YYZ" is already IATA
    origin_resolves = [c for c in resolve_calls if c != "TestCity" and c != "Testland"]
    assert origin_resolves == []


# ---------------------------------------------------------------------------
# Error: --from cannot be resolved → non-zero exit + stderr message
# ---------------------------------------------------------------------------
def test_cli_unresolvable_origin_errors(monkeypatch, capsys):
    monkeypatch.setattr(appmod, "resolve_airport", lambda city: "")

    rc = cli.main([
        "--from", "Nowhere_____",
        "--city", "Shanghai",
        "--dep-start", "2026-12-12",
        "--ret-start", "2027-01-04",
    ])
    err = capsys.readouterr().err
    assert rc != 0
    assert "origin" in err.lower() or "resolve" in err.lower()


# ---------------------------------------------------------------------------
# Error: no destinations (top_cities returns empty) → non-zero + stderr
# ---------------------------------------------------------------------------
def test_cli_no_destinations_errors(monkeypatch, capsys):
    monkeypatch.setattr(appmod, "resolve_airport", lambda city: "YYZ")
    monkeypatch.setattr(appmod, "top_cities", lambda c, n: [])

    rc = cli.main([
        "--from", "Toronto",
        "--country", "EmptyLand",
        "--dep-start", "2026-12-12",
        "--ret-start", "2027-01-04",
    ])
    err = capsys.readouterr().err
    assert rc != 0
    assert "destination" in err.lower() or "no cities" in err.lower()


# ---------------------------------------------------------------------------
# Error: missing --dep-start → non-zero
# ---------------------------------------------------------------------------
def test_cli_missing_dep_start_errors(monkeypatch, capsys):
    monkeypatch.setattr(appmod, "resolve_airport", lambda city: "YYZ")
    with pytest.raises(SystemExit) as exc:
        cli.main([
            "--from", "Toronto",
            "--city", "Shanghai",
            "--ret-start", "2027-01-04",
            # --dep-start intentionally omitted
        ])
    assert exc.value.code != 0


# ---------------------------------------------------------------------------
# Error: missing --ret-start → non-zero
# ---------------------------------------------------------------------------
def test_cli_missing_ret_start_errors(monkeypatch, capsys):
    monkeypatch.setattr(appmod, "resolve_airport", lambda city: "YYZ")
    with pytest.raises(SystemExit) as exc:
        cli.main([
            "--from", "Toronto",
            "--city", "Shanghai",
            "--dep-start", "2026-12-12",
            # --ret-start intentionally omitted
        ])
    assert exc.value.code != 0


# ---------------------------------------------------------------------------
# --child args are parsed and passed correctly
# ---------------------------------------------------------------------------
def test_cli_child_ages_forwarded(monkeypatch, capsys):
    received = {}

    def capture_search(origin, dests, adults, child_ages, dep_dates, ret_dates,
                       threshold_pct=25, families=1):
        received.update({"child_ages": child_ages, "adults": adults})
        return _fake_run_search(origin, dests, adults, child_ages, dep_dates, ret_dates)

    monkeypatch.setattr(appmod, "resolve_airport", lambda city: "YYZ" if "Toronto" in city else "PVG")
    monkeypatch.setattr(appmod, "run_search", capture_search)

    cli.main([
        "--from", "Toronto",
        "--city", "Shanghai",
        "--adults", "2",
        "--child", "11", "--child", "9",
        "--dep-start", "2026-12-12",
        "--ret-start", "2027-01-04",
    ])
    assert received["child_ages"] == [11, 9]
    assert received["adults"] == 2


# ---------------------------------------------------------------------------
# --max-cities limits the number of cities fetched
# ---------------------------------------------------------------------------
def test_cli_max_cities_respected(monkeypatch, capsys):
    fetched = {}

    def spy_top_cities(country, n):
        fetched["n"] = n
        return [{"city": f"City{i}", "iata": f"C{i:02d}"} for i in range(n)]

    monkeypatch.setattr(appmod, "resolve_airport", lambda city: "YYZ")
    monkeypatch.setattr(appmod, "top_cities", spy_top_cities)
    monkeypatch.setattr(appmod, "run_search", _fake_run_search)

    cli.main([
        "--from", "Toronto",
        "--country", "China",
        "--max-cities", "3",
        "--dep-start", "2026-12-12",
        "--ret-start", "2027-01-04",
    ])
    assert fetched["n"] == 3


# ---------------------------------------------------------------------------
# Integration / e2e: full cli.main() with no real network calls renders matrix
# ---------------------------------------------------------------------------
def test_cli_renders_full_matrix(monkeypatch, capsys):
    """End-to-end: cli.main() renders a dep×ret matrix with chosen prices."""
    monkeypatch.setattr(appmod, "resolve_airport", lambda city: "YYZ")
    monkeypatch.setattr(appmod, "top_cities",
                        lambda c, n: [{"city": "TestCity", "iata": "TST"}])
    monkeypatch.setattr(appmod, "run_search", _fake_run_search)

    rc = cli.main([
        "--from", "Toronto",
        "--country", "Testland",
        "--dep-start", "2026-12-12", "--dep-span", "2",
        "--ret-start", "2027-01-04", "--ret-span", "2",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    # Matrix header row includes return dates
    assert "2027-01-04" in out
    # Departure dates appear as row headers
    assert "2026-12-12" in out
    # A chosen price appears
    assert "1100" in out
    # Recommendation appears
    assert "Best pick" in out
    # City name appears
    assert "TestCity" in out


# ---------------------------------------------------------------------------
# Matrix renders "no-data" for cells with no price (line 71 coverage)
# ---------------------------------------------------------------------------
def test_cli_renders_no_data_cell(monkeypatch, capsys):
    def no_data_search(origin, dests, adults, child_ages, dep_dates, ret_dates,
                       threshold_pct=25, families=1):
        grid = [[{
            "dep": d, "ret": r,
            "cheapest_cad": None, "stops": None,
            "nonstop_cad": None, "chosen": "cheapest", "chosen_cad": None,
            "source": "no-data", "book": "https://kayak.com/x",
        } for r in ret_dates] for d in dep_dates]
        return {
            "origin": origin, "adults": adults, "child_ages": child_ages,
            "families": families, "dep_dates": dep_dates, "ret_dates": ret_dates,
            "results": [{"city": "NoWhere", "iata": "XXX", "grid": grid, "best": None}],
            "recommendation": "No prices found.",
            "providers": [],
        }

    monkeypatch.setattr(appmod, "resolve_airport", lambda city: "YYZ")
    monkeypatch.setattr(appmod, "run_search", no_data_search)

    rc = cli.main([
        "--from", "Toronto",
        "--city", "NoWhere",
        "--dep-start", "2026-12-12",
        "--ret-start", "2027-01-04",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no-data" in out
    assert "no priceable cells found" in out


# ---------------------------------------------------------------------------
# --city with a 3-letter IATA code is used directly (lines 113-114)
# ---------------------------------------------------------------------------
def test_cli_city_as_iata_code(monkeypatch, capsys):
    monkeypatch.setattr(appmod, "resolve_airport", lambda city: "YYZ")
    monkeypatch.setattr(appmod, "run_search", _fake_run_search)

    rc = cli.main([
        "--from", "Toronto",
        "--city", "PVG",
        "--dep-start", "2026-12-12",
        "--ret-start", "2027-01-04",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PVG" in out


# ---------------------------------------------------------------------------
# Error: --city resolves to empty IATA → non-zero exit (lines 118-120)
# ---------------------------------------------------------------------------
def test_cli_unresolvable_city_errors(monkeypatch, capsys):
    monkeypatch.setattr(appmod, "resolve_airport", lambda city: "")

    rc = cli.main([
        "--from", "YYZ",
        "--city", "NowhereCity",
        "--dep-start", "2026-12-12",
        "--ret-start", "2027-01-04",
    ])
    err = capsys.readouterr().err
    assert rc != 0
    assert "destination" in err.lower() or "resolve" in err.lower()


# ---------------------------------------------------------------------------
# --nonstop-threshold is forwarded to run_search
# ---------------------------------------------------------------------------
def test_cli_nonstop_threshold_forwarded(monkeypatch, capsys):
    received = {}

    def capture(origin, dests, adults, child_ages, dep_dates, ret_dates,
                threshold_pct=25, families=1):
        received["threshold_pct"] = threshold_pct
        return _fake_run_search(origin, dests, adults, child_ages, dep_dates, ret_dates)

    monkeypatch.setattr(appmod, "resolve_airport", lambda city: "YYZ")
    monkeypatch.setattr(appmod, "run_search", capture)

    cli.main([
        "--from", "Toronto",
        "--city", "Shanghai",
        "--dep-start", "2026-12-12",
        "--ret-start", "2027-01-04",
        "--nonstop-threshold", "15",
    ])
    assert received["threshold_pct"] == 15
