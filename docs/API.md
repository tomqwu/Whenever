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
      "best": { "dep": "2026-12-15", "ret": "2027-01-07", "chosen_cad": 8123, "chosen": "cheapest", "stops": 1 },
      "grid": [
        [ { "dep": "...", "ret": "...", "cheapest_cad": 8298, "stops": 1,
            "nonstop_cad": 14756, "chosen": "cheapest", "chosen_cad": 8298,
            "source": "travelpayouts", "book": "https://..." } ]
      ]
    }
  ]
}
```

Cells with no API result have `"cheapest_cad": null, "source": "no-data"`.

**Errors**

| Status | Condition |
|--------|-----------|
| 400    | `origin`, `destinations`, or dates are missing/empty/malformed (`{"error": "origin, destinations and dates required"}`) |
