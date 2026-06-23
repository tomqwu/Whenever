"""E2E tests for the export endpoints.

Uses the live_server fixture (Werkzeug threaded server, no Playwright browser).
app.run_search is monkeypatched to return a fixture dict — no real API calls.
"""
import json
import urllib.request
import urllib.error
import pytest
import app as appmod


# ---------------------------------------------------------------------------
# Fixture result dict (same shape as run_search output)
# ---------------------------------------------------------------------------

FIXTURE_RESULT = {
    "origin": "YYZ",
    "adults": 2,
    "child_ages": [5],
    "families": 1,
    "dep_dates": ["2026-12-12"],
    "ret_dates": ["2027-01-04"],
    "recommendation": "Best value: Shanghai (PVG) at ~CA$8,000/family.",
    "providers": ["travelpayouts"],
    "results": [
        {
            "city": "Shanghai",
            "iata": "PVG",
            "grid": [
                [
                    {
                        "dep": "2026-12-12", "ret": "2027-01-04",
                        "cheapest_cad": 8000, "stops": 1,
                        "nonstop_cad": 8500, "chosen": "cheapest",
                        "chosen_cad": 8000,
                        "source": "travelpayouts",
                        "book": "https://www.aviasales.com/link1",
                    }
                ]
            ],
            "best": {
                "dep": "2026-12-12", "ret": "2027-01-04",
                "cheapest_cad": 8000, "chosen_cad": 8000,
                "chosen": "cheapest", "stops": 1,
                "source": "travelpayouts",
            },
        }
    ],
}

# Valid search body matching the fixture
VALID_BODY = {
    "origin": "YYZ",
    "destinations": [{"city": "Shanghai", "iata": "PVG"}],
    "adults": 2,
    "child_ages": [5],
    "dep_dates": ["2026-12-12"],
    "ret_dates": ["2027-01-04"],
}

# Body missing dates (triggers 400)
MISSING_DATES_BODY = {
    "origin": "YYZ",
    "destinations": [{"city": "Shanghai", "iata": "PVG"}],
}


def _post(url, body):
    """HTTP POST with JSON body; returns (status_code, headers_dict, response_bytes)."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, dict(e.headers), e.read()


# ---------------------------------------------------------------------------
# CSV export endpoint tests
# ---------------------------------------------------------------------------

class TestCsvExport:
    def test_csv_returns_200(self, live_server, monkeypatch):
        monkeypatch.setattr(appmod, "run_search", lambda *a, **k: FIXTURE_RESULT)
        status, headers, body = _post(f"{live_server}/api/export/csv", VALID_BODY)
        assert status == 200

    def test_csv_content_type(self, live_server, monkeypatch):
        monkeypatch.setattr(appmod, "run_search", lambda *a, **k: FIXTURE_RESULT)
        status, headers, body = _post(f"{live_server}/api/export/csv", VALID_BODY)
        ct = headers.get("Content-Type", "")
        assert ct.startswith("text/csv")

    def test_csv_content_disposition_attachment(self, live_server, monkeypatch):
        monkeypatch.setattr(appmod, "run_search", lambda *a, **k: FIXTURE_RESULT)
        status, headers, body = _post(f"{live_server}/api/export/csv", VALID_BODY)
        cd = headers.get("Content-Disposition", "")
        assert "attachment" in cd
        assert "whenever-matrix.csv" in cd

    def test_csv_body_contains_header_row(self, live_server, monkeypatch):
        monkeypatch.setattr(appmod, "run_search", lambda *a, **k: FIXTURE_RESULT)
        status, headers, body = _post(f"{live_server}/api/export/csv", VALID_BODY)
        text = body.decode("utf-8")
        assert "city" in text
        assert "iata" in text
        assert "dep_date" in text
        assert "cheapest_cad" in text

    def test_csv_body_contains_data(self, live_server, monkeypatch):
        monkeypatch.setattr(appmod, "run_search", lambda *a, **k: FIXTURE_RESULT)
        status, headers, body = _post(f"{live_server}/api/export/csv", VALID_BODY)
        text = body.decode("utf-8")
        assert "Shanghai" in text
        assert "PVG" in text

    def test_csv_missing_dates_returns_400(self, live_server, monkeypatch):
        monkeypatch.setattr(appmod, "run_search", lambda *a, **k: FIXTURE_RESULT)
        status, headers, body = _post(f"{live_server}/api/export/csv", MISSING_DATES_BODY)
        assert status == 400

    def test_csv_missing_origin_returns_400(self, live_server, monkeypatch):
        monkeypatch.setattr(appmod, "run_search", lambda *a, **k: FIXTURE_RESULT)
        bad_body = {**VALID_BODY, "origin": ""}
        status, headers, body = _post(f"{live_server}/api/export/csv", bad_body)
        assert status == 400


# ---------------------------------------------------------------------------
# PDF export endpoint tests
# ---------------------------------------------------------------------------

class TestPdfExport:
    def test_pdf_returns_200(self, live_server, monkeypatch):
        monkeypatch.setattr(appmod, "run_search", lambda *a, **k: FIXTURE_RESULT)
        status, headers, body = _post(f"{live_server}/api/export/pdf", VALID_BODY)
        assert status == 200

    def test_pdf_content_type(self, live_server, monkeypatch):
        monkeypatch.setattr(appmod, "run_search", lambda *a, **k: FIXTURE_RESULT)
        status, headers, body = _post(f"{live_server}/api/export/pdf", VALID_BODY)
        ct = headers.get("Content-Type", "")
        assert ct == "application/pdf"

    def test_pdf_content_disposition_attachment(self, live_server, monkeypatch):
        monkeypatch.setattr(appmod, "run_search", lambda *a, **k: FIXTURE_RESULT)
        status, headers, body = _post(f"{live_server}/api/export/pdf", VALID_BODY)
        cd = headers.get("Content-Disposition", "")
        assert "attachment" in cd
        assert "whenever-matrix.pdf" in cd

    def test_pdf_body_starts_with_pdf_magic(self, live_server, monkeypatch):
        monkeypatch.setattr(appmod, "run_search", lambda *a, **k: FIXTURE_RESULT)
        status, headers, body = _post(f"{live_server}/api/export/pdf", VALID_BODY)
        assert body[:4] == b"%PDF"

    def test_pdf_missing_dates_returns_400(self, live_server, monkeypatch):
        monkeypatch.setattr(appmod, "run_search", lambda *a, **k: FIXTURE_RESULT)
        status, headers, body = _post(f"{live_server}/api/export/pdf", MISSING_DATES_BODY)
        assert status == 400

    def test_pdf_missing_origin_returns_400(self, live_server, monkeypatch):
        monkeypatch.setattr(appmod, "run_search", lambda *a, **k: FIXTURE_RESULT)
        bad_body = {**VALID_BODY, "origin": ""}
        status, headers, body = _post(f"{live_server}/api/export/pdf", bad_body)
        assert status == 400
