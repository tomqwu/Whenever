# Architecture

Whenever is a small Flask web app with three responsibilities, kept deliberately separate:

```
                ┌──────────────────────────────────────────────┐
   Browser ───▶ │  Flask (app.py)                              │
   (UI)         │                                              │
                │  1. GenAI layer  ──▶  Ollama (local model)     │
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
  "airlines": ["Air Canada", "ANA"], "layovers": [{ "iata": "NRT", "duration_min": 80 }],
  "source": "travelpayouts", "book": "https://..." }
```

`airlines` and `layovers` describe the **cheapest** itinerary and are real-data-only
(`null`/`[]` when a provider doesn't supply them, never fabricated): `airlines` is the
de-duplicated carrier **names** (serpapi, skyscanner) or IATA **codes** (amadeus from
segment `carrierCode`, kiwi from the `airlines`/route codes, travelpayouts `[airline]`);
`layovers` is a per-connection `[{ iata, name?, duration_min }]` list (serpapi from
`layovers[]`, amadeus/kiwi derived from inter-segment gaps, skyscanner from segment
connection IATAs, travelpayouts `null` — no per-stop detail). `_build_cell` also derives
`chosen_layovers` (the selected fare's layovers; `[]` when the nonstop line is chosen),
which the best/summary/recommendation surface alongside `chosen_duration_min`.

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

### Bounded provider retry / backoff (transient failures)

The one-shot provider request calls (Amadeus token + search, Travelpayouts, Kiwi,
SerpApi) go through a shared `_request_with_retry(method, url, …)` helper that makes a
**transient** failure degrade gracefully or recover instead of immediately dropping the
cell to no-data.

**What is retried:** `requests.ConnectionError` (fast TCP refusal / DNS failure) and
responses with a retryable status (`429`, `500`, `502`, `503`, `504`), up to
`PROVIDER_RETRIES + 1` total attempts. Between attempts it sleeps
`PROVIDER_BACKOFF × 2**attempt` seconds, **each sleep capped at `PROVIDER_BACKOFF_MAX`**;
a `429` carrying a `Retry-After` delta-seconds header honours that value, also clamped to
`PROVIDER_BACKOFF_MAX` (so a hostile/large header can't stall a worker).

**What is NOT retried — `requests.Timeout` is terminal.** A full Timeout means the
per-attempt budget (e.g. 20–30s) has already been consumed; retrying would stack identical
stalls across attempts and potentially block a cell for 60–90s or more with
`PROVIDER_RETRIES=2`. Instead, on any `Timeout` the helper returns `None` immediately —
the provider falls through to the next one after just one timeout, not retries+1.

After the final attempt it returns the last response (e.g. a persistent 5xx) so each
provider's existing `status_code != 200 → None` contract is unchanged, or `None` if the
attempt raised a network/timeout error. The whole thing is strictly **bounded** — same
discipline as the Skyscanner poll fix; a blip recovers, a genuine outage falls back to the
next provider in seconds, not minutes. The **Skyscanner poll path is deliberately NOT
wrapped** by this helper — it keeps its own purpose-built bounded 502 retry
(`_skyscanner_get`) and the poll-attempt budget, and poll requests pass `retries_502=0`,
so the two retry mechanisms never stack/double-retry.

## Nonstop-preference rule

For each date cell: if a nonstop exists and `nonstop_cad <= cheapest_cad * (1 + threshold)`,
the nonstop is "chosen"; otherwise the cheapest connection is chosen. Threshold is user-set
(default 25%). This encodes "prefer direct unless it's significantly more expensive."

## Configuration (env)

| Variable | Purpose | Default |
|----------|---------|---------|
| `OLLAMA_HOST` | Ollama base URL (local or `https://ollama.com` for cloud) | `http://localhost:11434` |
| `OLLAMA_MODEL` | model tag (local default `qwen3:8b`; cloud e.g. `gpt-oss:120b`). Tiers: small/fast `qwen3.5:4b`; balanced `qwen3:8b` (default); prosumer `qwen3:30b`; cloud `gpt-oss:120b`. | `qwen3:8b` |
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
| `PROVIDER_RETRIES` | Max **retries** (extra attempts) for a transient provider failure — ConnectionError, HTTP 5xx, or a 429 rate-limit — on the one-shot provider calls (amadeus token + search, travelpayouts, kiwi, serpapi). `retries + 1` total attempts. **Note: `requests.Timeout` is NOT retried** (terminal — returns None immediately); retrying a full timeout would stack stalls and defeat the bounded goal. Set `0` to disable retrying. | `2` |
| `PROVIDER_BACKOFF` | Base seconds for exponential backoff between provider retries: sleep `PROVIDER_BACKOFF × 2**attempt`, each sleep capped at `PROVIDER_BACKOFF_MAX`. Default 2 retries × 0.5s ≈ 0.5 + 1.0 ≈ 1.5s worst-case extra wait — bounded, never minutes. | `0.5` |
| `PROVIDER_BACKOFF_MAX` | Per-sleep cap (seconds) for provider backoff **and** for a 429 `Retry-After` header (a large/hostile `Retry-After` is clamped to this), so a single cell can't stall. | `4` |
| `FARE_CACHE_TTL` | In-memory cache TTL (seconds) for fare results. Set `<= 0` to disable. | `3600` |
| `FARE_CACHE_PATH` | JSON file the fare cache is persisted to so real fares survive a restart, cutting provider calls/quota. Each entry stores its fetch time and is revalidated against the **current** `FARE_CACHE_TTL` on load, so lowering the TTL takes effect immediately (no stale serving). Empty string = memory-only (no disk I/O). `FARE_CACHE_TTL <= 0` disables caching **and** persistence. Only real priced results are written; no-data sentinels never are. Writes are atomic (temp file + `os.replace`) and lock-guarded for concurrent searches. | `whenever_fare_cache.json` |
| `SEARCH_CONCURRENCY` | Max parallel threads for the departure×return grid fetch in `run_search`. Each cell is one provider call, so large grids × many cities = many provider calls (quota/cost) — recommend modest date spans (default UI: 2×2). | `8` |
| `MAX_SEARCH_CELLS` | Hard cap on total search grid cells (cities × dep_dates × ret_dates). Each cell = one provider API call. A request exceeding the cap returns HTTP 400 before any fare calls are made. Set `<= 0` to disable the cap. The frontend also shows a soft confirm dialog above 40 cells (see `CONFIRM_CELLS` in `templates/index.html`). | `200` |
| `MAX_DATE_SPAN` | Generous per-direction day cap. `dep_span`/`ret_span` (and `date_range`'s `count`) are clamped to this ceiling **before** the date arrays are expanded, so a malformed huge span (e.g. `dep_span=10000000`) can't allocate millions of dates and tie up the worker before the cell cap's 400 fires. The form max is small; this is a safety ceiling, not the typical value. | `60` |
| `PORT` | Dev-server port (`python app.py`). Default avoids macOS AirPlay Receiver, which holds 5000. | `5001` |
| `RATE_LIMIT_ENABLED` | Enable in-memory per-IP rate limiting. Set to `0`, `false`, or `no` to disable (e.g. when a reverse proxy already handles this, or in tests). | `true` |
| `RATE_LIMIT_WINDOW` | Sliding-window size in seconds for rate limiting. | `60` |
| `SEARCH_RATE_PER_MIN` | Max requests per window for the **search** bucket (`/api/search`, `/api/search/stream`, `/api/export/csv`, `/api/export/pdf`). Exceeding this returns HTTP 429 + `Retry-After`. | `10` |
| `API_RATE_PER_MIN` | Max requests per window for the **api** bucket (`/api/top-cities`, `/api/suggest`, `/api/resolve`, `/api/watch` POST/GET, `/api/watch/<id>` DELETE). | `60` |
| `TRUST_PROXY` | Whether to trust the client-supplied `X-Forwarded-For` header for client-IP identity. Set to `1`/`true`/`yes` **only** when the app sits behind a trusted reverse proxy that sets `X-Forwarded-For` itself. Default **false**: XFF is ignored entirely and the rate-limit bucket keys on the real socket peer (`REMOTE_ADDR`), so a client cannot rotate/spoof the header to bypass 429s or burn another user's quota. | `false` |

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
(default 3600) and persists them to `FARE_CACHE_PATH`, so even a *fresh* `python scheduler.py`
process loads still-valid cached prices from disk rather than calling the provider. Running it
more than once per TTL window will serve cached prices, not fresh API calls. Set a shorter
`FARE_CACHE_TTL`, set `FARE_CACHE_PATH=""` to disable persistence, or run at most once per hour.

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
nonstop_cad, nonstop_duration_min, chosen, chosen_cad, chosen_stops, chosen_duration_min,
airlines, layovers, source, book`. `airlines` is a `"A, B"` string and `layovers` a
compact `"PEK 1h20m, NRT"` string (cheapest itinerary). One row per
`(city, dep_date, ret_date)` matrix cell. `None`/no-data cells render as empty strings.

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

## Rate Limiting (#60)

A lightweight in-memory per-IP sliding-window rate limiter (no new dependencies) guards endpoints that trigger real provider calls.

**Two buckets:**
- **search** — `/api/search`, `/api/search/stream`, `/api/export/csv`, `/api/export/pdf` (default: 10 req/60 s)
- **api** — `/api/top-cities`, `/api/suggest`, `/api/resolve`, `/api/watch` (POST/GET/DELETE) (default: 60 req/60 s)

**Exempt:** `/` and `/api/health`.

**Behavior:**
- Client IP = the real socket peer (`REMOTE_ADDR`) by default. The client-supplied `X-Forwarded-For` header is **ignored** unless `TRUST_PROXY` is set true (app behind a trusted proxy that sets XFF), in which case the **first hop** of `X-Forwarded-For` is used. This prevents a client from rotating/spoofing XFF to evade rate limits.
- Sliding window: timestamps older than `RATE_LIMIT_WINDOW` seconds are pruned on each request.
- The prune → check → append sequence runs under a module-level `threading.Lock` (`_rate_lock`) so the count-and-record is atomic under a threaded WSGI server (two same-IP requests cannot both pass the limit check before either records).
- If the count ≥ the bucket limit, returns `HTTP 429` with JSON `{"error": "rate limit exceeded, slow down"}` and a `Retry-After` header (seconds until the oldest timestamp ages out).
- For `/api/search/stream`, the 429 is returned **before** any streaming begins (the decorator runs first).
- Memory hygiene: pruning on every call keeps per-IP bucket lists bounded to at most `limit` entries.
- Disable via `RATE_LIMIT_ENABLED=false` (or `0`/`no`) — useful when a reverse proxy already handles rate limiting, or in tests (the autouse `_reset_state` fixture defaults it off so all existing tests are unaffected).
- Pairs with the existing quota guard (`MAX_SEARCH_CELLS`) — both remain active when rate limiting is enabled.

## Known limitations

- Travelpayouts returns **cached** market fares (real, but not always live seat-level quotes);
  Amadeus test environment has limited inventory. Click-through booking links show live fares.
- Per-ticket → party scaling treats children at ~full fare for Travelpayouts (Amadeus prices
  children properly). Verify exact totals at booking.
- An in-memory TTL cache (`_fare_cache`, default 3600 s) sits in front of provider calls.
  Only real priced results are cached; no-data sentinels are never stored. Configure via
  `FARE_CACHE_TTL`; set `<= 0` to disable. The cache is process-local (not shared across
  workers), but is **persisted to disk** (`FARE_CACHE_PATH`, default
  `whenever_fare_cache.json`) so real fares survive a restart while still fresh. Each entry
  stores **when its fare was fetched** (not an absolute expiry), and freshness is judged as
  `now - fetched < FARE_CACHE_TTL` everywhere — in memory and on load — so **lowering the
  TTL between runs takes effect immediately**: a fare written under a longer TTL is dropped
  on the next check/load instead of being served stale. On a write of a new real fare the
  whole still-fresh cache is rewritten atomically (temp file + `os.replace`) under a
  `threading.Lock`; in-memory cache hits never touch disk. On startup `_load_fare_cache()`
  reads the file, drops entries no longer fresh under the **current** `FARE_CACHE_TTL`, and
  rebuilds the tuple-keyed cache; a missing/corrupt/malformed file is logged and starts
  empty (never crashes). Set `FARE_CACHE_PATH=""` for memory-only (no disk I/O).
