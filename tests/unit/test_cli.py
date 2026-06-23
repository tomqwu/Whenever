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


# ---------------------------------------------------------------------------
# Fix 2: 3-letter mixed-case city (e.g. "Hue") must go through resolve_airport
# ---------------------------------------------------------------------------

def test_cli_title_case_3letter_city_resolves(monkeypatch, capsys):
    """'Hue' is 3 letters but not all-uppercase, so it must be resolved, not used as-is."""
    resolve_calls = []

    def spy_resolve(city):
        resolve_calls.append(city)
        return "HUI"

    monkeypatch.setattr(appmod, "resolve_airport", spy_resolve)
    monkeypatch.setattr(appmod, "run_search", _fake_run_search)

    rc = cli.main([
        "--from", "YYZ",
        "--city", "Hue",
        "--dep-start", "2026-12-12",
        "--ret-start", "2027-01-04",
    ])
    assert rc == 0
    # resolve_airport must have been called for the destination "Hue"
    assert "Hue" in resolve_calls, (
        "3-letter mixed-case city 'Hue' must be resolved via resolve_airport, "
        "not used directly as an IATA code."
    )


def test_cli_uppercase_iata_city_skips_resolve(monkeypatch, capsys):
    """'YYZ' passed as --city must be used directly without calling resolve_airport."""
    resolve_calls = []

    def spy_resolve(city):
        resolve_calls.append(city)
        return "PVG"

    monkeypatch.setattr(appmod, "resolve_airport", spy_resolve)
    monkeypatch.setattr(appmod, "run_search", _fake_run_search)

    rc = cli.main([
        "--from", "YYZ",      # uppercase IATA → passthrough (no resolve call)
        "--city", "YVR",      # uppercase IATA → passthrough (no resolve call)
        "--dep-start", "2026-12-12",
        "--ret-start", "2027-01-04",
    ])
    assert rc == 0
    # resolve_airport must NOT have been called for the IATA origin or IATA destination
    assert resolve_calls == [], (
        "All-uppercase 3-letter codes ('YYZ', 'YVR') must bypass resolve_airport entirely."
    )


# ---------------------------------------------------------------------------
# Fix 2: lowercase 3-letter input for --from must also go through resolve
# ---------------------------------------------------------------------------
def test_cli_lowercase_3letter_origin_resolves(monkeypatch, capsys):
    """'yyz' is 3 letters but lowercase, so it must be resolved, not passed straight through."""
    resolve_calls = []

    def spy_resolve(city):
        resolve_calls.append(city)
        return "YYZ"

    monkeypatch.setattr(appmod, "resolve_airport", spy_resolve)
    monkeypatch.setattr(appmod, "run_search", _fake_run_search)
    monkeypatch.setattr(appmod, "top_cities",
                        lambda c, n: [{"city": "TestCity", "iata": "TST"}])

    rc = cli.main([
        "--from", "yyz",
        "--country", "Canada",
        "--dep-start", "2026-12-12",
        "--ret-start", "2027-01-04",
    ])
    assert rc == 0
    assert "yyz" in resolve_calls, (
        "Lowercase 'yyz' must be resolved via resolve_airport, not used as a raw IATA code."
    )


# ---------------------------------------------------------------------------
# Fix 3: malformed dep_start → non-zero exit, stderr message, no run_search call
# ---------------------------------------------------------------------------

def test_cli_malformed_dep_start_errors(monkeypatch, capsys):
    """A malformed --dep-start must print an error to stderr and exit non-zero
    WITHOUT calling run_search (matching the web API's 400 behavior)."""
    run_search_calls = []

    def spy_run_search(*a, **k):
        run_search_calls.append(1)
        return _fake_run_search(*a, **k)

    monkeypatch.setattr(appmod, "resolve_airport", lambda city: "YYZ")
    monkeypatch.setattr(appmod, "run_search", spy_run_search)

    rc = cli.main([
        "--from", "YYZ",
        "--city", "PVG",
        "--dep-start", "not-a-date",
        "--ret-start", "2027-01-04",
    ])
    err = capsys.readouterr().err
    assert rc != 0, "Malformed dep_start must exit non-zero"
    assert err.strip(), "A message must be printed to stderr"
    assert run_search_calls == [], "run_search must NOT be called when dep_dates is empty"


def test_cli_zero_dep_span_errors(monkeypatch, capsys):
    """--dep-span 0 produces an empty dep_dates; must exit non-zero without calling run_search."""
    run_search_calls = []

    def spy_run_search(*a, **k):
        run_search_calls.append(1)
        return _fake_run_search(*a, **k)

    monkeypatch.setattr(appmod, "resolve_airport", lambda city: "YYZ")
    monkeypatch.setattr(appmod, "run_search", spy_run_search)

    rc = cli.main([
        "--from", "YYZ",
        "--city", "PVG",
        "--dep-start", "2026-12-12",
        "--dep-span", "0",
        "--ret-start", "2027-01-04",
    ])
    err = capsys.readouterr().err
    assert rc != 0, "--dep-span 0 (empty window) must exit non-zero"
    assert err.strip(), "A message must be printed to stderr"
    assert run_search_calls == [], "run_search must NOT be called when dep_dates is empty"


def test_cli_malformed_ret_start_errors(monkeypatch, capsys):
    """A malformed --ret-start must print an error to stderr and exit non-zero."""
    run_search_calls = []

    def spy_run_search(*a, **k):
        run_search_calls.append(1)
        return _fake_run_search(*a, **k)

    monkeypatch.setattr(appmod, "resolve_airport", lambda city: "YYZ")
    monkeypatch.setattr(appmod, "run_search", spy_run_search)

    rc = cli.main([
        "--from", "YYZ",
        "--city", "PVG",
        "--dep-start", "2026-12-12",
        "--ret-start", "bad-ret-date",
    ])
    err = capsys.readouterr().err
    assert rc != 0, "Malformed ret_start must exit non-zero"
    assert err.strip(), "A message must be printed to stderr"
    assert run_search_calls == [], "run_search must NOT be called when ret_dates is empty"


# ---------------------------------------------------------------------------
# --nonstop-threshold accepts fractional (float) values (API parity)
# ---------------------------------------------------------------------------
def test_cli_nonstop_threshold_fractional(monkeypatch, capsys):
    """--nonstop-threshold 25.5 must be accepted and forwarded as float 25.5."""
    received = {}

    def capture(origin, dests, adults, child_ages, dep_dates, ret_dates,
                threshold_pct=25, families=1):
        received["threshold_pct"] = threshold_pct
        return _fake_run_search(origin, dests, adults, child_ages, dep_dates, ret_dates)

    monkeypatch.setattr(appmod, "resolve_airport", lambda city: "YYZ")
    monkeypatch.setattr(appmod, "run_search", capture)

    rc = cli.main([
        "--from", "Toronto",
        "--city", "Shanghai",
        "--dep-start", "2026-12-12",
        "--ret-start", "2027-01-04",
        "--nonstop-threshold", "25.5",
    ])
    assert rc == 0, "--nonstop-threshold 25.5 should not be rejected by argparse"
    assert received["threshold_pct"] == 25.5
    assert isinstance(received["threshold_pct"], float)


def test_cli_nonstop_threshold_integer_still_works(monkeypatch, capsys):
    """--nonstop-threshold 25 (integer) must still be accepted and forwarded as 25.0."""
    received = {}

    def capture(origin, dests, adults, child_ages, dep_dates, ret_dates,
                threshold_pct=25, families=1):
        received["threshold_pct"] = threshold_pct
        return _fake_run_search(origin, dests, adults, child_ages, dep_dates, ret_dates)

    monkeypatch.setattr(appmod, "resolve_airport", lambda city: "YYZ")
    monkeypatch.setattr(appmod, "run_search", capture)

    rc = cli.main([
        "--from", "Toronto",
        "--city", "Shanghai",
        "--dep-start", "2026-12-12",
        "--ret-start", "2027-01-04",
        "--nonstop-threshold", "25",
    ])
    assert rc == 0, "--nonstop-threshold 25 should be accepted"
    assert received["threshold_pct"] == 25.0


# ---------------------------------------------------------------------------
# Fix: top_cities raises (e.g. Ollama down) → non-zero exit + stderr + no run_search
# ---------------------------------------------------------------------------
def test_cli_top_cities_raises_errors_gracefully(monkeypatch, capsys):
    """If top_cities raises, the CLI must print a concise error to stderr,
    return non-zero, and must NOT call run_search."""
    run_search_calls = []

    def exploding_top_cities(country, n):
        raise RuntimeError("Ollama connection refused")

    def spy_run_search(*a, **k):
        run_search_calls.append(1)
        return _fake_run_search(*a, **k)

    monkeypatch.setattr(appmod, "resolve_airport", lambda city: "YYZ")
    monkeypatch.setattr(appmod, "top_cities", exploding_top_cities)
    monkeypatch.setattr(appmod, "run_search", spy_run_search)

    rc = cli.main([
        "--from", "Toronto",
        "--country", "China",
        "--dep-start", "2026-12-12",
        "--ret-start", "2027-01-04",
    ])
    err = capsys.readouterr().err
    assert rc != 0, "top_cities failure must exit non-zero"
    assert "China" in err, "Error message must mention the country"
    assert "Ollama connection refused" in err, "Error message must include the exception text"
    assert run_search_calls == [], "run_search must NOT be called when top_cities raises"
