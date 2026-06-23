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
                │       • Amadeus Self-Service                 │
                │       • Travelpayouts / Aviasales            │
                │       • Kiwi / Tequila                       │
                │       • get_fare() adapter, no AI here       │
                │                                              │
                │  3. Booking links ──▶ provider deep-link or  │
                │                       Kayak search fallback  │
                └──────────────────────────────────────────────┘
```

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
   - calls `get_fare(origin, dest, dep, ret, adults, children)` per cell,
   - applies the **nonstop-preference rule** (pick nonstop if within the premium threshold),
   - finds the best cell per city,
   - calls `build_recommendation()` → Ollama analyzes the grid and names the best value.
5. **`GET /api/health`** — reports Ollama reachability + configured flight providers.

## The fare adapter

`get_fare()` tries each configured provider in order and returns the first real result:

```python
def get_fare(origin, dest, dep, ret, adults, children):
    for provider in (amadeus_fare, travelpayouts_fare, kiwi_fare):
        res = provider(...)
        if res and res.get("cheapest_cad"):
            return res
    return {"cheapest_cad": None, "source": "no-data"}
```

Each provider returns a normalized dict:

```json
{ "cheapest_cad": 8298, "stops": 1, "nonstop_cad": 14756,
  "source": "travelpayouts", "book": "https://..." }
```

Amadeus, Travelpayouts, and Kiwi/Tequila are the providers wired into this tuple today. To add
another (e.g. Skyscanner via RapidAPI), write one function with that signature and add it to the
tuple. No other code changes needed.

## Nonstop-preference rule

For each date cell: if a nonstop exists and `nonstop_cad <= cheapest_cad * (1 + threshold)`,
the nonstop is "chosen"; otherwise the cheapest connection is chosen. Threshold is user-set
(default 25%). This encodes "prefer direct unless it's significantly more expensive."

## Configuration (env)

| Variable | Purpose | Default |
|----------|---------|---------|
| `OLLAMA_HOST` | Ollama base URL | `http://localhost:11434` |
| `OLLAMA_MODEL` | model tag | `deepseek-v4pro` |
| `CURRENCY` | output currency | `cad` |
| `TRAVELPAYOUTS_TOKEN` | Travelpayouts API token | — |
| `AMADEUS_CLIENT_ID` / `AMADEUS_CLIENT_SECRET` | Amadeus creds | — |
| `KIWI_API_KEY` | Kiwi/Tequila API key (free Self-Service tier at tequila.kiwi.com) | — |
| `FARE_CACHE_TTL` | In-memory cache TTL (seconds) for fare results. Set `<= 0` to disable. | `3600` |

## Known limitations

- Travelpayouts returns **cached** market fares (real, but not always live seat-level quotes);
  Amadeus test environment has limited inventory. Click-through booking links show live fares.
- Per-ticket → party scaling treats children at ~full fare for Travelpayouts (Amadeus prices
  children properly). Verify exact totals at booking.
- An in-memory TTL cache (`_fare_cache`, default 3600 s) sits in front of provider calls.
  Only real priced results are cached; no-data sentinels are never stored. Configure via
  `FARE_CACHE_TTL`; set `<= 0` to disable. Cache is process-local and not shared across
  workers or restarts.
