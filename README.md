# Whenever ✈️

[![CI](https://github.com/tomqwu/Whenever/actions/workflows/ci.yml/badge.svg)](https://github.com/tomqwu/Whenever/actions/workflows/ci.yml)

A flexible-trip, best-value flight finder for travelers without fixed dates.
Tell it roughly where and when — it searches **multiple cities × multiple dates**, prefers
nonstop when the premium is small, and surfaces the most cost-effective combination.
**All pricing comes from live flight APIs.** Your local **model via Ollama** (default `qwen3:8b`) is used
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
- **Destination autocomplete** → start typing in the destination field and pick from a
  type-ahead list of **countries or cities** (backed by a bundled airports dataset). Choosing
  a **country** expands it into the **top destination cities**; choosing a **city** adds just
  that city, and you can add **several cities**.
- **Passengers** → adults + children (with ages), priced **per family** and as a group.
- **Flexible dates** → a 4×4 (or larger) grid of departure × return dates.
- **Best-value matrix** → each cell shows the cheapest fare, stop count, and the nonstop fare
  where one exists; nonstop is auto-picked when its premium is within your threshold.
- **Compare all providers (opt-in)** → by default each cell makes one provider call (first real
  price wins). Tick **Compare all providers** to query every configured provider per cell and keep
  the cheapest, showing the runners-up as a subtle "also: SOURCE $PRICE" line. It's slower and uses
  more provider quota, so it's off by default.
- **Clickable prices** → every fare deep-links to Kayak/Google Flights to book.
- **AI recommendation** → the model reads the grid and names the single best trip.
- **Export results** → after a search, download the fare matrix as a PDF or CSV from the UI.
- **Shareable searches** → Searches are shareable — after running, copy the link from the **Copy link** button to share a prefilled, auto-run search.
- **Watch a trip** → after a search, tap **☆ Watch** on any priced city to save its best trip for price-drop monitoring; run `python scheduler.py` (e.g. via cron) to re-price all watches and alert when a fare drops.
- **Mobile-friendly** → the form stacks and the date matrix scrolls horizontally inside its own container on small screens, so the app stays usable on a phone without sideways page-scroll.
- **Friendly empty/error states** → cells with no fare show a calm "—" (with a "no fare found for these dates" tooltip) instead of an alarming error; a city or search that finds nothing shows a short "try different dates or add a provider key" note; and with no flight provider configured a clear banner explains how to add one.

## Prerequisites

1. **Python 3.10+**
2. **Ollama** running locally with your model pulled, e.g.:
   ```bash
   ollama serve            # if not already running
   ollama list             # confirm your model name
   ```
   This app defaults to model `qwen3:8b`. Override with `OLLAMA_MODEL` if your tag differs.

## Setup & run

```bash
cd "$(dirname "Whenever")/Whenever"     # the folder containing app.py
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export OLLAMA_HOST=http://localhost:11434
export OLLAMA_MODEL=qwen3:8b             # match `ollama list`

# --- a flight API is REQUIRED for pricing (pick one or both) ---
export TRAVELPAYOUTS_TOKEN=...           # easiest: free token, real cached fares + booking links
# export AMADEUS_CLIENT_ID=...           # alternative: Amadeus Self-Service
# export AMADEUS_CLIENT_SECRET=...

python3 app.py
```

Then open **http://localhost:5001** in your browser. (Port 5001 avoids the macOS
AirPlay Receiver, which holds 5000. Override with the `PORT` env var.)

## Flight data (required)

The app fetches **real prices from flight APIs** — it will not fabricate fares. Configure at least one:

- **Travelpayouts / Aviasales** (recommended, easiest): sign up free, get a token, set
  `TRAVELPAYOUTS_TOKEN`. Returns real cached market fares with stop counts and booking deep-links.
- **Amadeus Self-Service**: set `AMADEUS_CLIENT_ID` / `AMADEUS_CLIENT_SECRET`. Tried first when present.

If no provider is set, the grid shows "no fares" and tells you to add a key. Each price still links
out to a live booking search. The model then analyzes whatever real fares were collected to pick the
best-value trip — it is never the source of a price.

The fare layer is a simple adapter (`get_fare` in `app.py`); Amadeus, Travelpayouts, and Kiwi/Tequila
are wired in this way, and new providers (e.g. Skyscanner) plug in the same way.

### Demo / sample mode (no key required)

Want to explore the UI without a provider key? Set `DEMO_MODE=1` (default **off**). The app then
serves **clearly-labeled SAMPLE fares** — deterministic, obviously-fake numbers (carrier `DemoAir`)
generated locally, **not real prices and never from a flight API**. A prominent persistent
**“⚠️ DEMO DATA — sample fares, NOT real prices”** banner stays on screen, each cell is tagged
`demo`, and the AI summary is prefixed `(DEMO …)`.

This is the **only** exception to the real-data-only rule, and only because it is explicit and
unmistakable: demo data is **never** a silent fallback (turn it off and a missing/failed provider
just shows “no fares”), demo and real **never mix**, and demo fares are **never** written to the
real fare cache. Leave `DEMO_MODE` off (blank) for any real pricing.

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
