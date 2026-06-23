# Contributing

## Make targets

A `Makefile` wraps the common tasks (run `make` to list them):

```bash
make install     # venv + runtime/dev deps + Playwright chromium
make run         # run the Flask web app
make test        # full suite + 99% coverage gate (same as CI)
make test-unit   # unit tests only
make test-e2e    # Playwright e2e only
make cov         # full suite + HTML coverage report (htmlcov/)
make ci          # alias for the CI gate
make scheduler   # run the price-watch scheduler once
make clean       # remove caches, coverage artifacts, local watch DB
```

## Dev setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in OLLAMA_MODEL and a flight API key
python3 app.py
```

(Or just `make install && make run`.)

## Tests

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium
pytest --cov=app --cov=watch --cov=scheduler --cov=export --cov-fail-under=99    # unit + e2e + coverage gate
```

CI runs the same command on every PR and blocks merge to `main` if it fails or
coverage drops below 99% for `app`, `watch`, `scheduler`, or `export`.

## Project layout

```
Whenever/
├── app.py                 # Flask backend + run_search (core search logic)
├── export.py              # render_csv / render_pdf — consumes run_search output (fpdf2)
├── watch.py               # WatchDB + check_all_watches (price-watch persistence)
├── scheduler.py           # Standalone cron script: runs check_all_watches
├── templates/index.html   # single-page UI
├── requirements.txt       # includes fpdf2 (pure-Python PDF; no system libs required)
├── .env.example
├── docs/
│   ├── ARCHITECTURE.md
│   └── API.md
├── README.md
├── CONTRIBUTING.md
└── LICENSE
```

## Ground rules

- **Prices are real-data only.** The LLM may transform/analyze fares, never originate them.
  Any new pricing path must hit a real flight API and normalize to the `get_fare` dict shape.
- Keep providers behind the `get_fare` adapter (see `docs/ARCHITECTURE.md`).
- Don't commit secrets — use `.env` (git-ignored).

## Price-watch env vars

| Variable | Purpose | Default |
|----------|---------|---------|
| `WATCH_DB` | Path to the SQLite DB file used by `watch.py`/`scheduler.py` | `whenever_watches.db` |
| `WATCH_WEBHOOK_URL` | URL to POST JSON on each price drop (optional) | — |

Run the price-watch checker manually or via cron: `python scheduler.py`.

## Adding a country seed

To pre-configure the destination cities for a country (bypassing the LLM for that country):

1. Open `config/country_seeds.yaml`.
2. Add a new top-level key — the country name **lowercased** (e.g. `japan`).
3. Follow this schema:
   ```yaml
   japan:
     display_name: "Japan"
     candidates:
       - city: "Tokyo"
         iata: "NRT"
         alt_iata: ["HND"]
         priority: 1
         notes: "Optional free-text note"
       - city: "Osaka"
         iata: "KIX"
         priority: 2
       - city: "Sapporo"
         iata: "CTS"
         priority: 5
         optional: true   # shown unchecked; user opts in
   ```
4. **Priority** controls ordering. Required cities (no `optional: true`) up to `n` are returned
   first; optional cities are always appended regardless of `n`.
5. **IATA codes** must be verified against the configured flight provider (Amadeus, Travelpayouts, Kiwi).
   `alt_iata` is metadata only — the primary `iata` is what `get_fare` uses.
6. No code changes needed — the YAML is loaded at startup by `_load_seed_config()` in `app.py`.

## Adding a flight provider

1. Write `def myprovider_fare(origin, dest, dep, ret, adults, children): -> dict|None`.
2. Return `{cheapest_cad, stops, nonstop_cad, source, book}` or `None`.
3. Add it to the tuple in `get_fare()`.

## Ideas / roadmap

- Caching layer to respect API rate limits on large grids.
- Another provider for cross-checking (e.g. Skyscanner via RapidAPI); Amadeus, Travelpayouts, and Kiwi/Tequila are already wired in.
- "Watch this trip" daily price-drop alerts.
