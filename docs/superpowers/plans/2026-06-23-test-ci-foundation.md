# Test & CI Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the existing Flask app a hermetic test suite (unit + Playwright e2e) reaching ≥99% line coverage, gated by GitHub Actions CI on `main`.

**Architecture:** `app.py` already exists and works; we are writing tests *against* it (not TDD-building new code), so each test step should pass immediately green against the current implementation. All external I/O (`requests` to Ollama/Amadeus/Travelpayouts) is monkeypatched so tests run offline and deterministically. E2E boots the real Flask app in a background thread with the fare/LLM functions stubbed, then drives the browser with Playwright. CI runs the whole suite plus the coverage gate.

**Tech Stack:** Python 3.11, Flask, pytest, pytest-cov, pytest-playwright (Chromium), werkzeug `make_server`, GitHub Actions.

## Global Constraints

- Coverage gate: line coverage of `app.py` must be **≥ 99%** (`pytest --cov=app --cov-fail-under=99`). Verbatim from CLAUDE.md rule 5.
- Tests must be **offline** — no real network, no real Ollama, no real flight API. Monkeypatch all `requests` calls.
- **Real-data-only principle is not under test relaxation:** stubs in tests stand in for real APIs; never assert AI-originated prices as if real.
- Tracks GitHub issues **#1 (branch reconcile), #2 (unit tests), #3 (e2e), #4 (CI)**.
- Implements CLAUDE.md / AGENTS.md workflow rules 1, 2, 5.

---

### Task 1: Test scaffolding, dev deps, coverage config

**Files:**
- Create: `requirements-dev.txt`
- Create: `pytest.ini`
- Create: `.coveragerc`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/unit/__init__.py`
- Create: `tests/unit/test_smoke.py`

**Interfaces:**
- Consumes: existing `app.py` (module importable as `app`).
- Produces: pytest discovery rooted at `tests/`; fixtures `client` (Flask test client), `fake_resp` (response factory), and an autouse `_reset_state` fixture that clears `lru_cache`s and the Amadeus token between tests. Later tasks import these from `conftest.py`.

- [ ] **Step 1: Add dev dependencies**

Create `requirements-dev.txt`:

```
pytest>=8.0
pytest-cov>=5.0
pytest-playwright>=0.5
```

- [ ] **Step 2: Add pytest config**

Create `pytest.ini`:

```ini
[pytest]
addopts = -q
testpaths = tests
```

- [ ] **Step 3: Add coverage config**

Create `.coveragerc`. The `__main__` guard (the dev-server launch) is excluded — it cannot run under test:

```ini
[run]
source = app

[report]
show_missing = True
exclude_lines =
    pragma: no cover
    if __name__ == .__main__.:
```

- [ ] **Step 4: Add shared fixtures**

Create `tests/__init__.py` (empty) and `tests/unit/__init__.py` (empty). Then create `tests/conftest.py`:

```python
import pytest
import app as appmod


class FakeResp:
    """Stand-in for a requests.Response."""
    def __init__(self, json_data=None, status=200, raise_exc=None):
        self._json = {} if json_data is None else json_data
        self.status_code = status
        self._raise = raise_exc

    def json(self):
        return self._json

    def raise_for_status(self):
        if self._raise is not None:
            raise self._raise


@pytest.fixture
def fake_resp():
    return FakeResp


@pytest.fixture
def client():
    appmod.app.config.update(TESTING=True)
    return appmod.app.test_client()


@pytest.fixture(autouse=True)
def _reset_state():
    """Caches and the Amadeus token leak across tests; reset them."""
    appmod.top_cities.cache_clear()
    appmod.resolve_airport.cache_clear()
    appmod._amadeus_token["value"] = None
    appmod._amadeus_token["exp"] = 0
    yield
    appmod.top_cities.cache_clear()
    appmod.resolve_airport.cache_clear()
```

- [ ] **Step 5: Add a smoke test**

Create `tests/unit/test_smoke.py`:

```python
import app as appmod


def test_app_imports():
    assert appmod.app is not None


def test_health_endpoint_smoke(client, monkeypatch):
    monkeypatch.setattr(appmod, "ollama_ok", lambda: False)
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.get_json()["model"] == appmod.OLLAMA_MODEL
```

- [ ] **Step 6: Install and run**

Run:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/unit/test_smoke.py -v
```

Expected: 2 passed.

- [ ] **Step 7: Commit**

```bash
git add requirements-dev.txt pytest.ini .coveragerc tests/__init__.py tests/conftest.py tests/unit/__init__.py tests/unit/test_smoke.py
git commit -m "test: add pytest scaffolding, fixtures, and coverage config"
```

---

### Task 2: Unit tests for pure helpers and the Ollama layer

**Files:**
- Create: `tests/unit/test_helpers.py`
- Create: `tests/unit/test_ollama.py`
- Test target: `app.py` (`extract_json`, `date_range`, `kayak_link`, `providers_configured`, `ollama_chat`, `ollama_ok`, `top_cities`, `resolve_airport`)

**Interfaces:**
- Consumes: `client`, `fake_resp`, `_reset_state` from `tests/conftest.py`.
- Produces: nothing downstream depends on these tests.

- [ ] **Step 1: Write helper tests**

Create `tests/unit/test_helpers.py`:

```python
import pytest
import app as appmod


def test_extract_json_array():
    assert appmod.extract_json('```json\n[{"a":1}]\n```') == [{"a": 1}]


def test_extract_json_object():
    assert appmod.extract_json('noise {"x": 2} tail') == {"x": 2}


def test_extract_json_raises_when_absent():
    with pytest.raises(ValueError):
        appmod.extract_json("no json here")


def test_date_range():
    assert appmod.date_range("2026-12-12", 3) == [
        "2026-12-12", "2026-12-13", "2026-12-14",
    ]


def test_kayak_link_without_children():
    url = appmod.kayak_link("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, [])
    assert url == "https://www.kayak.com/flights/YYZ-PVG/2026-12-12/2027-01-04/2adults"


def test_kayak_link_with_children():
    url = appmod.kayak_link("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, [11, 9])
    assert url.endswith("/2adults/children-11-9")


def test_providers_configured_combinations(monkeypatch):
    monkeypatch.setattr(appmod, "AMADEUS_ID", "id")
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", "secret")
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "tok")
    assert appmod.providers_configured() == ["amadeus", "travelpayouts"]

    monkeypatch.setattr(appmod, "AMADEUS_ID", None)
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", None)
    assert appmod.providers_configured() == []
```

- [ ] **Step 2: Run helper tests**

Run: `pytest tests/unit/test_helpers.py -v`
Expected: all passed.

- [ ] **Step 3: Write Ollama-layer tests**

Create `tests/unit/test_ollama.py`:

```python
import app as appmod


def test_ollama_chat_strips_think_and_uses_system(monkeypatch, fake_resp):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["json"] = json
        return fake_resp({"message": {"content": "<think>reason</think>  HELLO "}})

    monkeypatch.setattr(appmod.requests, "post", fake_post)
    out = appmod.ollama_chat("hi", system="be terse")
    assert out == "HELLO"
    assert captured["json"]["messages"][0]["role"] == "system"


def test_ollama_chat_without_system(monkeypatch, fake_resp):
    def fake_post(url, json=None, timeout=None):
        return fake_resp({"message": {"content": "world"}})

    monkeypatch.setattr(appmod.requests, "post", fake_post)
    out = appmod.ollama_chat("hi")
    assert out == "world"


def test_ollama_ok_true(monkeypatch, fake_resp):
    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: fake_resp({}))
    assert appmod.ollama_ok() is True


def test_ollama_ok_false(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(appmod.requests, "get", boom)
    assert appmod.ollama_ok() is False


def test_top_cities_filters_invalid_items(monkeypatch):
    payload = [
        {"city": "Beijing", "iata": "pek"},
        {"city": "NoCode"},          # dropped: no iata
        "garbage",                    # dropped: not a dict
        {"city": "Shanghai", "iata": "PVGXX"},
    ]
    monkeypatch.setattr(appmod, "ollama_chat", lambda *a, **k: __import__("json").dumps(payload))
    appmod.top_cities.cache_clear()
    out = appmod.top_cities("China", 6)
    assert out == [
        {"city": "Beijing", "iata": "PEK"},
        {"city": "Shanghai", "iata": "PVG"},
    ]


def test_resolve_airport_success(monkeypatch):
    monkeypatch.setattr(appmod, "ollama_chat", lambda *a, **k: '{"iata":"yyz"}')
    appmod.resolve_airport.cache_clear()
    assert appmod.resolve_airport("Toronto") == "YYZ"


def test_resolve_airport_handles_error(monkeypatch):
    def boom(*a, **k):
        raise ValueError("no json")

    monkeypatch.setattr(appmod, "ollama_chat", boom)
    appmod.resolve_airport.cache_clear()
    assert appmod.resolve_airport("Nowhere") == ""
```

- [ ] **Step 4: Run Ollama tests**

Run: `pytest tests/unit/test_ollama.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_helpers.py tests/unit/test_ollama.py
git commit -m "test: cover pure helpers and the Ollama layer"
```

---

### Task 3: Unit tests for fare providers and the get_fare adapter

**Files:**
- Create: `tests/unit/test_fares.py`
- Test target: `app.py` (`amadeus_token`, `amadeus_fare`, `travelpayouts_fare`, `get_fare`)

**Interfaces:**
- Consumes: `fake_resp`, `_reset_state` from `tests/conftest.py`.
- Produces: nothing downstream depends on these tests.

- [ ] **Step 1: Write fare tests**

Create `tests/unit/test_fares.py`. The Amadeus offer shape mirrors the real API: `itineraries[].segments[]` (stops = max segments − 1) and `price.grandTotal`.

```python
import app as appmod


def _amadeus_offer(grand_total, segs):
    return {
        "price": {"grandTotal": str(grand_total)},
        "itineraries": [{"segments": [{} for _ in range(segs)]}],
    }


def test_amadeus_token_none_without_creds(monkeypatch):
    monkeypatch.setattr(appmod, "AMADEUS_ID", None)
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", None)
    assert appmod.amadeus_token() is None


def test_amadeus_token_fetch_then_cache(monkeypatch, fake_resp):
    monkeypatch.setattr(appmod, "AMADEUS_ID", "id")
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", "secret")
    calls = {"n": 0}

    def fake_post(url, data=None, timeout=None):
        calls["n"] += 1
        return fake_resp({"access_token": "T123", "expires_in": 1799})

    monkeypatch.setattr(appmod.requests, "post", fake_post)
    assert appmod.amadeus_token() == "T123"
    # second call uses the cached token, no new POST
    monkeypatch.setattr(appmod.requests, "post",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("refetched")))
    assert appmod.amadeus_token() == "T123"
    assert calls["n"] == 1


def test_amadeus_fare_none_without_token(monkeypatch):
    monkeypatch.setattr(appmod, "amadeus_token", lambda: None)
    assert appmod.amadeus_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_amadeus_fare_picks_cheapest_and_nonstop(monkeypatch, fake_resp):
    monkeypatch.setattr(appmod, "amadeus_token", lambda: "T")
    offers = [
        _amadeus_offer(8000, 2),   # 1 stop, cheapest
        _amadeus_offer(14000, 1),  # nonstop
    ]
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": offers}, status=200))
    res = appmod.amadeus_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res == {"cheapest_cad": 8000, "stops": 1, "nonstop_cad": 14000, "source": "amadeus"}


def test_amadeus_fare_non_200(monkeypatch, fake_resp):
    monkeypatch.setattr(appmod, "amadeus_token", lambda: "T")
    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: fake_resp({}, status=429))
    assert appmod.amadeus_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_amadeus_fare_empty(monkeypatch, fake_resp):
    monkeypatch.setattr(appmod, "amadeus_token", lambda: "T")
    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: fake_resp({"data": []}))
    assert appmod.amadeus_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_travelpayouts_none_without_token(monkeypatch):
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", None)
    assert appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_travelpayouts_scales_and_builds_book_link(monkeypatch, fake_resp):
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "tok")
    data = [
        {"price": 1000, "transfers": 1, "return_transfers": 0, "link": "/deal/abc"},
        {"price": 1500, "transfers": 0, "return_transfers": 0, "link": "/ns"},
    ]
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": data}, status=200))
    res = appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 1)
    # pax = 3, cheapest 1000*3=3000, nonstop 1500*3=4500
    assert res["cheapest_cad"] == 3000
    assert res["nonstop_cad"] == 4500
    assert res["stops"] == 1
    assert res["source"] == "travelpayouts"
    assert res["book"] == "https://www.aviasales.com/deal/abc"


def test_travelpayouts_non_200(monkeypatch, fake_resp):
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "tok")
    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: fake_resp({}, status=500))
    assert appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_travelpayouts_empty(monkeypatch, fake_resp):
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "tok")
    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: fake_resp({"data": []}))
    assert appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_get_fare_returns_first_valid(monkeypatch):
    monkeypatch.setattr(appmod, "amadeus_fare", lambda *a: None)
    monkeypatch.setattr(appmod, "travelpayouts_fare",
                        lambda *a: {"cheapest_cad": 4200, "source": "travelpayouts"})
    res = appmod.get_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["source"] == "travelpayouts"


def test_get_fare_skips_exceptions(monkeypatch):
    def boom(*a):
        raise RuntimeError("provider down")

    monkeypatch.setattr(appmod, "amadeus_fare", boom)
    monkeypatch.setattr(appmod, "travelpayouts_fare",
                        lambda *a: {"cheapest_cad": 100, "source": "travelpayouts"})
    assert appmod.get_fare("YYZ", "PVG", "d", "r", 1, 0)["cheapest_cad"] == 100


def test_get_fare_no_data(monkeypatch):
    monkeypatch.setattr(appmod, "amadeus_fare", lambda *a: None)
    monkeypatch.setattr(appmod, "travelpayouts_fare", lambda *a: None)
    res = appmod.get_fare("YYZ", "PVG", "d", "r", 1, 0)
    assert res == {"cheapest_cad": None, "stops": None, "nonstop_cad": None, "source": "no-data"}
```

- [ ] **Step 2: Run fare tests**

Run: `pytest tests/unit/test_fares.py -v`
Expected: all passed.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_fares.py
git commit -m "test: cover fare providers and the get_fare adapter"
```

---

### Task 4: Unit tests for Flask routes and recommendation, hit 99% gate

**Files:**
- Create: `tests/unit/test_routes.py`
- Test target: `app.py` (`index`, `health`, `api_top_cities`, `api_resolve`, `api_search`, `build_recommendation`)

**Interfaces:**
- Consumes: `client`, `_reset_state` from `tests/conftest.py`.
- Produces: the full unit suite, which must drive `app.py` coverage to ≥99%.

- [ ] **Step 1: Write route + recommendation tests**

Create `tests/unit/test_routes.py`:

```python
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
    r = client.post("/api/search", json={"origin": "", "destinations": []})
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
```

- [ ] **Step 2: Run the full unit suite with the coverage gate**

Run: `pytest tests/unit --cov=app --cov-report=term-missing --cov-fail-under=99`
Expected: all passed, and coverage line for `app.py` ≥ 99%. If `term-missing` reports any uncovered line that is genuinely unreachable in tests (other than the excluded `__main__` guard), add a `# pragma: no cover` to that exact line in `app.py` and re-run. Do not lower the threshold.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_routes.py app.py
git commit -m "test: cover Flask routes and recommendation; reach 99% coverage"
```

---

### Task 5: Playwright end-to-end tests

**Files:**
- Create: `tests/e2e/__init__.py`
- Create: `tests/e2e/conftest.py`
- Create: `tests/e2e/test_ui.py`

**Interfaces:**
- Consumes: `app.app` (the Flask WSGI app), `app.ollama_ok`, `app.top_cities`, `app.get_fare`, `app.build_recommendation` (all monkeypatched in the live-server fixture).
- Produces: a `live_server` fixture (yields the base URL) and the `page` fixture from pytest-playwright.

- [ ] **Step 1: Install the browser**

Run: `python3 -m playwright install chromium`
Expected: Chromium downloaded.

- [ ] **Step 2: Write the live-server fixture**

Create `tests/e2e/__init__.py` (empty), then `tests/e2e/conftest.py`. The app reads `get_fare`/`build_recommendation`/`top_cities`/`ollama_ok` as module globals at request time, so patching them before the server thread serves is sufficient:

```python
import threading
import pytest
from werkzeug.serving import make_server
import app as appmod


@pytest.fixture
def live_server(monkeypatch):
    monkeypatch.setattr(appmod, "ollama_ok", lambda: True)
    monkeypatch.setattr(appmod, "top_cities",
                        lambda country, n=6: [{"city": "Shanghai", "iata": "PVG"}])
    monkeypatch.setattr(appmod, "get_fare", lambda *a, **k: {
        "cheapest_cad": 8000, "stops": 1, "nonstop_cad": 8500,
        "source": "test", "book": "https://example.com/book",
    })
    monkeypatch.setattr(appmod, "build_recommendation",
                        lambda *a, **k: "Best value: Shanghai (PVG)")

    srv = make_server("127.0.0.1", 5099, appmod.app)
    thread = threading.Thread(target=srv.serve_forever)
    thread.start()
    try:
        yield "http://127.0.0.1:5099"
    finally:
        srv.shutdown()
        thread.join()
```

- [ ] **Step 3: Write the UI tests**

Create `tests/e2e/test_ui.py`:

```python
def test_page_loads_and_health_renders(live_server, page):
    page.goto(live_server)
    page.wait_for_selector("#status")
    status = page.inner_text("#status")
    assert status.lower()                          # model name from /api/health


def test_full_search_flow(live_server, page):
    page.goto(live_server)
    page.click("#loadCities")
    page.wait_for_selector(".chip.on")             # Shanghai chip, toggled on
    page.click("#run")
    page.wait_for_selector("#summary .card")
    assert "Shanghai" in page.inner_text("#summary")
    assert "Best value: Shanghai" in page.inner_text("#rec")
    assert page.query_selector("table") is not None
```

- [ ] **Step 4: Run the e2e suite**

Run: `pytest tests/e2e -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/__init__.py tests/e2e/conftest.py tests/e2e/test_ui.py
git commit -m "test: add Playwright e2e for page load and search flow"
```

---

### Task 6: GitHub Actions CI with the coverage gate

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `README.md` (add a CI badge + testing section), `CONTRIBUTING.md` (document running tests)

**Interfaces:**
- Consumes: `requirements.txt`, `requirements-dev.txt`, the `tests/` suite.
- Produces: a CI check named `test` that becomes the required status check for `main` in Task 7.

- [ ] **Step 1: Write the workflow**

Create `.github/workflows/ci.yml`. The whole suite (unit + e2e) runs under one coverage gate because `pytest.ini` sets `testpaths = tests`:

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt -r requirements-dev.txt
      - name: Install Playwright browser
        run: python -m playwright install --with-deps chromium
      - name: Run tests with coverage gate
        run: pytest --cov=app --cov-report=term-missing --cov-fail-under=99
```

- [ ] **Step 2: Document testing in CONTRIBUTING.md**

Add this section under "Dev setup" in `CONTRIBUTING.md`:

```markdown
## Tests

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium
pytest --cov=app --cov-fail-under=99    # unit + e2e + coverage gate
```

CI runs the same command on every PR and blocks merge to `main` if it fails or
coverage drops below 99%.
```

- [ ] **Step 3: Add a CI badge to README.md**

Add directly under the title line (`# Whenever ✈️`) in `README.md`:

```markdown
[![CI](https://github.com/tomqwu/Whenever/actions/workflows/ci.yml/badge.svg)](https://github.com/tomqwu/Whenever/actions/workflows/ci.yml)
```

- [ ] **Step 4: Verify the suite passes locally one more time**

Run: `pytest --cov=app --cov-report=term-missing --cov-fail-under=99`
Expected: all passed, coverage ≥ 99%.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml README.md CONTRIBUTING.md
git commit -m "ci: add GitHub Actions test + 99% coverage gate"
```

---

### Task 7: Reconcile the default branch and enable branch protection (issue #1)

**Files:** none (GitHub settings + git remote operations). These steps are **manual / outside the codebase** and gate everything above into the workflow CLAUDE.md mandates.

**Interfaces:**
- Consumes: the `test` CI check produced in Task 6 (must have run at least once on a PR for GitHub to offer it as a required check).
- Produces: a protected `main` branch where merge requires green CI.

- [ ] **Step 1: Push the foundation work on a branch and open a PR**

```bash
git checkout -b foundation/test-ci
git push -u origin foundation/test-ci
gh pr create --fill --base master --title "Test & CI foundation" \
  --body "Closes #2, closes #3, closes #4. Sets up #1 (branch reconcile)."
```

(Base is `master` for now — it is still the remote default; the rename happens in Step 3.)

- [ ] **Step 2: Confirm CI runs green on the PR**

Run: `gh pr checks --watch`
Expected: the `test` check passes.

- [ ] **Step 3: Make `main` the default branch**

The remote default is `master`; local work is on `main`. Rename on GitHub so they match CLAUDE.md:

```bash
gh api -X POST repos/tomqwu/Whenever/branches/master/rename -f new_name=main
```

This renames `master` → `main`, repoints the default, and retargets open PRs. Verify:

```bash
gh repo view --json defaultBranchRef -q .defaultBranchRef.name   # expect: main
```

- [ ] **Step 4: Enable branch protection on `main`**

Require a PR and the `test` status check before merge (CLAUDE.md rules 1 + 2):

```bash
gh api -X PUT repos/tomqwu/Whenever/branches/main/protection \
  -H "Accept: application/vnd.github+json" \
  -f "required_status_checks[strict]=true" \
  -f "required_status_checks[contexts][]=test" \
  -f "enforce_admins=true" \
  -f "required_pull_request_reviews[required_approving_review_count]=0" \
  -f "restrictions=" 2>&1 | head -20
```

Expected: JSON describing the protection rule (no error). If the API rejects `restrictions=`, re-run omitting that line — it is only required on some plans.

- [ ] **Step 5: Merge once green (CLAUDE.md rule 2)**

```bash
gh pr merge --squash --delete-branch
```

Expected: PR merged into `main`; issues #2, #3, #4 auto-closed by the PR body.

- [ ] **Step 6: Close issue #1**

```bash
gh issue close 1 --comment "Default branch is now main with required 'test' status check and PR-gated merges."
```

---

## Self-Review

**Spec coverage:**
- Issue #2 (unit tests) → Tasks 2, 3, 4. ✓
- Issue #3 (e2e) → Task 5. ✓
- Issue #4 (CI gating + 99% coverage) → Task 6. ✓
- Issue #1 (branch reconcile + protection) → Task 7. ✓
- CLAUDE.md rule 5 (99% coverage) → Task 4 Step 2 gate + Task 6 CI. ✓
- CLAUDE.md rules 1 & 2 (CI gates/merge on green) → Task 7 protection + merge. ✓
- CLAUDE.md rule 4 (update docs with changes) → Task 6 Steps 2–3. ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code; every command shows expected output. The single conditional action (`# pragma: no cover` in Task 4 Step 2) is bounded and explicit. ✓

**Type/name consistency:** Stub `get_fare` return dicts match `app.py`'s shape (`cheapest_cad, stops, nonstop_cad, source, book`). Amadeus offer helper matches `itineraries[].segments[]` / `price.grandTotal`. Fixture names (`client`, `fake_resp`, `live_server`, `_reset_state`) are defined once and reused consistently. CI check name `test` (job id in `ci.yml`) matches the required-context name in Task 7 Step 4. ✓

**Not in scope (separate plans/issues):** CLAUDE.md rule 3 (`codex:review` before push) is a per-change workflow habit, not buildable infra. Roadmap issues #5–#8 (CLI, caching, second provider, price alerts) each warrant their own plan.
