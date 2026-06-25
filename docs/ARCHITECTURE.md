# Architecture

Whenever is a small Flask web app with three responsibilities, kept deliberately separate:

```
                ┌──────────────────────────────────────────────┐
   Browser ───▶ │  Flask (app.py)                              │
   (UI)         │                                              │
                │  1. GenAI layer  ──▶  Ollama (local DeepSeek) │
                │       • country → top cities                 │
                │       • city → IATA                          │
                │       • analyze collected fares → pick best  │
                │                                              │
                │  2. Fare layer   ──▶  Flight APIs (REAL data)│
                │       • SerpApi (Google Flights, preferred)  │
                │       • Amadeus Self-Service                 │
                │       • Travelpayouts / Aviasales            │
                │       • Kiwi / Tequila                       │
                │       • get_fare() adapter, no AI here       │
                │                                              │
                │  3. Booking links ──▶ provider deep-link or  │
                │                       Kayak search fallback  │
                └──────────────────────────────────────────────┘
```

## Ollama: local and cloud modes

The app supports both **local Ollama** (default, no auth) and **Ollama cloud** (`https://ollama.com`, Bearer auth):

- **Local:** leave `OLLAMA_API_KEY` unset. `OLLAMA_HOST` defaults to `http://localhost:11434`. No auth header is sent.
- **Cloud:** set `OLLAMA_HOST=https://ollama.com`, `OLLAMA_API_KEY=<key>`, and `OLLAMA_MODEL=<cloud model>` (e.g. `gpt-oss:120b`). The app sends `Authorization: Bearer <key>` on every Ollama request (`/api/chat` and `/api/tags`).

The `_ollama_headers()` helper in `app.py` returns `{"Authorization": "Bearer <key>"}` when the key is set, or `{}` when unset — so local behavior is completely unchanged.

## Core principle: AI analyzes, APIs price

The model **never produces a price**. All fares are fetched from flight APIs. The LLM only
operates *on* collected data (suggesting cities, ranking the result grid). If no flight API is
configured, cells return `source: "no-data"` rather than a fabricated number.

## Request flow

1. **`GET /`** — serves `templates/index.html`.
2. **`POST /api/top-cities`** `{country, n}` → `top_cities()` asks Ollama for top destinations + IATA.
3. **`POST /api/resolve`** `{city}` → `resolve_airport()` maps a city name to an IATA code.
4. **`POST /api/search`** — the main endpoint:
   - builds the departure × return date grid,
   - calls `get_fare(origin, dest, dep, ret, adults, children)` per cell **concurrently** via a `ThreadPoolExecutor` (bounded by `SEARCH_CONCURRENCY`, default 8),
   - applies the **nonstop-preference rule** (pick nonstop if within the premium threshold),
   - finds the best cell per city,
   - calls `build_recommendation()` → Ollama analyzes the grid and names the best value.
5. **`GET /api/health`** — reports Ollama reachability + configured flight providers.

## The fare adapter

`get_fare()` tries each configured provider in order and returns the first real result:

```python
def get_fare(origin, dest, dep, ret, adults, children):
    for provider in (skyscanner_fare, serpapi_fare, amadeus_fare, travelpayouts_fare, kiwi_fare):
        res = provider(...)
        if res and res.get("cheapest_cad"):
            return res
    return {"cheapest_cad": None, "source": "no-data"}
```

Each provider returns a normalized dict:

```json
{ "cheapest_cad": 8298, "stops": 1, "duration_min": 875, "nonstop_cad": 14756,
  "source": "travelpayouts", "book": "https://..." }
```

Each price line is paired with **its own** stops/duration (no mixed itineraries):
`duration_min` is the cheapest itinerary's round-trip time (pairs with
`cheapest_cad` + `stops`), `nonstop_duration_min` is the nonstop itinerary's
(pairs with `nonstop_cad`, 0 stops), and `_build_cell` derives
`chosen_duration_min` — the duration of the selected fare (`nonstop_duration_min`
when nonstop is chosen, else `duration_min`) — which pairs with `chosen_cad` and
feeds the best/summary/recommendation. Durations are parsed from each provider's
real data — amadeus ISO-8601 `itineraries[].duration` summed across legs,
travelpayouts `duration`/`duration_to`+`duration_back`, kiwi Tequila `duration`
seconds. SerpApi durations are always `null`: its single-call round-trip response
describes only the outbound leg (matching its `nonstop_cad=null` contract), so
exposing `total_duration` would understate the true round-trip time. Any of these
fields is `null` whenever its itinerary's duration is unavailable (never
fabricated, and never borrowed from another itinerary), and the best-value
recommendation factors `chosen_duration_min` in (price is balanced against total
flight time).

**Provider priority (first match wins):**
1. **RapidAPI flights-sky / Skyscanner** (`RAPIDAPI_KEY`, host `RAPIDAPI_HOST`) — **preferred; richest data** (round-trip duration, airlines, layovers). Place IDs are plain IATA codes (no resolution step). 3-step async flow: `search-roundtrip` → poll `search-incomplete` until the session is `complete` → parse `itineraries.buckets[].items[]`. The poll budget is longer-but-bounded and env-configurable (`SKYSCANNER_POLL_ATTEMPTS` × `SKYSCANNER_POLL_INTERVAL`, default ~12 × 1.5s ≈ 18s) so most routes — incl. long-haul — finish instead of falling back to serpapi; a per-poll `SKYSCANNER_POLL_TIMEOUT` (default 8s) plus break-on-timeout keep a hung session from running for minutes. Prices are party totals in CAD for the passed `adults`. Transient HTTP 502 on the request is retried a couple times; any other non-200, missing key, or unexpected shape → `None`. `airlines` (cheapest item's marketing carrier names) and `layovers` (connection IATA per leg) ride in the normalized dict for surfacing in the UI.
2. **SerpApi / Google Flights** (`SERPAPI_KEY`) — live Google Flights results; best coverage for long-haul exact-date searches (e.g. Toronto → China Dec 2026). Prices are party totals in CAD (not per-ticket). No direct booking URL; falls back to Kayak link. The single-call round-trip response exposes only the outbound leg, so `nonstop_cad` is not populated for this provider (no false round-trip nonstop), `stops` reflects the outbound leg only, and `duration_min` is `null` (total round-trip duration unavailable; a future return-leg fetch could populate it).
3. **Amadeus Self-Service** (`AMADEUS_CLIENT_ID` + `AMADEUS_CLIENT_SECRET`) — test environment; limited inventory.
4. **Travelpayouts / Aviasales** (`TRAVELPAYOUTS_TOKEN`) — cached market fares; per-ticket price scaled to party total.
5. **Kiwi / Tequila** (`KIWI_API_KEY`) — real fares with booking deep-links.

To add another provider, write one function with the signature above and prepend/append it to the tuple. No other code changes needed.

## Nonstop-preference rule

For each date cell: if a nonstop exists and `nonstop_cad <= cheapest_cad * (1 + threshold)`,
the nonstop is "chosen"; otherwise the cheapest connection is chosen. Threshold is user-set
(default 25%). This encodes "prefer direct unless it's significantly more expensive."

## Configuration (env)

| Variable | Purpose | Default |
|----------|---------|---------|
| `OLLAMA_HOST` | Ollama base URL (local or `https://ollama.com` for cloud) | `http://localhost:11434` |
| `OLLAMA_MODEL` | model tag (e.g. `deepseek-v4pro` local, `gpt-oss:120b` cloud) | `deepseek-v4pro` |
| `OLLAMA_API_KEY` | Bearer API key for Ollama cloud (`https://ollama.com/settings/keys`); leave unset for local Ollama | — |
| `CURRENCY` | output currency | `cad` |
| `TRAVELPAYOUTS_TOKEN` | Travelpayouts API token | — |
| `AMADEUS_CLIENT_ID` / `AMADEUS_CLIENT_SECRET` | Amadeus creds | — |
| `KIWI_API_KEY` | Kiwi/Tequila API key (free Self-Service tier at tequila.kiwi.com) | — |
| `RAPIDAPI_KEY` | RapidAPI key for the flights-sky (Skyscanner data) API — **preferred** provider (richest data: duration/airlines/layovers) | — |
| `RAPIDAPI_HOST` | RapidAPI host for the flights-sky API | `flights-sky.p.rapidapi.com` |
| `SKYSCANNER_POLL_ATTEMPTS` | Max number of `search-incomplete` poll requests while the async session reaches `complete`. Longer-but-bounded budget. | `12` |
| `SKYSCANNER_POLL_INTERVAL` | Seconds slept between poll attempts. Default ~12 × 1.5s ≈ 18s of total waiting. | `1.5` |
| `SKYSCANNER_POLL_TIMEOUT` | Per-poll request timeout (seconds). A request-level Timeout/error breaks the loop immediately (no further retries), so a hung session degrades to no-data within ~`attempts × interval` + one timeout — bounded, never minutes. | `8` |
| `SERPAPI_KEY` | SerpApi API key for live Google Flights data (free trial at serpapi.com) | — |
| `FARE_CACHE_TTL` | In-memory cache TTL (seconds) for fare results. Set `<= 0` to disable. | `3600` |
| `SEARCH_CONCURRENCY` | Max parallel threads for the departure×return grid fetch in `run_search`. Each cell is one provider call, so large grids × many cities = many provider calls (quota/cost) — recommend modest date spans (default UI: 2×2). | `8` |
| `MAX_SEARCH_CELLS` | Hard cap on total search grid cells (cities × dep_dates × ret_dates). Each cell = one provider API call. A request exceeding the cap returns HTTP 400 before any fare calls are made. Set `<= 0` to disable the cap. The frontend also shows a soft confirm dialog above 40 cells (see `CONFIRM_CELLS` in `templates/index.html`). | `200` |
| `MAX_DATE_SPAN` | Generous per-direction day cap. `dep_span`/`ret_span` (and `date_range`'s `count`) are clamped to this ceiling **before** the date arrays are expanded, so a malformed huge span (e.g. `dep_span=10000000`) can't allocate millions of dates and tie up the worker before the cell cap's 400 fires. The form max is small; this is a safety ceiling, not the typical value. | `60` |
| `PORT` | Dev-server port (`python app.py`). Default avoids macOS AirPlay Receiver, which holds 5000. | `5001` |

## Price Watch

`watch.py` + `scheduler.py` implement the "Watch This Trip" feature (issue #8).

**DB file:** SQLite (stdlib `sqlite3`), path from `WATCH_DB` env (default `whenever_watches.db`).
WAL mode is enabled for file DBs to handle light concurrent access.
Schema: two tables — `watches` (one row per saved search) and `price_history` (one row per check).

**Cron pattern:**
```
# Check all active watches once per hour
0 * * * * cd /path/to/Whenever && python scheduler.py
```
`scheduler.py` opens the DB, calls `check_all_watches(db, fare_fn=app.get_fare)`, prints a
summary to stdout, and exits 0. No long-running daemon — just run it as a cron job.

**Notification options:**
- Stdout: `[PRICE DROP] YYZ→PEK 2026-12-14/2027-01-04 CA$8,000 → CA$7,000 (-$1,000) book: <url>`
- Webhook: set `WATCH_WEBHOOK_URL` to receive a JSON POST on each drop. Webhook failures are
  caught and logged silently — they do not abort the run.
- Email/SMS: deferred to a future release.

**Cache-TTL caveat:** `get_fare` caches real priced results for `FARE_CACHE_TTL` seconds
(default 3600). Running `python scheduler.py` more than once per hour will serve cached prices,
not fresh API calls. Set a shorter `FARE_CACHE_TTL` or run at most once per hour.

**Real-data guardrail:** `check_all_watches` calls `app.get_fare` directly. No fabricated
prices are ever stored in `watches.last_price`. If `get_fare` returns `cheapest_cad=None`
(no-data), the check is recorded in `price_history` with a null price, but `last_price` in
`watches` is left unchanged (the last real price is preserved).

## Export

`export.py` provides `render_csv(result) -> str` and `render_pdf(result) -> bytes`,
both consuming the dict returned by `run_search()`.

**Endpoints (stateless POST-only):**

| Endpoint | Method | Response |
|----------|--------|----------|
| `/api/export/csv` | POST | `text/csv; charset=utf-8`, `Content-Disposition: attachment; filename="whenever-matrix.csv"` |
| `/api/export/pdf` | POST | `application/pdf`, `Content-Disposition: attachment; filename="whenever-matrix.pdf"` |

Both accept the same JSON body as `POST /api/search`. The server re-runs `run_search`
(calling the real `get_fare` chain) on each export request — there is no server-side
job storage and no `GET /api/export/<job_id>` route.

**PDF library:** `fpdf2` (pure-Python; pip-only; no system libraries such as pango or
cairo are required). The PDF is built programmatically — no HTML template is used.

**CSV columns:** `city, iata, dep_date, ret_date, cheapest_cad, stops, duration_min,
nonstop_cad, nonstop_duration_min, chosen, chosen_cad, chosen_duration_min, source,
book`. One row per `(city, dep_date, ret_date)` matrix cell.
`None`/no-data cells render as empty strings.

**Real-data guardrail:** export routes call `run_search` which calls `get_fare`.
No fabricated prices appear in exported files. Web-only — there is no CLI export path.

## Country Seed Config

`config/country_seeds.yaml` is a hand-maintained YAML file that maps lowercase country names
to a list of destination candidates with IATA codes, priorities, and an optional flag.

**Path:** loaded at `app.py` module import time via `_load_seed_config()` into `_SEED_CONFIG`.
The path is resolved relative to `app.py`: `os.path.join(os.path.dirname(__file__), "config", "country_seeds.yaml")`.

**n-limit / optional rule** (implemented in `top_cities(country, n)`):
- **Required cities** (`optional: false` or omitted): up to `n` returned, sorted by `priority`.
- **Optional cities** (`optional: true`): ALL appended after the required set regardless of `n`.
  The frontend renders optional cities unchecked; users opt in by clicking the chip.
- Example: `top_cities("China", 6)` → 6 required (priority 1–6) + 6 optional (priority 7–11, incl. nearby Hong Kong/Taiwan/Japan) = 12 total.
- `n` caps the required set only; it never filters optional cities.

**LLM fallback:** If no seed entry exists for the requested country, the existing `ollama_chat`
path is used and entries are annotated with `optional: False`. The `lru_cache` on `top_cities`
remains valid — the seed lookup is deterministic by `(country, n)`.

**Graceful degradation:** If `config/country_seeds.yaml` is absent or unparseable at startup,
a warning is logged and `_SEED_CONFIG = {}` so all countries fall through to the LLM.

## Known limitations

- Travelpayouts returns **cached** market fares (real, but not always live seat-level quotes);
  Amadeus test environment has limited inventory. Click-through booking links show live fares.
- Per-ticket → party scaling treats children at ~full fare for Travelpayouts (Amadeus prices
  children properly). Verify exact totals at booking.
- An in-memory TTL cache (`_fare_cache`, default 3600 s) sits in front of provider calls.
  Only real priced results are cached; no-data sentinels are never stored. Configure via
  `FARE_CACHE_TTL`; set `<= 0` to disable. Cache is process-local and not shared across
  workers or restarts.
