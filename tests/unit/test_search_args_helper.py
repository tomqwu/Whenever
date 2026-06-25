"""Tests for the _search_args_from_body helper extracted from api_search.

Ensures the refactor does not change api_search's behavior: same 400s, same
date parsing (issue #9), same threshold handling. All existing route tests
(test_routes.py) must continue to pass unchanged — this file adds targeted
coverage for the helper itself.
"""
import app as appmod


# ---------------------------------------------------------------------------
# _search_args_from_body helper tests
# ---------------------------------------------------------------------------

class TestSearchArgsFromBody:
    """Tests for appmod._search_args_from_body(body) -> dict | None."""

    def test_valid_body_returns_dict(self):
        body = {
            "origin": "YYZ",
            "destinations": [{"city": "Shanghai", "iata": "PVG"}],
            "dep_dates": ["2026-12-12"],
            "ret_dates": ["2027-01-04"],
        }
        result = appmod._search_args_from_body(body)
        assert result is not None
        assert isinstance(result, dict)

    def test_missing_origin_returns_none(self):
        body = {
            "destinations": [{"city": "Shanghai", "iata": "PVG"}],
            "dep_dates": ["2026-12-12"],
            "ret_dates": ["2027-01-04"],
        }
        assert appmod._search_args_from_body(body) is None

    def test_empty_origin_returns_none(self):
        body = {
            "origin": "",
            "destinations": [{"city": "Shanghai", "iata": "PVG"}],
            "dep_dates": ["2026-12-12"],
            "ret_dates": ["2027-01-04"],
        }
        assert appmod._search_args_from_body(body) is None

    def test_missing_destinations_returns_none(self):
        body = {
            "origin": "YYZ",
            "dep_dates": ["2026-12-12"],
            "ret_dates": ["2027-01-04"],
        }
        assert appmod._search_args_from_body(body) is None

    def test_empty_destinations_returns_none(self):
        body = {
            "origin": "YYZ",
            "destinations": [],
            "dep_dates": ["2026-12-12"],
            "ret_dates": ["2027-01-04"],
        }
        assert appmod._search_args_from_body(body) is None

    def test_missing_dates_returns_none(self):
        """Issue #9: no dep_dates and no dep_start must return None (not crash)."""
        body = {
            "origin": "YYZ",
            "destinations": [{"city": "Shanghai", "iata": "PVG"}],
        }
        assert appmod._search_args_from_body(body) is None

    def test_non_string_dep_start_returns_none(self):
        """Issue #9: integer dep_start must return None, not raise TypeError."""
        body = {
            "origin": "YYZ",
            "destinations": [{"city": "Shanghai", "iata": "PVG"}],
            "dep_start": 123, "ret_start": 456,
        }
        assert appmod._search_args_from_body(body) is None

    def test_malformed_dates_returns_none(self):
        """Issue #9: bad date strings must return None."""
        body = {
            "origin": "YYZ",
            "destinations": [{"city": "Shanghai", "iata": "PVG"}],
            "dep_start": "not-a-date", "ret_start": "also-bad",
        }
        assert appmod._search_args_from_body(body) is None

    def test_valid_body_returns_correct_kwargs(self):
        """The returned dict must be keyword-argument-ready for run_search."""
        body = {
            "origin": "YYZ",
            "destinations": [{"city": "Shanghai", "iata": "PVG"}],
            "dep_dates": ["2026-12-12"],
            "ret_dates": ["2027-01-04"],
            "adults": 3,
            "child_ages": [5, 8],
            "nonstop_threshold": 30.0,
            "families": 2,
        }
        result = appmod._search_args_from_body(body)
        assert result is not None
        assert result["origin"] == "YYZ"
        assert result["dests"] == [{"city": "Shanghai", "iata": "PVG"}]
        assert result["adults"] == 3
        assert result["child_ages"] == [5, 8]
        assert result["dep_dates"] == ["2026-12-12"]
        assert result["ret_dates"] == ["2027-01-04"]
        assert result["threshold_pct"] == 30.0
        assert result["families"] == 2

    def test_threshold_parsed_as_float(self):
        """nonstop_threshold must be float (not int-truncated)."""
        body = {
            "origin": "YYZ",
            "destinations": [{"city": "X", "iata": "XXX"}],
            "dep_dates": ["2026-12-12"],
            "ret_dates": ["2027-01-04"],
            "nonstop_threshold": "25.5",
        }
        result = appmod._search_args_from_body(body)
        assert result is not None
        assert result["threshold_pct"] == 25.5

    def test_dep_span_generates_dates(self):
        """dep_start + dep_span should produce a list of dates."""
        body = {
            "origin": "YYZ",
            "destinations": [{"city": "X", "iata": "XXX"}],
            "dep_start": "2026-12-12", "dep_span": 3,
            "ret_start": "2027-01-04", "ret_span": 2,
        }
        result = appmod._search_args_from_body(body)
        assert result is not None
        assert result["dep_dates"] == ["2026-12-12", "2026-12-13", "2026-12-14"]
        assert result["ret_dates"] == ["2027-01-04", "2027-01-05"]

    def test_huge_span_clamped_before_expansion(self):
        """A malformed huge dep_span must NOT allocate millions of dates.

        Codex P2: the date arrays were materialized before the cell cap ran, so a
        span like 10_000_000 could exhaust the worker. The span is now clamped to
        MAX_DATE_SPAN before date_range expands it.
        """
        body = {
            "origin": "YYZ",
            "destinations": [{"city": "X", "iata": "XXX"}],
            "dep_start": "2026-12-12", "dep_span": 10_000_000,
            "ret_start": "2027-01-04", "ret_span": 2,
        }
        result = appmod._search_args_from_body(body)
        assert result is not None
        assert len(result["dep_dates"]) <= appmod.MAX_DATE_SPAN
        assert len(result["ret_dates"]) == 2

    def test_negative_span_floors_to_one(self):
        """A negative span floors to 1 (still a valid single-date search), never
        a negative count that would yield an empty/None list."""
        body = {
            "origin": "YYZ",
            "destinations": [{"city": "X", "iata": "XXX"}],
            "dep_start": "2026-12-12", "dep_span": -7,
            "ret_start": "2027-01-04", "ret_span": -1,
        }
        result = appmod._search_args_from_body(body)
        assert result is not None
        assert result["dep_dates"] == ["2026-12-12"]
        assert result["ret_dates"] == ["2027-01-04"]

    def test_zero_span_falls_back_to_default(self):
        """dep_span=0 is falsy, so the `or 4` guard restores the default span."""
        body = {
            "origin": "YYZ",
            "destinations": [{"city": "X", "iata": "XXX"}],
            "dep_start": "2026-12-12", "dep_span": 0,
            "ret_dates": ["2027-01-04"],
        }
        result = appmod._search_args_from_body(body)
        assert result is not None
        assert len(result["dep_dates"]) == 4

    def test_explicit_dates_ignore_garbage_span(self):
        """Codex review: explicit dep_dates/ret_dates plus a non-numeric stale
        span (e.g. dep_span='abc') must NOT raise. The span is only consulted on
        the fallback path; with explicit arrays it is never parsed."""
        body = {
            "origin": "YYZ",
            "destinations": [{"city": "X", "iata": "XXX"}],
            "dep_dates": ["2026-12-12"],
            "ret_dates": ["2027-01-04"],
            "dep_span": "abc", "ret_span": "xyz",
        }
        result = appmod._search_args_from_body(body)
        assert result is not None
        assert result["dep_dates"] == ["2026-12-12"]
        assert result["ret_dates"] == ["2027-01-04"]

    def test_fallback_huge_span_clamped(self):
        """Fallback path: a huge numeric span is still clamped to MAX_DATE_SPAN."""
        body = {
            "origin": "YYZ",
            "destinations": [{"city": "X", "iata": "XXX"}],
            "dep_start": "2026-12-12", "dep_span": 10_000_000,
            "ret_start": "2027-01-04", "ret_span": 2,
        }
        result = appmod._search_args_from_body(body)
        assert result is not None
        assert len(result["dep_dates"]) == appmod.MAX_DATE_SPAN
        assert len(result["ret_dates"]) == 2

    def test_fallback_garbage_span_degrades_no_crash(self):
        """Fallback path (no explicit dates): a garbage span degrades to the
        default span instead of raising a 500."""
        body = {
            "origin": "YYZ",
            "destinations": [{"city": "X", "iata": "XXX"}],
            "dep_start": "2026-12-12", "dep_span": "abc",
            "ret_start": "2027-01-04", "ret_span": None,
        }
        result = appmod._search_args_from_body(body)
        assert result is not None
        # default span (4) applied since the span could not be parsed
        assert len(result["dep_dates"]) == 4
        assert len(result["ret_dates"]) == 4

    def test_fallback_garbage_span_missing_start_returns_none(self):
        """Fallback path with a garbage span AND a missing start: degrades to a
        400 (None) via empty date_range, never a 500."""
        body = {
            "origin": "YYZ",
            "destinations": [{"city": "X", "iata": "XXX"}],
            "dep_span": "abc", "ret_span": "abc",
        }
        assert appmod._search_args_from_body(body) is None


# ---------------------------------------------------------------------------
# api_search still behaves identically after refactor
# ---------------------------------------------------------------------------

class TestApiSearchUnchangedAfterRefactor:
    """These mirror existing test_routes.py tests but live here to explicitly
    document that the _search_args_from_body refactor does not alter api_search."""

    def test_search_validation_still_400(self, client):
        r = client.post("/api/search", json={
            "origin": "", "destinations": [],
            "dep_dates": ["2026-12-12"], "ret_dates": ["2027-01-04"],
        })
        assert r.status_code == 400

    def test_search_missing_dates_still_400(self, client):
        r = client.post("/api/search", json={
            "origin": "YYZ",
            "destinations": [{"city": "X", "iata": "XXX"}],
        })
        assert r.status_code == 400

    def test_search_non_string_dates_still_400(self, client):
        r = client.post("/api/search", json={
            "origin": "YYZ",
            "destinations": [{"city": "X", "iata": "XXX"}],
            "dep_start": 123, "ret_start": 456,
        })
        assert r.status_code == 400

    def test_search_malformed_dates_still_400(self, client):
        r = client.post("/api/search", json={
            "origin": "YYZ",
            "destinations": [{"city": "X", "iata": "XXX"}],
            "dep_start": "not-a-date", "ret_start": "also-bad",
        })
        assert r.status_code == 400

    def test_huge_span_post_does_not_allocate_millions(self, client, monkeypatch):
        """Codex P2: POST with dep_span=10_000_000 must not build millions of dates.

        The span is clamped to MAX_DATE_SPAN before expansion. With 1 city ×
        MAX_DATE_SPAN(60) × ret_span(2) = 120 cells (< MAX_SEARCH_CELLS 200) the
        search runs, but the dep_dates list is bounded — never millions.
        """
        captured = {}

        def _spy_run_search(**kwargs):
            captured["dep_dates"] = kwargs["dep_dates"]
            captured["ret_dates"] = kwargs["ret_dates"]
            return {"origin": kwargs["origin"], "results": [], "recommendation": "",
                    "providers": {}, "dep_dates": kwargs["dep_dates"],
                    "ret_dates": kwargs["ret_dates"], "adults": kwargs["adults"],
                    "child_ages": kwargs["child_ages"], "families": kwargs["families"]}

        monkeypatch.setattr(appmod, "run_search", _spy_run_search)
        r = client.post("/api/search", json={
            "origin": "YYZ",
            "destinations": [{"city": "X", "iata": "XXX"}],
            "dep_start": "2026-12-12", "dep_span": 10_000_000,
            "ret_start": "2027-01-04", "ret_span": 2,
        })
        assert r.status_code == 200
        assert len(captured["dep_dates"]) <= appmod.MAX_DATE_SPAN
        assert len(captured["ret_dates"]) == 2

    def test_huge_span_post_hits_cell_cap_400_when_over_limit(self, client, monkeypatch):
        """A clamped span that still exceeds MAX_SEARCH_CELLS returns the cap 400,
        and run_search is never invoked (no millions of cells materialized)."""
        monkeypatch.setattr(appmod, "MAX_SEARCH_CELLS", 50)
        called = {"run": False}
        monkeypatch.setattr(appmod, "run_search",
                            lambda **k: called.__setitem__("run", True))
        r = client.post("/api/search", json={
            "origin": "YYZ",
            "destinations": [{"city": "X", "iata": "XXX"}],
            "dep_start": "2026-12-12", "dep_span": 10_000_000,
            "ret_start": "2027-01-04", "ret_span": 10_000_000,
        })
        assert r.status_code == 400
        assert "too large" in r.get_json()["error"]
        assert called["run"] is False

    def test_giant_explicit_array_still_hits_cell_cap(self, client, monkeypatch):
        """A giant explicit dep_dates array is still bounded by the cell cap 400
        (len checked before run_search) — unchanged by this fix."""
        monkeypatch.setattr(appmod, "MAX_SEARCH_CELLS", 50)
        called = {"run": False}
        monkeypatch.setattr(appmod, "run_search",
                            lambda **k: called.__setitem__("run", True))
        r = client.post("/api/search", json={
            "origin": "YYZ",
            "destinations": [{"city": "X", "iata": "XXX"}],
            "dep_dates": [f"2026-12-{d:02d}" for d in range(1, 29)],
            "ret_dates": ["2027-01-04", "2027-01-05", "2027-01-06"],
        })
        assert r.status_code == 400
        assert "too large" in r.get_json()["error"]
        assert called["run"] is False

    def test_search_success_still_works(self, client, monkeypatch):
        monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: {
            "cheapest_cad": 1000, "stops": 1, "nonstop_cad": 1100,
            "source": "test", "book": None,
        })
        monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "Best")
        r = client.post("/api/search", json={
            "origin": "YYZ",
            "destinations": [{"city": "Shanghai", "iata": "PVG"}],
            "dep_dates": ["2026-12-12"],
            "ret_dates": ["2027-01-04"],
        })
        assert r.status_code == 200
        data = r.get_json()
        assert data["origin"] == "YYZ"
        assert data["results"][0]["city"] == "Shanghai"
