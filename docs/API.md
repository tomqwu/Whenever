# HTTP API reference

Base URL: `http://localhost:5001` (default; set `PORT` to change)

---

### `GET /api/health`

Reports backend readiness.

**Response**
```json
{ "ollama": true, "model": "deepseek-v4pro", "providers": ["travelpayouts"] }
```

---

### `POST /api/top-cities`

Expand a country into its top destination cities (via the local model).

**Body**
```json
{ "country": "China", "n": 6 }
```
**Response**
```json
{ "cities": [ { "city": "Beijing", "iata": "PEK" }, { "city": "Shanghai", "iata": "PVG" } ] }
```

---

### `POST /api/resolve`

Resolve a city name to an airport code.

**Body** `{ "city": "Toronto" }` → **Response** `{ "iata": "YYZ" }`

---

### `POST /api/search`

Run the multi-city × multi-date best-value search.

**Body**
```json
{
  "origin": "YYZ",
  "destinations": [ { "city": "Shanghai", "iata": "PVG" } ],
  "adults": 2,
  "child_ages": [11, 9],
  "families": 3,
  "dep_start": "2026-12-12", "dep_span": 4,
  "ret_start": "2027-01-04", "ret_span": 4,
  "nonstop_threshold": 25
}
```

You may instead pass explicit `dep_dates` / `ret_dates` arrays.

**Response (abridged)**
```json
{
  "origin": "YYZ", "adults": 2, "child_ages": [11, 9], "families": 3,
  "dep_dates": ["2026-12-12", "..."], "ret_dates": ["2027-01-04", "..."],
  "providers": ["travelpayouts"],
  "recommendation": "Best value: Shanghai ...",
  "results": [
    {
      "city": "Shanghai", "iata": "PVG",
      "best": { "dep": "2026-12-15", "ret": "2027-01-07", "chosen_cad": 8123, "chosen": "cheapest", "stops": 1, "duration_min": 875 },
      "grid": [
        [ { "dep": "...", "ret": "...", "cheapest_cad": 8298, "stops": 1,
            "duration_min": 875, "nonstop_cad": 14756, "chosen": "cheapest", "chosen_cad": 8298,
            "source": "travelpayouts", "book": "https://..." } ]
      ]
    }
  ]
}
```

`duration_min` is the total round-trip flight time in minutes for the chosen fare,
or `null` when the provider does not supply a duration (never fabricated).

Cells with no API result have `"cheapest_cad": null, "source": "no-data"`.

**Errors**

| Status | Condition |
|--------|-----------|
| 400    | `origin`, `destinations`, or dates are missing/empty/malformed (`{"error": "origin, destinations and dates required"}`) |

---

### `POST /api/search/stream`

Streaming variant of `/api/search`. Accepts an **identical request body** but returns
results incrementally as `application/x-ndjson` (one JSON object per line, newline-delimited).
Use this endpoint to populate the flight grid live as each cell's fare arrives.

**Body:** identical to `POST /api/search` (see above).

**Response:** `200 application/x-ndjson` — one compact JSON object per line:

#### Line types (in order)

| type | When | Key fields |
|------|------|------------|
| `meta` | First line | `origin`, `adults`, `child_ages`, `families`, `dep_dates`, `ret_dates`, `providers`, `results` (array of `{city,iata}`), `total_cells` |
| `cell` | One per cell, as completed | `dest_index` (index into `meta.results`), plus all fields from a `/api/search` grid cell: `dep`, `ret`, `cheapest_cad`, `stops`, `duration_min`, `nonstop_cad`, `chosen`, `chosen_cad`, `source`, `book` |
| `recommendation` | After all cells | `text` (same string as `/api/search`'s `recommendation` field) |
| `done` | Last line | _(no extra fields)_ |

#### Example stream

```ndjson
{"type":"meta","origin":"YYZ","adults":2,"child_ages":[11,9],"families":3,"dep_dates":["2026-12-12","2026-12-13"],"ret_dates":["2027-01-04","2027-01-05"],"providers":["travelpayouts"],"results":[{"city":"Shanghai","iata":"PVG"}],"total_cells":4}
{"type":"cell","dest_index":0,"dep":"2026-12-13","ret":"2027-01-05","cheapest_cad":8400,"stops":1,"nonstop_cad":null,"chosen":"cheapest","chosen_cad":8400,"source":"travelpayouts","book":"https://..."}
{"type":"cell","dest_index":0,"dep":"2026-12-12","ret":"2027-01-04","cheapest_cad":8000,"stops":1,"nonstop_cad":8500,"chosen":"cheapest","chosen_cad":8000,"source":"travelpayouts","book":"https://..."}
...
{"type":"recommendation","text":"Best value: Shanghai on 2026-12-12 at CA$8,000/family."}
{"type":"done"}
```

Cells arrive in **completion order** (whichever future resolves first), not in dep×ret order.
The `dest_index` field maps each cell back to the correct city in `meta.results`.

**Errors** — returned as plain JSON (not streamed):

| Status | Condition |
|--------|-----------|
| 400 | Same validation as `/api/search`: missing/empty `origin`, `destinations`, or dates |

---

## Price watches

Save a trip so the standalone scheduler (`python scheduler.py`, run via cron) can
re-price it and alert on drops. Watches are stored in the SQLite DB at `WATCH_DB`
(default `whenever_watches.db`).

### `POST /api/watch`

Save a trip to watch. The scheduler baseline (`last_price`/`last_source`) is
**derived server-side** from a real fare lookup (`get_fare`) — per the
REAL-DATA-ONLY guardrail, any client-supplied `last_price`/`last_source` is
**ignored** (a tampered POST cannot inject a fabricated baseline and trigger
bogus drop alerts). Because the user just searched the trip, that lookup is
usually a cache HIT and returns the same real price. If the lookup yields no
data, the baseline is left unset and the scheduler's first run seeds it from a
real fetch.

**Body**
```json
{
  "origin": "YYZ",
  "dest_iata": "PVG",
  "dest_city": "Shanghai",
  "dep_date": "2026-12-12",
  "ret_date": "2027-01-04",
  "adults": 2,
  "child_ages": [11, 9],
  "threshold_pct": 25.0
}
```

`origin`, `dest_iata`, `dep_date`, `ret_date` are **required**; the rest are
optional (`adults` defaults to 2, `threshold_pct` to 25.0). `last_price` /
`last_source` may be sent but are ignored (the baseline is server-derived).

**Response** `{ "id": 1, "ok": true }`

**Errors**

| Status | Condition |
|--------|-----------|
| 400 | Missing/empty `origin`, `dest_iata`, `dep_date`, or `ret_date` |

---

### `GET /api/watch`

List saved (active) watches.

**Response**
```json
{ "watches": [ {
  "id": 1, "origin": "YYZ", "dest_iata": "PVG", "dest_city": "Shanghai",
  "dep_date": "2026-12-12", "ret_date": "2027-01-04", "adults": 2,
  "child_ages": [11, 9], "threshold_pct": 25.0,
  "last_price": 8000, "last_source": "travelpayouts"
} ] }
```

---

### `DELETE /api/watch/<id>`

Remove (deactivate) a watch by id.

**Response** `{ "ok": true }`
