# Whenever ✈️

[![CI](https://github.com/tomqwu/Whenever/actions/workflows/ci.yml/badge.svg)](https://github.com/tomqwu/Whenever/actions/workflows/ci.yml)

A flexible-trip, best-value flight finder for travelers without fixed dates.
Tell it roughly where and when — it searches **multiple cities × multiple dates**, prefers
nonstop when the premium is small, and surfaces the most cost-effective combination.
**All pricing comes from live flight APIs.** Your local **DeepSeek model via Ollama** is used
only to work *on* that collected data — expanding a country into its top cities and analyzing
the fare grid to recommend the best-value trip. The model never invents prices.

---

## Get the code

```bash
git clone https://github.com/tomqwu/Whenever.git
cd Whenever
```

## What it does

- **Departure city** → resolves to an airport code.
- **Arrival country** → DeepSeek expands it into the **top destination cities** (with airport codes).
- **Passengers** → adults + children (with ages), priced **per family** and as a group.
- **Flexible dates** → a 4×4 (or larger) grid of departure × return dates.
- **Best-value matrix** → each cell shows the cheapest fare, stop count, and the nonstop fare
  where one exists; nonstop is auto-picked when its premium is within your threshold.
- **Clickable prices** → every fare deep-links to Kayak/Google Flights to book.
- **AI recommendation** → DeepSeek reads the grid and names the single best trip.

## Prerequisites

1. **Python 3.10+**
2. **Ollama** running locally with your model pulled, e.g.:
   ```bash
   ollama serve            # if not already running
   ollama list             # confirm your model name
   ```
   This app defaults to model `deepseek-v4pro`. Override with `OLLAMA_MODEL` if your tag differs.

## Setup & run

```bash
cd "$(dirname "Whenever")/Whenever"     # the folder containing app.py
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export OLLAMA_HOST=http://localhost:11434
export OLLAMA_MODEL=deepseek-v4pro       # match `ollama list`

# --- a flight API is REQUIRED for pricing (pick one or both) ---
export TRAVELPAYOUTS_TOKEN=...           # easiest: free token, real cached fares + booking links
# export AMADEUS_CLIENT_ID=...           # alternative: Amadeus Self-Service
# export AMADEUS_CLIENT_SECRET=...

python3 app.py
```

Then open **http://localhost:5000** in your browser.

## Flight data (required)

The app fetches **real prices from flight APIs** — it will not fabricate fares. Configure at least one:

- **Travelpayouts / Aviasales** (recommended, easiest): sign up free, get a token, set
  `TRAVELPAYOUTS_TOKEN`. Returns real cached market fares with stop counts and booking deep-links.
- **Amadeus Self-Service**: set `AMADEUS_CLIENT_ID` / `AMADEUS_CLIENT_SECRET`. Tried first when present.

If no provider is set, the grid shows "no fares" and tells you to add a key. Each price still links
out to a live booking search. DeepSeek then analyzes whatever real fares were collected to pick the
best-value trip — it is never the source of a price.

The fare layer is a simple adapter (`get_fare` in `app.py`); Amadeus, Travelpayouts, and Kiwi/Tequila
are wired in this way, and new providers (e.g. Skyscanner) plug in the same way.

## Files

- `app.py` — Flask backend + `run_search` core (search logic shared by routes and tests).
- `templates/index.html` — the UI (form + matrices + clickable prices).
- `requirements.txt` — Python deps.
- `.env.example` — copy to `.env` and fill in.
- `docs/ARCHITECTURE.md` — how the GenAI / fare / booking layers fit together.
- `docs/API.md` — HTTP endpoint reference.
- `CONTRIBUTING.md` — dev setup and how to add a flight provider.

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — design, the fare adapter, the nonstop rule, config table.
- [HTTP API](docs/API.md) — request/response shapes for every endpoint.
- [Contributing](CONTRIBUTING.md) — layout, ground rules, roadmap.

## Roadmap

- Caching layer to respect API rate limits on large date grids.
- More flight providers for cross-checking (Skyscanner, etc.); Kiwi/Tequila is already supported.
- "Watch this trip" daily price-drop alerts.

## License

[MIT](LICENSE) © 2026 Tom Wu
