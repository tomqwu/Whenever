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
