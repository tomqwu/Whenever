# flights-sky (Skyscanner) API — integration reference

The preferred fare provider (`skyscanner_fare` in `app.py`). This is the **real,
verified** reference for the API our key is subscribed to. Written from live probing
— keep it accurate as the API evolves.

## ⚠️ Which API is this? (don't confuse the two)

| | **What we use** | The Postman doc you may find online |
|---|---|---|
| RapidAPI host | **`flights-sky.p.rapidapi.com`** | `sky-scanner3.p.rapidapi.com` |
| Path prefix | **`/web/flights/...`** | `/flights/...` (no `/web/`) |
| Place param | **`placeIdFrom` / `placeIdTo` = plain IATA** (e.g. `YYZ`) | `fromEntityId` / `toEntityId` = Skyscanner entity IDs (e.g. `PARI`, `NYCA`, or IATA) |
| Postman collection | (no public collection found) | https://documenter.getpostman.com/view/33975579/2sA3QpBswL |

These are **two different RapidAPI listings** (both Skyscanner-data scrapers, same async
pattern, likely same author). **Our key (`RAPIDAPI_KEY`) is subscribed ONLY to
`flights-sky`.** Hitting `sky-scanner3` with it returns `{"message":"API doesn't exists"}`
(HTTP 404, `x-rapidapi-proxy-response: true` = not subscribed). So: the Postman doc is
useful for *understanding the API family*, but the exact host/paths/params there do NOT
apply to us. Use this file as the source of truth for our integration.

## Auth (all requests)

```
x-rapidapi-host: flights-sky.p.rapidapi.com
x-rapidapi-key:  <RAPIDAPI_KEY>          # from .env; RAPIDAPI_HOST overrides the host
```

Quota lives on the RapidAPI dashboard. Read remaining quota from response headers:
`X-RateLimit-Requests-Limit`, `X-RateLimit-Requests-Remaining`, `X-RateLimit-Requests-Reset`.
(Observed plan: 15,000 requests/cycle.)

## Endpoints we use

### 1. Resolve a place (optional — we usually skip it)
`GET /web/flights/auto-complete?query=<city|iata>`
→ `data[]` of `{PlaceId, PlaceName, CityId, CountryId, GeoContainerId, ...}` where
**`PlaceId` is the IATA code** (e.g. query "Toronto" → `PlaceId: "YYZ"`).
Because `PlaceId` IS the IATA code and the app already has IATA codes for every airport,
**we pass IATA directly to search and skip this call.**

### 2. Round-trip search (async — kicks off a session)
`GET /web/flights/search-roundtrip`

| query param | value |
|---|---|
| `placeIdFrom` / `placeIdTo` | origin / dest **IATA** (e.g. `YYZ`, `PEK`) |
| `departDate` / `returnDate` | `YYYY-MM-DD` |
| `adults` / `children` | passenger counts (price is the **party total**, scaled by both — verified: adults=1→575.78 vs adults=2→1151.57; children=0→1785.64 vs children=2→2398.00 CAD) |
| `currency` / `market` / `locale` | `CAD` / `CA` / `en-US` |
| `cabinClass` | `economy` |

Returns `{data: {context: {status, sessionId}, itineraries: {...}}}`.
- `data.context.status` is usually **`"incomplete"`** on the first call, with a
  `sessionId` to poll. Sometimes already `"complete"`.

### 3. Poll until complete
`GET /web/flights/search-incomplete?sessionId=<sessionId>` (pass the sessionId RAW — do
NOT pre-`quote()` it; `requests`' `params=` encodes once). Repeat until
`data.context.status == "complete"`.

### 4. Parse results
`data.itineraries` is a **dict** with **`buckets[]`** (ids `Best` / `Cheapest` /
`Fastest` / `Direct`), each with **`items[]`** (~8 each). Each item:
- `price.raw` (number, party total) + `price.formatted`
- `legs[]` — `legs[0]` = outbound, `legs[1]` = return. Each leg:
  - `stopCount`, `durationInMinutes`
  - `carriers.marketing[].name` (airline names)
  - `segments[]` — each `{origin/destination.{flightPlaceId,displayCode}, durationInMinutes}`;
    a layover = the connection airport between consecutive segments.

Cheapest = `min(items, key=price.raw)`. We normalize into the shared `get_fare` dict
(`cheapest_cad`, `stops`, `nonstop_cad`, `duration_min`, `nonstop_duration_min`,
`airlines`, `layovers`, `nonstop_airlines`, `source="skyscanner"`).

## ⏱️ Latency — this API is SLOW, size timeouts accordingly

Live-measured for long-haul (YYZ→PEK): the initial `search-roundtrip` call takes
**~27–48s** just to return the (incomplete) session, and individual `search-incomplete`
polls can themselves hang 30s+. Total end-to-end can be **1–2 min per cell**, worse when
the free tier is throttled (heavy back-to-back calls visibly degrade response time).

Therefore (see `app.py` config + `.env.example`):
- `SKYSCANNER_CONNECT_TIMEOUT` (default 10): short connect timeout — a *dead host* fails fast.
- `SKYSCANNER_SEARCH_TIMEOUT` (default 90): the **read/inactivity** timeout for the initial
  search. `requests`' timeout fires only after N seconds of NO bytes — it is NOT a total
  wall-clock cap. So we keep waiting as long as the API is responding, and only give up on
  prolonged silence. **A low cap (the original hardcoded 15s) timed out every call and
  dropped all real long-haul fares — never do that.**
- `SKYSCANNER_POLL_ATTEMPTS` / `_INTERVAL` / `_TIMEOUT` (default 24 / 1.5s / 20s): generous,
  bounded poll budget.
- `SKYSCANNER_POLL_MAX_STALLS` (default 3): a single slow/timed-out poll does NOT abort the
  search; we only bail after this many *consecutive* silences. The session staying
  `"incomplete"` means it's still progressing — keep polling.

The grid fetch is concurrent (`SEARCH_CONCURRENCY`) and results are cached
(`FARE_CACHE_*`, persisted), which hides the per-cell latency for repeat searches.

## Other endpoints (NOT yet used — for future development)

The sibling `sky-scanner3` Postman doc lists more endpoints. Likely-equivalent paths may
exist on `flights-sky` under `/web/flights/` — **verify live before relying on them**:
- `search-one-way`, `search-multi-city` (POST), `search-everywhere`
- `detail` — full detail of one itinerary via `itineraryId` + a `token` from the search
  response (the user referenced `/web/flights/details`). Could replace bucket parsing if
  we need richer per-itinerary data (e.g. real layover gap durations, which the buckets
  don't expose).
- `price-calendar` / `price-calendar-return` — cheapest price per date. **Potentially a
  much cheaper/faster way to populate the flexible-date grid** than one full search per
  cell — worth evaluating to cut both latency and quota.
- `cheapest-one-way`, `airports`, `skyId-list`, `get-config`.

## Gotchas learned
- Endpoints live under `/web/flights/` — `/flights/...` (no `/web/`) returns different
  param errors / 404s on this host.
- `placeId*` must be IATA; skyId (`YTOA`), the numeric entityId (`27536640`), and the
  base64 `presentation.id` were all rejected as "invalid".
- The initial response's `itineraries` may be a list of ID strings while incomplete;
  the full `buckets[].items[]` objects appear once `status == "complete"`.
- Transient `502 Proxy Error` happens — retry the initial call a couple times.
