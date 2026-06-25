import app as appmod


def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"Whenever" in r.data


def test_health(client, monkeypatch):
    monkeypatch.setattr(appmod, "ollama_ok", lambda: True)
    body = client.get("/api/health").get_json()
    assert body["ollama"] is True
    assert "model" in body


def test_top_cities_requires_country(client):
    r = client.post("/api/top-cities", json={"country": ""})
    assert r.status_code == 400


def test_top_cities_success(client, monkeypatch):
    monkeypatch.setattr(appmod, "top_cities", lambda c, n: [{"city": "Beijing", "iata": "PEK"}])
    r = client.post("/api/top-cities", json={"country": "China", "n": 6})
    assert r.status_code == 200
    assert r.get_json()["cities"][0]["iata"] == "PEK"


def test_top_cities_model_error(client, monkeypatch):
    def boom(c, n):
        raise RuntimeError("model offline")

    monkeypatch.setattr(appmod, "top_cities", boom)
    r = client.post("/api/top-cities", json={"country": "China"})
    assert r.status_code == 502


def test_resolve_with_and_without_city(client, monkeypatch):
    monkeypatch.setattr(appmod, "resolve_airport", lambda city: "YYZ")
    assert client.post("/api/resolve", json={"city": "Toronto"}).get_json()["iata"] == "YYZ"
    assert client.post("/api/resolve", json={"city": ""}).get_json()["iata"] == ""


def test_search_validation(client):
    # Supply explicit dep_dates/ret_dates to avoid the pre-validation date_range("") crash
    # tracked in issue #9; without them the test never reaches the real 400 branch.
    r = client.post("/api/search", json={
        "origin": "", "destinations": [],
        "dep_dates": ["2026-12-12"], "ret_dates": ["2027-01-04"],
    })
    assert r.status_code == 400


def test_search_missing_dates_returns_400(client):
    # Issue #9: omitting dates must return 400, not 500 (ValueError from date_range(""))
    r = client.post("/api/search", json={
        "origin": "YYZ",
        "destinations": [{"city": "X", "iata": "XXX"}],
    })
    assert r.status_code == 400


def test_search_non_string_dates_returns_400(client):
    # Issue #9: non-string dep_start (e.g. integer) must return 400, not 500 (TypeError)
    r = client.post("/api/search", json={
        "origin": "YYZ",
        "destinations": [{"city": "X", "iata": "XXX"}],
        "dep_start": 123, "ret_start": 456,
    })
    assert r.status_code == 400


def test_search_malformed_dates_returns_400(client):
    # Issue #9: malformed dep_start/ret_start must also return 400, not 500
    r = client.post("/api/search", json={
        "origin": "YYZ",
        "destinations": [{"city": "X", "iata": "XXX"}],
        "dep_start": "not-a-date", "ret_start": "also-bad",
    })
    assert r.status_code == 400


def test_search_success_picks_nonstop_when_within_threshold(client, monkeypatch):
    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: {
        "cheapest_cad": 1000, "stops": 1, "nonstop_cad": 1100, "source": "test", "book": None,
    })
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "Best value: Shanghai")
    payload = {
        "origin": "YYZ",
        "destinations": [{"city": "Shanghai", "iata": "PVG"}],
        "adults": 2, "child_ages": [11],
        "dep_start": "2026-12-12", "dep_span": 2,
        "ret_start": "2027-01-04", "ret_span": 2,
        "nonstop_threshold": 25,
    }
    data = client.post("/api/search", json=payload).get_json()
    cell = data["results"][0]["grid"][0][0]
    assert cell["chosen"] == "nonstop"            # 1100 <= 1000 * 1.25
    assert cell["chosen_cad"] == 1100
    assert data["recommendation"] == "Best value: Shanghai"
    assert data["results"][0]["best"] is not None


def test_search_chooses_cheapest_when_nonstop_too_pricey(client, monkeypatch):
    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: {
        "cheapest_cad": 1000, "stops": 1, "nonstop_cad": 2000, "source": "test", "book": "https://b",
    })
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "rec")
    payload = {
        "origin": "YYZ", "destinations": [{"city": "X", "iata": "XXX"}],
        "dep_dates": ["2026-12-12"], "ret_dates": ["2027-01-04"],
        "nonstop_threshold": 10,
    }
    cell = client.post("/api/search", json=payload).get_json()["results"][0]["grid"][0][0]
    assert cell["chosen"] == "cheapest"           # 2000 > 1000 * 1.10
    assert cell["book"] == "https://b"


def test_search_handles_no_data_cells(client, monkeypatch):
    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: {
        "cheapest_cad": None, "stops": None, "nonstop_cad": None, "source": "no-data",
    })
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "rec")
    payload = {
        "origin": "YYZ", "destinations": [{"city": "X", "iata": "XXX"}],
        "dep_dates": ["2026-12-12"], "ret_dates": ["2027-01-04"],
    }
    res = client.post("/api/search", json=payload).get_json()["results"][0]
    assert res["best"] is None                    # no priceable cells
    assert res["grid"][0][0]["book"].startswith("https://www.kayak.com")


def test_build_recommendation_success(monkeypatch):
    monkeypatch.setattr(appmod, "ollama_chat", lambda prompt: "AI says go to Shanghai")
    results = [{"city": "Shanghai", "iata": "PVG",
                "best": {"chosen_cad": 8000, "dep": "2026-12-12", "ret": "2027-01-04",
                         "chosen": "cheapest", "stops": 1}}]
    out = appmod.build_recommendation("YYZ", results, 2, [11], 3)
    assert out == "AI says go to Shanghai"


def test_build_recommendation_fallback_with_prices(monkeypatch):
    def boom(prompt):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(appmod, "ollama_chat", boom)
    results = [{"city": "Shanghai", "iata": "PVG",
                "best": {"chosen_cad": 8000, "dep": "2026-12-12", "ret": "2027-01-04",
                         "chosen": "cheapest", "stops": 1}}]
    out = appmod.build_recommendation("YYZ", results, 2, [11], 3)
    assert "Best value: Shanghai" in out
    assert "AI summary unavailable" in out


def test_build_recommendation_fallback_no_prices(monkeypatch):
    def boom(prompt):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(appmod, "ollama_chat", boom)
    results = [{"city": "Nowhere", "iata": "XXX", "best": None}]
    out = appmod.build_recommendation("YYZ", results, 1, [], 1)
    assert out == "No priceable options found."


# ---------------------------------------------------------------------------
# Fix 1 regression: fractional nonstop_threshold must not be truncated to int
# ---------------------------------------------------------------------------

def test_search_fractional_threshold_honored(client, monkeypatch):
    """Regression: api_search must parse nonstop_threshold as float.

    cheapest=1000, nonstop=1255.  Boundary:
      threshold=25   → limit=1250  → nonstop too pricey → cheapest chosen
      threshold=25.5 → limit=1255  → nonstop exactly at limit → nonstop chosen
    If threshold were truncated to int(25.5)=25 the nonstop would not be picked.
    """
    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: {
        "cheapest_cad": 1000, "stops": 1, "nonstop_cad": 1255,
        "source": "test", "book": None,
    })
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "rec")

    payload = {
        "origin": "YYZ",
        "destinations": [{"city": "X", "iata": "XXX"}],
        "dep_dates": ["2026-12-12"],
        "ret_dates": ["2027-01-04"],
        "nonstop_threshold": 25.5,  # float: 1255 <= 1000*1.255 → nonstop chosen
    }
    cell = client.post("/api/search", json=payload).get_json()["results"][0]["grid"][0][0]
    assert cell["chosen"] == "nonstop", (
        "Fractional threshold 25.5 should select nonstop at 1255 (limit=1255.0); "
        "int truncation to 25 would set limit=1250 and pick cheapest instead."
    )
    assert cell["chosen_cad"] == 1255


def test_search_string_fractional_threshold_does_not_raise(client, monkeypatch):
    """Regression: nonstop_threshold sent as string '25.5' must not raise (int() would fail)."""
    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: {
        "cheapest_cad": 1000, "stops": 1, "nonstop_cad": 1100,
        "source": "test", "book": None,
    })
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "rec")

    payload = {
        "origin": "YYZ",
        "destinations": [{"city": "X", "iata": "XXX"}],
        "dep_dates": ["2026-12-12"],
        "ret_dates": ["2027-01-04"],
        "nonstop_threshold": "25.5",  # string decimal — float() handles this, int() does not
    }
    r = client.post("/api/search", json=payload)
    assert r.status_code == 200, "String '25.5' threshold must not crash the route"


# ---------------------------------------------------------------------------
# Export route coverage — /api/export/csv and /api/export/pdf
# ---------------------------------------------------------------------------

_EXPORT_PAYLOAD = {
    "origin": "YYZ",
    "destinations": [{"city": "Shanghai", "iata": "PVG"}],
    "dep_dates": ["2026-12-12"],
    "ret_dates": ["2027-01-04"],
}

_FAKE_FARE = {
    "cheapest_cad": 1200, "stops": 1, "nonstop_cad": None,
    "source": "test", "book": None,
}


def test_export_csv_returns_csv(client, monkeypatch):
    """POST /api/export/csv must return text/csv with the grid data."""
    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: _FAKE_FARE)
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "rec")

    r = client.post("/api/export/csv", json=_EXPORT_PAYLOAD)
    assert r.status_code == 200
    assert "text/csv" in r.content_type
    assert b"PVG" in r.data


def test_export_csv_bad_payload_returns_400(client):
    """POST /api/export/csv with missing fields must return 400."""
    r = client.post("/api/export/csv", json={"origin": ""})
    assert r.status_code == 400


def test_export_pdf_returns_pdf(client, monkeypatch):
    """POST /api/export/pdf must return application/pdf bytes."""
    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: _FAKE_FARE)
    monkeypatch.setattr(appmod, "build_recommendation", lambda *a, **k: "rec")

    r = client.post("/api/export/pdf", json=_EXPORT_PAYLOAD)
    assert r.status_code == 200
    assert r.content_type == "application/pdf"
    assert r.data[:4] == b"%PDF"


def test_export_pdf_bad_payload_returns_400(client):
    """POST /api/export/pdf with missing fields must return 400."""
    r = client.post("/api/export/pdf", json={"origin": ""})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Quota guard: MAX_SEARCH_CELLS hard cap (backend) — both routes
# ---------------------------------------------------------------------------

_LARGE_PAYLOAD = {
    "origin": "YYZ",
    "destinations": [{"city": "A", "iata": "AAA"}, {"city": "B", "iata": "BBB"}],
    "dep_dates": ["2026-12-12", "2026-12-13"],
    "ret_dates": ["2027-01-04", "2027-01-05"],
    # 2 dests × 2 dep × 2 ret = 8 cells
}

_SMALL_PAYLOAD = {
    "origin": "YYZ",
    "destinations": [{"city": "A", "iata": "AAA"}],
    "dep_dates": ["2026-12-12"],
    "ret_dates": ["2027-01-04"],
    # 1 dest × 1 dep × 1 ret = 1 cell
}


def test_search_rejects_over_cap(client, monkeypatch):
    """POST /api/search with cells > MAX_SEARCH_CELLS must return 400 with 'too large' error."""
    monkeypatch.setattr("app.MAX_SEARCH_CELLS", 2)
    r = client.post("/api/search", json=_LARGE_PAYLOAD)
    assert r.status_code == 400
    body = r.get_json()
    assert "too large" in body["error"]
    assert "8" in body["error"]   # total_cells present in message
    assert "2" in body["error"]   # cap value present


def test_search_accepts_at_or_under_cap(client, monkeypatch):
    """POST /api/search with cells <= MAX_SEARCH_CELLS must succeed (200)."""
    monkeypatch.setattr("app.MAX_SEARCH_CELLS", 1)
    monkeypatch.setattr("app.get_fare", lambda *a, **k: {
        "cheapest_cad": 1000, "stops": 0, "nonstop_cad": None, "source": "test", "book": None,
    })
    monkeypatch.setattr("app.build_recommendation", lambda *a, **k: "rec")
    r = client.post("/api/search", json=_SMALL_PAYLOAD)
    assert r.status_code == 200


def test_search_disabled_cap_allows_large(client, monkeypatch):
    """MAX_SEARCH_CELLS <= 0 disables the cap — large searches must pass through."""
    monkeypatch.setattr("app.MAX_SEARCH_CELLS", 0)
    monkeypatch.setattr("app.get_fare", lambda *a, **k: {
        "cheapest_cad": 1000, "stops": 0, "nonstop_cad": None, "source": "test", "book": None,
    })
    monkeypatch.setattr("app.build_recommendation", lambda *a, **k: "rec")
    r = client.post("/api/search", json=_LARGE_PAYLOAD)
    assert r.status_code == 200


def test_stream_rejects_over_cap(client, monkeypatch):
    """POST /api/search/stream with cells > MAX_SEARCH_CELLS must return 400 JSON (not streamed)."""
    monkeypatch.setattr("app.MAX_SEARCH_CELLS", 2)
    r = client.post("/api/search/stream", json=_LARGE_PAYLOAD)
    assert r.status_code == 400
    body = r.get_json()
    assert "too large" in body["error"]
    assert "application/json" in r.content_type


def test_stream_accepts_at_or_under_cap(client, monkeypatch):
    """POST /api/search/stream with cells <= MAX_SEARCH_CELLS must stream normally (200)."""
    monkeypatch.setattr("app.MAX_SEARCH_CELLS", 1)
    monkeypatch.setattr("app.get_fare", lambda *a, **k: {
        "cheapest_cad": 1000, "stops": 0, "nonstop_cad": None, "source": "test", "book": None,
    })
    monkeypatch.setattr("app.build_recommendation", lambda *a, **k: "rec")
    r = client.post("/api/search/stream", json=_SMALL_PAYLOAD)
    assert r.status_code == 200
    assert "application/x-ndjson" in r.content_type


def test_stream_disabled_cap_allows_large(client, monkeypatch):
    """MAX_SEARCH_CELLS <= 0 disables the cap — large streams must proceed normally."""
    monkeypatch.setattr("app.MAX_SEARCH_CELLS", 0)
    monkeypatch.setattr("app.get_fare", lambda *a, **k: {
        "cheapest_cad": 1000, "stops": 0, "nonstop_cad": None, "source": "test", "book": None,
    })
    monkeypatch.setattr("app.build_recommendation", lambda *a, **k: "rec")
    r = client.post("/api/search/stream", json=_LARGE_PAYLOAD)
    assert r.status_code == 200
