#!/usr/bin/env python3
"""
Whenever - flexible-trip best-value flight finder.

Smart features (top-cities expansion, best-value recommendation) run on a local
LLM via Ollama (default model: deepseek-v4pro). Fares come from Amadeus if
credentials are set, otherwise from clearly-labeled AI estimates. Every price
deep-links to a real Kayak search for booking.
"""
import os, re, json, time, datetime as dt, logging, concurrent.futures, sqlite3
from functools import lru_cache
import requests
import yaml
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, Response, stream_with_context
import export
import watch

load_dotenv()  # load .env if present; real shell env vars take precedence (override=False default)

_log = logging.getLogger(__name__)

# ----------------------- country seed config -----------------------
def _load_seed_config():
    path = os.path.join(os.path.dirname(__file__), "config", "country_seeds.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            _log.warning("country_seeds.yaml parsed to non-dict; falling back to LLM-only")
            return {}
        return data
    except FileNotFoundError:
        _log.warning("config/country_seeds.yaml not found; falling back to LLM-only for all countries")
        return {}
    except Exception as exc:
        _log.warning("Failed to parse country_seeds.yaml (%s); falling back to LLM-only", exc)
        return {}

_SEED_CONFIG: dict = _load_seed_config()

# ----------------------------- config -----------------------------
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "deepseek-v4pro")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY")
AMADEUS_ID = os.environ.get("AMADEUS_CLIENT_ID")
AMADEUS_SECRET = os.environ.get("AMADEUS_CLIENT_SECRET")
# Travelpayouts / Aviasales token (free signup) -> real cached market fares + booking links
TRAVELPAYOUTS_TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN")
KIWI_API_KEY = os.environ.get("KIWI_API_KEY")
SERPAPI_KEY = os.environ.get("SERPAPI_KEY")
CURRENCY = os.environ.get("CURRENCY", "cad").lower()
FARE_CACHE_TTL = int(os.environ.get("FARE_CACHE_TTL", "3600"))
SEARCH_CONCURRENCY = int(os.environ.get("SEARCH_CONCURRENCY", "8"))
# Hard cap on search grid size. Each cell = one provider API call. A value <= 0 disables the cap.
MAX_SEARCH_CELLS = int(os.environ.get("MAX_SEARCH_CELLS", "200"))
# Generous per-direction day cap. Bounds dep_span/ret_span (and date_range count)
# BEFORE expansion so a malformed huge span can't allocate millions of dates. The
# form max is small; this is a safety ceiling, not the typical value.
MAX_DATE_SPAN = int(os.environ.get("MAX_DATE_SPAN", "60"))
# Dev-server port. Default 5001 to avoid macOS AirPlay Receiver, which holds 5000.
PORT = int(os.environ.get("PORT", "5001"))

def providers_configured():
    p = []
    if SERPAPI_KEY:
        p.append("serpapi")
    if AMADEUS_ID and AMADEUS_SECRET:
        p.append("amadeus")
    if TRAVELPAYOUTS_TOKEN:
        p.append("travelpayouts")
    if KIWI_API_KEY:
        p.append("kiwi")
    return p

app = Flask(__name__)

# ----------------------- fare cache -----------------------
# Maps (origin, dest, dep, ret, adults, children) -> (expiry_epoch, result_dict).
# Only real priced results (cheapest_cad is truthy) are stored; no-data sentinels
# are never cached so a transient provider failure is not persisted.
_fare_cache: dict = {}

# ----------------------------- Ollama -----------------------------
def _ollama_headers():
    """Return Bearer auth header if OLLAMA_API_KEY is set, else empty dict."""
    return {"Authorization": f"Bearer {OLLAMA_API_KEY}"} if OLLAMA_API_KEY else {}


def ollama_chat(prompt, system=None, timeout=120):
    """Call local Ollama; return raw text. Strips <think> reasoning blocks."""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    r = requests.post(
        f"{OLLAMA_HOST}/api/chat",
        json={"model": OLLAMA_MODEL, "messages": msgs, "stream": False,
              "options": {"temperature": 0.2}},
        headers=_ollama_headers(),
        timeout=timeout,
    )
    r.raise_for_status()
    txt = r.json().get("message", {}).get("content", "")
    # remove deepseek-style reasoning
    txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S)
    return txt.strip()

def extract_json(text):
    """Pull the first JSON object/array out of an LLM response."""
    text = re.sub(r"```(?:json)?", "", text)
    m = re.search(r"(\[.*\]|\{.*\})", text, flags=re.S)
    if not m:
        raise ValueError("no JSON found in model output")
    return json.loads(m.group(1))

def ollama_ok():
    try:
        requests.get(f"{OLLAMA_HOST}/api/tags", headers=_ollama_headers(), timeout=3).raise_for_status()
        return True
    except Exception:
        return False

# ----------------------- top cities (seed-first, then GenAI) -----------------------
@lru_cache(maxsize=128)
def top_cities(country, n=6):
    """Return top destination cities for *country*.

    Seed path (preferred):
        If ``_SEED_CONFIG`` has an entry for ``country.lower()``, build the
        result from the YAML candidates:
        - Required cities (``optional`` falsy) up to ``n``, sorted by priority.
        - ALL optional cities (``optional: true``) appended after, regardless of ``n``.
        Each entry: ``{city, iata, optional: bool, priority: int}``.

    LLM fallback:
        If no seed entry exists, call ``ollama_chat`` and return up to ``n`` entries
        each annotated with ``optional: False``.
    """
    key = country.lower()
    seed = _SEED_CONFIG.get(key)
    if seed:
        candidates = seed.get("candidates", [])
        # Sort all candidates by priority
        candidates = sorted(candidates, key=lambda c: c.get("priority", 999))
        required = [c for c in candidates if not c.get("optional", False)]
        optional = [c for c in candidates if c.get("optional", False)]

        out = []
        for c in required[:n]:
            out.append({
                "city": str(c["city"]).strip(),
                "iata": str(c["iata"]).strip().upper()[:3],
                "optional": False,
                "priority": int(c.get("priority", 0)),
            })
        for c in optional:
            out.append({
                "city": str(c["city"]).strip(),
                "iata": str(c["iata"]).strip().upper()[:3],
                "optional": True,
                "priority": int(c.get("priority", 0)),
            })
        return out

    # LLM fallback
    prompt = (
        f"List the top {n} destination cities in {country} for international leisure "
        f"travelers. Return ONLY a JSON array; each item: "
        f'{{"city":"<name>","iata":"<primary international airport IATA code>"}}. '
        f"No commentary."
    )
    data = extract_json(ollama_chat(prompt))
    out = []
    for d in data:
        if isinstance(d, dict) and d.get("iata"):
            out.append({
                "city": str(d.get("city", "")).strip(),
                "iata": str(d["iata"]).strip().upper()[:3],
                "optional": False,
            })
    return out[:n]

@lru_cache(maxsize=256)
def resolve_airport(city):
    """City name -> IATA via the model (cached)."""
    prompt = (f'Give ONLY the primary international airport IATA code for "{city}". '
              f'Return JSON like {{"iata":"XXX"}}.')
    try:
        d = extract_json(ollama_chat(prompt))
        return str(d.get("iata", "")).strip().upper()[:3]
    except Exception:
        return ""

# --------------------------- fares --------------------------------
_amadeus_token = {"value": None, "exp": 0}

def amadeus_token():
    if not (AMADEUS_ID and AMADEUS_SECRET):
        return None
    if _amadeus_token["value"] and time.time() < _amadeus_token["exp"] - 30:
        return _amadeus_token["value"]
    r = requests.post(
        "https://test.api.amadeus.com/v1/security/oauth2/token",
        data={"grant_type": "client_credentials",
              "client_id": AMADEUS_ID, "client_secret": AMADEUS_SECRET},
        timeout=20,
    )
    r.raise_for_status()
    j = r.json()
    _amadeus_token["value"] = j["access_token"]
    _amadeus_token["exp"] = time.time() + j.get("expires_in", 1799)
    return _amadeus_token["value"]

def amadeus_fare(origin, dest, dep, ret, adults, children):
    tok = amadeus_token()
    if not tok:
        return None
    params = {
        "originLocationCode": origin, "destinationLocationCode": dest,
        "departureDate": dep, "returnDate": ret,
        "adults": adults, "children": children,
        "currencyCode": "CAD", "max": 8,
    }
    r = requests.get("https://test.api.amadeus.com/v2/shopping/flight-offers",
                     headers={"Authorization": f"Bearer {tok}"},
                     params=params, timeout=30)
    if r.status_code != 200:
        return None
    offers = r.json().get("data", [])
    if not offers:
        return None
    def stops(o):
        return max(len(s["segments"]) - 1 for s in o["itineraries"])
    cheapest = min(offers, key=lambda o: float(o["price"]["grandTotal"]))
    nonstops = [o for o in offers if stops(o) == 0]
    ns = min(nonstops, key=lambda o: float(o["price"]["grandTotal"])) if nonstops else None
    return {
        "cheapest_cad": round(float(cheapest["price"]["grandTotal"])),
        "stops": stops(cheapest),
        "nonstop_cad": round(float(ns["price"]["grandTotal"])) if ns else None,
        "source": "amadeus",
    }

def travelpayouts_fare(origin, dest, dep, ret, adults, children):
    """Real cached market fares from Travelpayouts/Aviasales. Price is PER TICKET;
    we scale to the whole party. Returns booking deep-link."""
    if not TRAVELPAYOUTS_TOKEN:
        return None
    params = {
        "origin": origin, "destination": dest,
        "departure_at": dep, "return_at": ret,
        "currency": CURRENCY, "sorting": "price", "direct": "false",
        "limit": 30, "one_way": "false", "token": TRAVELPAYOUTS_TOKEN,
    }
    r = requests.get("https://api.travelpayouts.com/aviasales/v3/prices_for_dates",
                     params=params, timeout=30)
    if r.status_code != 200:
        return None
    data = r.json().get("data", [])
    if not data:
        return None
    pax = adults + children  # per-ticket price -> party total (children priced ~ full here)
    def total(o):
        return float(o["price"]) * pax
    def is_ns(o):
        return int(o.get("transfers", 9)) == 0 and int(o.get("return_transfers", 9)) == 0
    cheapest = min(data, key=lambda o: float(o["price"]))
    nonstops = [o for o in data if is_ns(o)]
    ns = min(nonstops, key=lambda o: float(o["price"])) if nonstops else None
    link = cheapest.get("link")
    book = ("https://www.aviasales.com" + link) if link and link.startswith("/") else None
    return {
        "cheapest_cad": round(total(cheapest)),
        "stops": int(cheapest.get("transfers", 0)) + int(cheapest.get("return_transfers", 0)),
        "nonstop_cad": round(total(ns)) if ns else None,
        "source": "travelpayouts",
        "book": book,
    }

def kiwi_fare(origin, dest, dep, ret, adults, children):
    """Real fares from Kiwi/Tequila v2/search API. Returns booking deep-link."""
    if not KIWI_API_KEY:
        return None
    # Tequila expects dates in dd/mm/YYYY format
    try:
        dep_fmt = dt.date.fromisoformat(dep).strftime("%d/%m/%Y")
        ret_fmt = dt.date.fromisoformat(ret).strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        dep_fmt = dep
        ret_fmt = ret
    params = {
        "fly_from": origin, "fly_to": dest,
        "date_from": dep_fmt, "date_to": dep_fmt,
        "return_from": ret_fmt, "return_to": ret_fmt,
        "adults": adults, "children": children,
        "curr": "CAD", "limit": 20,
    }
    r = requests.get(
        "https://tequila-api.kiwi.com/v2/search",
        headers={"apikey": KIWI_API_KEY},
        params=params,
        timeout=30,
    )
    if r.status_code != 200:
        return None
    try:
        data = r.json().get("data", [])
    except Exception:
        return None
    if not data:
        return None

    def parse_itin(itin):
        """Return (price_int, max_stops, is_nonstop, deep_link) or raise."""
        price = int(round(float(itin["price"])))
        route = itin["route"]
        outbound = [seg for seg in route if seg["return"] == 0]
        inbound = [seg for seg in route if seg["return"] == 1]
        out_stops = len(outbound) - 1
        in_stops = len(inbound) - 1
        max_stops = max(out_stops, in_stops)
        is_ns = max_stops == 0
        return price, max_stops, is_ns, itin.get("deep_link")

    try:
        parsed = [parse_itin(it) for it in data]
    except Exception:
        return None

    cheapest = min(parsed, key=lambda x: x[0])
    nonstops = [p for p in parsed if p[2]]
    ns = min(nonstops, key=lambda x: x[0]) if nonstops else None

    return {
        "cheapest_cad": cheapest[0],
        "stops": cheapest[1],
        "nonstop_cad": ns[0] if ns else None,
        "source": "kiwi",
        "book": cheapest[3],
    }


def serpapi_fare(origin, dest, dep, ret, adults, children):
    """Live Google Flights fares via SerpApi. Price is the PARTY TOTAL in CAD (not per-ticket)."""
    if not SERPAPI_KEY:
        return None
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": dest,
        "outbound_date": dep,
        "return_date": ret,
        "currency": "CAD",
        "adults": adults,
        "children": children,
        "type": 1,
        "sort_by": 2,
        "api_key": SERPAPI_KEY,
    }
    r = requests.get("https://serpapi.com/search.json", params=params, timeout=30)
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    if data.get("error"):
        return None
    flights = (data.get("best_flights") or []) + (data.get("other_flights") or [])
    if not flights:
        return None
    # Real Google Flights responses sometimes include entries with NO usable price
    # (missing key or price: None). Filter those out so a few priceless entries don't
    # discard the whole result — keep the priced ones and pick the cheapest among them.
    flights = [f for f in flights if isinstance(f.get("price"), (int, float))]
    if not flights:
        return None
    try:
        cheapest = min(flights, key=lambda f: f["price"])
        cheapest_cad = round(float(cheapest["price"]))
        # stops reflects the OUTBOUND leg only: SerpApi's round-trip (type=1) response
        # describes only the outbound choice (Google's first-screen view). This is a
        # reasonable display approximation.
        stops = len(cheapest.get("layovers") or [])
    except Exception:
        return None
    # SerpApi's round-trip response only describes the outbound leg, so we cannot
    # confirm a true round-trip nonstop without an extra departure_token request;
    # to avoid mislabeling we don't claim nonstop for this provider.
    nonstop_cad = None
    return {
        "cheapest_cad": cheapest_cad,
        "stops": stops,
        "nonstop_cad": nonstop_cad,
        "source": "serpapi",
        "book": None,
    }


def _get_fare_uncached(origin, dest, dep, ret, adults, children):
    """Try each configured provider in order; return first real priced result."""
    for provider in (serpapi_fare, amadeus_fare, travelpayouts_fare, kiwi_fare):
        try:
            res = provider(origin, dest, dep, ret, adults, children)
            if res and res.get("cheapest_cad"):
                return res
        except Exception:
            continue
    return {"cheapest_cad": None, "stops": None, "nonstop_cad": None, "source": "no-data"}


def get_fare(origin, dest, dep, ret, adults, children):
    """Collect REAL pricing from configured flight APIs. No AI here.

    Results with real prices are cached in _fare_cache for FARE_CACHE_TTL seconds.
    No-data sentinels are never cached.  Set FARE_CACHE_TTL <= 0 to disable caching.
    """
    if FARE_CACHE_TTL <= 0:
        return _get_fare_uncached(origin, dest, dep, ret, adults, children)

    key = (origin, dest, dep, ret, adults, children)
    now = time.time()
    entry = _fare_cache.get(key)
    if entry is not None:
        expiry, cached_result = entry
        if expiry > now:
            return cached_result

    result = _get_fare_uncached(origin, dest, dep, ret, adults, children)
    if result.get("cheapest_cad"):
        _fare_cache[key] = (now + FARE_CACHE_TTL, result)
    return result

# ------------------------ booking links ---------------------------
def kayak_link(origin, dest, dep, ret, adults, child_ages):
    base = f"https://www.kayak.com/flights/{origin}-{dest}/{dep}/{ret}/{adults}adults"
    if child_ages:
        base += "/children-" + "-".join(str(a) for a in child_ages)
    return base

# --------------------------- routes -------------------------------
@app.route("/")
def index():
    return render_template("index.html",
                           model=OLLAMA_MODEL,
                           providers=providers_configured())

@app.route("/api/health")
def health():
    return jsonify({"ollama": ollama_ok(), "model": OLLAMA_MODEL,
                    "providers": providers_configured()})

@app.route("/api/top-cities", methods=["POST"])
def api_top_cities():
    body = request.get_json(force=True)
    country = (body.get("country") or "").strip()
    n = int(body.get("n", 6))
    if not country:
        return jsonify({"error": "country required"}), 400
    try:
        return jsonify({"cities": top_cities(country, n)})
    except Exception as e:
        return jsonify({"error": f"model error: {e}"}), 502

@app.route("/api/resolve", methods=["POST"])
def api_resolve():
    body = request.get_json(force=True)
    city = (body.get("city") or "").strip()
    code = resolve_airport(city) if city else ""
    return jsonify({"iata": code})

def date_range(start_iso, count):
    if not start_iso:
        return []
    try:
        d = dt.date.fromisoformat(start_iso)
    except (ValueError, TypeError):
        return []
    # Defensively clamp count so any caller is protected from a huge/negative
    # span building millions of dates (or a negative range). Floor at 0.
    try:
        count = max(0, min(int(count), MAX_DATE_SPAN))
    except (ValueError, TypeError):
        return []
    return [(d + dt.timedelta(days=i)).isoformat() for i in range(count)]

def _build_cell(origin, code, dep, ret, adults, child_ages, fare, threshold):
    """Build the cell dict for a single dep×ret combo.

    fare: dict from get_fare() with keys cheapest_cad, stops, nonstop_cad, source, book.
    threshold: float fraction (e.g. 0.25 for 25 %).
    Returns dict with keys: dep, ret, cheapest_cad, stops, nonstop_cad, chosen, chosen_cad, source, book.
    """
    cheap = fare.get("cheapest_cad")
    ns = fare.get("nonstop_cad")
    chosen = "cheapest"
    chosen_cad = cheap
    if ns and cheap and ns <= cheap * (1 + threshold):
        chosen, chosen_cad = "nonstop", ns
    return {
        "dep": dep, "ret": ret,
        "cheapest_cad": cheap, "stops": fare.get("stops"),
        "nonstop_cad": ns, "chosen": chosen, "chosen_cad": chosen_cad,
        "source": fare.get("source"),
        "book": fare.get("book") or kayak_link(origin, code, dep, ret, adults, child_ages),
    }


def run_search(origin, dests, adults, child_ages, dep_dates, ret_dates,
               threshold_pct=25, families=1):
    """Core best-value search shared by the web route and the CLI.

    origin: IATA str; dests: list of {"city","iata"}; child_ages: list[int];
    threshold_pct: nonstop premium % (e.g. 25). Returns dict with keys
    origin, adults, child_ages, families, dep_dates, ret_dates, results,
    recommendation, providers.

    All dep×ret cells across all destinations are fetched concurrently via a
    bounded ThreadPoolExecutor (SEARCH_CONCURRENCY workers, default 8).
    Results are assembled into the original dest/dep/ret order so the output
    is identical to the former sequential implementation.
    """
    children = len(child_ages)
    threshold = threshold_pct / 100.0

    # Build the flat task list preserving (dest_idx, dep_idx, ret_idx) positions.
    tasks = []
    for di, dest in enumerate(dests):
        code = (dest.get("iata") or "").upper()[:3]
        for dpi, dep in enumerate(dep_dates):
            for ri, ret in enumerate(ret_dates):
                tasks.append((di, dpi, ri, code, dep, ret))

    # Fetch all cells concurrently.
    fare_results: dict = {}  # (di, dpi, ri) -> fare dict
    workers = max(1, SEARCH_CONCURRENCY)

    def _fetch(task):
        di, dpi, ri, code, dep, ret = task
        return (di, dpi, ri), get_fare(origin, code, dep, ret, adults, children)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for key, fare in pool.map(_fetch, tasks):
            fare_results[key] = fare

    # Assemble results in the original dest order.
    results = []
    for di, dest in enumerate(dests):
        code = (dest.get("iata") or "").upper()[:3]
        grid = []
        for dpi, dep in enumerate(dep_dates):
            row = []
            for ri, ret in enumerate(ret_dates):
                f = fare_results[(di, dpi, ri)]
                row.append(_build_cell(origin, code, dep, ret, adults, child_ages, f, threshold))
            grid.append(row)
        flat = [c for r in grid for c in r if c["chosen_cad"]]
        best = min(flat, key=lambda c: c["chosen_cad"]) if flat else None
        results.append({"city": dest.get("city"), "iata": code,
                        "grid": grid, "best": best})

    recommendation = build_recommendation(origin, results, adults, child_ages, families)
    return {
        "origin": origin, "adults": adults, "child_ages": child_ages,
        "families": families, "dep_dates": dep_dates, "ret_dates": ret_dates,
        "results": results, "recommendation": recommendation,
        "providers": providers_configured(),
    }


def _span(v, default=4):
    """Safely parse a date-span value to an int clamped to [1, MAX_DATE_SPAN].

    Non-numeric or None values (e.g. a stale ``"abc"``) fall back to ``default``
    instead of raising, so a garbage span on the fallback path degrades to the
    default span rather than producing a 500. Falsy values (0, "") also fall
    back to ``default``, preserving the prior ``or 4`` semantics. ``date_range``
    re-clamps the count internally as a backstop.
    """
    if not v:
        return max(1, min(default, MAX_DATE_SPAN))
    try:
        n = int(v)
    except (ValueError, TypeError):
        n = default
    return max(1, min(n, MAX_DATE_SPAN))


def _search_args_from_body(b: dict):
    """Parse a request body dict into run_search keyword arguments.

    Returns a dict of kwargs suitable for ``run_search(**args)``, or None if
    the body is invalid (missing origin, destinations, or dates — same
    conditions as the existing 400 guard in api_search).

    This is the single authoritative parsing path shared by api_search,
    /api/export/csv, and /api/export/pdf.
    """
    origin = (b.get("origin") or "").upper()[:3]
    dests = b.get("destinations") or []
    adults = int(b.get("adults", 2))
    child_ages = [int(a) for a in (b.get("child_ages") or [])]
    # Span is only consulted on the FALLBACK path (when explicit dep_dates/
    # ret_dates are not supplied). When explicit date arrays are present, the
    # span fields are unused and must NOT be parsed at all, so a stale/garbage
    # span (e.g. dep_span="abc") is ignored rather than raising a 500.
    dep_dates = b.get("dep_dates")
    if not dep_dates:
        dep_dates = date_range(b.get("dep_start", ""), _span(b.get("dep_span", 4)))
    ret_dates = b.get("ret_dates")
    if not ret_dates:
        ret_dates = date_range(b.get("ret_start", ""), _span(b.get("ret_span", 4)))
    threshold_pct = float(b.get("nonstop_threshold", 25))
    families = int(b.get("families", 1))

    if not origin or not dests or not dep_dates or not ret_dates:
        return None

    return dict(
        origin=origin, dests=dests, adults=adults, child_ages=child_ages,
        dep_dates=dep_dates, ret_dates=ret_dates,
        threshold_pct=threshold_pct, families=families,
    )


_SEARCH_ARGS_400 = {"error": "origin, destinations and dates required"}


def _check_cell_cap(dests, dep_dates, ret_dates):
    """Return a 400 JSON response if the search exceeds MAX_SEARCH_CELLS, else None.

    Each cell = one provider API call (dest × dep_date × ret_date).
    A MAX_SEARCH_CELLS value <= 0 disables the cap entirely.
    """
    if MAX_SEARCH_CELLS <= 0:
        return None
    total_cells = len(dests) * len(dep_dates) * len(ret_dates)
    if total_cells > MAX_SEARCH_CELLS:
        return (
            jsonify({
                "error": (
                    f"search too large: {total_cells} cells exceeds limit {MAX_SEARCH_CELLS};"
                    " reduce cities or date ranges"
                )
            }),
            400,
        )
    return None


@app.route("/api/search", methods=["POST"])
def api_search():
    b = request.get_json(force=True)
    args = _search_args_from_body(b)
    if args is None:
        return jsonify(_SEARCH_ARGS_400), 400
    cap_err = _check_cell_cap(args["dests"], args["dep_dates"], args["ret_dates"])
    if cap_err is not None:
        return cap_err
    result = run_search(**args)
    return jsonify(result)


@app.route("/api/search/stream", methods=["POST"])
def api_search_stream():
    """POST /api/search/stream — same body as /api/search.

    Returns application/x-ndjson:
      {"type":"meta", ...}           (first)
      {"type":"cell", ...}           (one per cell, as completed)
      {"type":"recommendation", ...} (after all cells)
      {"type":"done"}                (last)

    If the body is invalid, returns 400 JSON (not streamed).
    """
    b = request.get_json(force=True)
    args = _search_args_from_body(b)
    if args is None:
        return jsonify(_SEARCH_ARGS_400), 400
    cap_err = _check_cell_cap(args["dests"], args["dep_dates"], args["ret_dates"])
    if cap_err is not None:
        return cap_err

    # Capture all args now — generator must not touch `request` after this point.
    origin = args["origin"]
    dests = args["dests"]
    adults = args["adults"]
    child_ages = args["child_ages"]
    dep_dates = args["dep_dates"]
    ret_dates = args["ret_dates"]
    threshold_pct = args["threshold_pct"]
    families = args["families"]

    @stream_with_context
    def generate():
        children = len(child_ages)
        threshold = threshold_pct / 100.0

        # Build the flat task list: (dest_index, code, dep, ret)
        tasks = []
        for di, dest in enumerate(dests):
            code = (dest.get("iata") or "").upper()[:3]
            for dep in dep_dates:
                for ret in ret_dates:
                    tasks.append((di, code, dep, ret))

        total_cells = len(tasks)

        # --- meta line ---
        meta = {
            "type": "meta",
            "origin": origin,
            "adults": adults,
            "child_ages": child_ages,
            "families": families,
            "nonstop_threshold": threshold_pct,
            "dep_dates": dep_dates,
            "ret_dates": ret_dates,
            "providers": providers_configured(),
            "results": [{"city": d.get("city"), "iata": (d.get("iata") or "").upper()[:3]}
                        for d in dests],
            "total_cells": total_cells,
        }
        yield json.dumps(meta) + "\n"

        # --- cell lines (as completed) ---
        # Accumulate cells in order for the final recommendation.
        # cells_by_dest[di] collects bare cell dicts (as returned by _build_cell).
        cells_by_dest: dict = {di: [] for di in range(len(dests))}

        def _fetch(task):
            di, code, dep, ret = task
            try:
                fare = get_fare(origin, code, dep, ret, adults, children)
            except Exception:
                fare = {"cheapest_cad": None, "stops": None, "nonstop_cad": None, "source": "no-data"}
            cell = _build_cell(origin, code, dep, ret, adults, child_ages, fare, threshold)
            return di, cell

        workers = max(1, SEARCH_CONCURRENCY)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_fetch, t): t for t in tasks}
            for fut in concurrent.futures.as_completed(futures):
                di, cell = fut.result()
                cells_by_dest[di].append(cell)
                line = {"type": "cell", "dest_index": di, **cell}
                yield json.dumps(line) + "\n"

        # --- recommendation line ---
        # Reconstruct `results` in dest order (same shape as run_search output)
        # so the streamed recommendation is DETERMINISTIC and identical to what
        # /api/search (run_search) would produce. Cells arrive in as_completed
        # order, so we must re-index them by (dep, ret) and rebuild the grid in
        # the ORIGINAL dep×ret order; `best` is then min(chosen_cad) over that
        # ordered flat list, so ties resolve to the SAME cell run_search picks.
        results = []
        for di, dest in enumerate(dests):
            code = (dest.get("iata") or "").upper()[:3]
            grid_cells = {(c["dep"], c["ret"]): c for c in cells_by_dest[di]}
            grid = [
                [grid_cells.get((dep, ret), {
                    "dep": dep, "ret": ret,
                    "cheapest_cad": None, "stops": None, "nonstop_cad": None,
                    "chosen": "cheapest", "chosen_cad": None, "source": "no-data",
                    "book": kayak_link(origin, code, dep, ret, adults, child_ages),
                }) for ret in ret_dates]
                for dep in dep_dates
            ]
            flat = [c for row in grid for c in row if c["chosen_cad"]]
            best = min(flat, key=lambda c: c["chosen_cad"]) if flat else None
            results.append({
                "city": dest.get("city"), "iata": code,
                "grid": grid, "best": best,
            })

        rec_text = build_recommendation(origin, results, adults, child_ages, families)
        yield json.dumps({"type": "recommendation", "text": rec_text}) + "\n"

        # --- done line ---
        yield json.dumps({"type": "done"}) + "\n"

    return Response(generate(), mimetype="application/x-ndjson")


@app.route("/api/export/csv", methods=["POST"])
def api_export_csv():
    b = request.get_json(force=True)
    args = _search_args_from_body(b)
    if args is None:
        return jsonify(_SEARCH_ARGS_400), 400
    cap_err = _check_cell_cap(args["dests"], args["dep_dates"], args["ret_dates"])
    if cap_err is not None:
        return cap_err
    result = run_search(**args)
    csv_text = export.render_csv(result)
    return Response(
        csv_text,
        status=200,
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="whenever-matrix.csv"'},
    )


@app.route("/api/export/pdf", methods=["POST"])
def api_export_pdf():
    b = request.get_json(force=True)
    args = _search_args_from_body(b)
    if args is None:
        return jsonify(_SEARCH_ARGS_400), 400
    cap_err = _check_cell_cap(args["dests"], args["dep_dates"], args["ret_dates"])
    if cap_err is not None:
        return cap_err
    result = run_search(**args)
    pdf_bytes = export.render_pdf(result)
    return Response(
        pdf_bytes,
        status=200,
        mimetype="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="whenever-matrix.pdf"'},
    )

# --------------------------- price watches ------------------------
def _watch_db():
    """Open a fresh WatchDB for the current request.

    A new sqlite connection is opened (and closed) per request to avoid reusing
    a single connection across Flask's worker threads. The path is resolved the
    same way scheduler.py does: WATCH_DB env, else whenever_watches.db.
    """
    return watch.WatchDB(os.environ.get("WATCH_DB") or "whenever_watches.db")


def _watch_to_json(row):
    """Project a watch row dict to a JSON-friendly subset for the UI."""
    return {
        "id": row.get("id"),
        "origin": row.get("origin"),
        "dest_iata": row.get("dest_iata"),
        "dest_city": row.get("dest_city"),
        "dep_date": row.get("dep_date"),
        "ret_date": row.get("ret_date"),
        "adults": row.get("adults"),
        "child_ages": row.get("child_ages") or [],
        "threshold_pct": row.get("threshold_pct"),
        "last_price": row.get("last_price"),
        "last_source": row.get("last_source"),
    }


@app.route("/api/watch", methods=["POST"])
def api_watch_add():
    # request.get_json() can return None (no body) or a non-dict (client posts
    # `null`, `[]`, a bare string/number); a subsequent b.get(...) would raise
    # AttributeError -> 500. Reject anything that isn't a JSON object up front.
    b = request.get_json(silent=True)
    if not isinstance(b, dict):
        return jsonify({"error": "watch payload required"}), 400
    origin = (b.get("origin") or "").strip().upper()
    dest_iata = (b.get("dest_iata") or "").strip().upper()
    dep_date = (b.get("dep_date") or "").strip()
    ret_date = (b.get("ret_date") or "").strip()
    if not origin or not dest_iata or not dep_date or not ret_date:
        return jsonify({"error": "origin, dest_iata, dep_date and ret_date required"}), 400

    dest_city = b.get("dest_city")

    # Coerce/validate numeric fields BEFORE touching the DB. The body is raw
    # JSON, so a stray non-numeric value (e.g. last_price "8,000") would
    # otherwise persist as TEXT and later crash check_all_watches' int<str
    # comparison, blocking every watch. Reject those with a clean 400.
    try:
        adults = int(b.get("adults", 2))
    except (TypeError, ValueError):
        return jsonify({"error": "adults must be numeric"}), 400
    try:
        threshold_pct = float(b.get("threshold_pct", 25.0))
    except (TypeError, ValueError):
        return jsonify({"error": "threshold_pct must be numeric"}), 400
    # child_ages: must be a JSON array when present. A non-list (e.g. the string
    # "11,9") would iterate per-character and persist nonsense ages, so reject it
    # with a 400. Within a list, keep only values that coerce cleanly to int.
    raw_child_ages = b.get("child_ages")
    if raw_child_ages is not None and not isinstance(raw_child_ages, list):
        return jsonify({"error": "child_ages must be a list"}), 400
    child_ages = []
    for a in (raw_child_ages or []):
        try:
            child_ages.append(int(a))
        except (TypeError, ValueError):
            continue
    # REAL-DATA-ONLY guardrail: never trust a client-supplied last_price. A
    # direct/tampered POST could inject a fabricated baseline and trigger bogus
    # drop alerts. Re-derive the baseline server-side from a real fare lookup
    # (get_fare). Because the user just searched this trip, the cell is usually
    # a cache HIT, so this is cheap and returns the same real price. The client's
    # last_price/last_source are ignored entirely.
    fare = get_fare(origin, dest_iata, dep_date, ret_date, adults, len(child_ages))
    cheapest = fare.get("cheapest_cad")
    if cheapest is not None:
        last_price = int(float(cheapest))
        last_source = fare.get("source")
    else:
        # No real data available — leave the baseline unset; the scheduler's
        # first run will seed it from a real fetch.
        last_price = None
        last_source = None

    db = _watch_db()
    try:
        # Idempotent creation: reloading/repeating a search for an already-watched
        # trip must not insert another active row (the scheduler would then re-price
        # it and emit duplicate drop alerts).
        sorted_ages = sorted(child_ages)
        # This route only carries child_ages (no separate count), so the
        # children COUNT it would store equals len(child_ages) — matching
        # add_watch's reconciliation. Include it in the dedup key so a
        # count-only watch (children=2, child_ages=[]) added elsewhere does NOT
        # collide with an adults-only request (children=0, child_ages=[]).
        children = len(child_ages)

        def _matching_active():
            """Return an existing active watch matching this trip key, or None."""
            for existing in db.list_watches(active_only=True):
                if (
                    (existing.get("origin") or "").strip().upper() == origin
                    and (existing.get("dest_iata") or "").strip().upper() == dest_iata
                    and existing.get("dep_date") == dep_date
                    and existing.get("ret_date") == ret_date
                    and int(existing.get("adults") or 0) == adults
                    and int(existing.get("children") or 0) == children
                    and sorted(existing.get("child_ages") or []) == sorted_ages
                ):
                    return existing
            return None

        def _dedup_response(existing):
            """Return the JSON response for a matching active watch.

            If the existing watch has no baseline yet (last_price is None) and
            the server-side get_fare lookup produced a real price, seed the
            existing row's baseline so the scheduler can detect the first real
            drop. The seed value comes from get_fare (real data), never from the
            client. An already established baseline is never overwritten.
            """
            seeded = False
            if existing.get("last_price") is None and last_price is not None:
                db.set_baseline(existing["id"], last_price, last_source)
                seeded = True
            resp = {"id": existing["id"], "ok": True, "existing": True}
            if seeded:
                resp["seeded"] = True
            return jsonify(resp)

        # Fast path: a list_watches() pre-check returns the existing id directly.
        existing = _matching_active()
        if existing is not None:
            return _dedup_response(existing)

        # Atomic backstop: a partial UNIQUE INDEX (active rows only) guards the
        # trip key at the SQLite level. Two concurrent identical POSTs can both
        # pass the pre-check above, but only one INSERT survives — the other
        # raises IntegrityError, which we resolve to the surviving row's id so
        # the response is identical to the pre-check path (one row, existing=True).
        try:
            watch_id = db.add_watch(
                origin=origin, dest_iata=dest_iata, dest_city=dest_city,
                dep_date=dep_date, ret_date=ret_date, adults=adults,
                child_ages=child_ages, threshold_pct=threshold_pct,
                last_price=last_price, last_source=last_source,
            )
        except sqlite3.IntegrityError:
            existing = _matching_active()
            if existing is None:
                raise
            return _dedup_response(existing)
    finally:
        db.close()
    return jsonify({"id": watch_id, "ok": True})


@app.route("/api/watch", methods=["GET"])
def api_watch_list():
    db = _watch_db()
    try:
        rows = db.list_watches()
    finally:
        db.close()
    return jsonify({"watches": [_watch_to_json(r) for r in rows]})


@app.route("/api/watch/<int:watch_id>", methods=["DELETE"])
def api_watch_remove(watch_id):
    db = _watch_db()
    try:
        db.remove_watch(watch_id)
    finally:
        db.close()
    return jsonify({"ok": True})


def build_recommendation(origin, results, adults, child_ages, families):
    bests = [{"city": r["city"], "iata": r["iata"],
              "price_per_family": r["best"]["chosen_cad"] if r["best"] else None,
              "dep": r["best"]["dep"] if r["best"] else None,
              "ret": r["best"]["ret"] if r["best"] else None,
              "chosen": r["best"]["chosen"] if r["best"] else None,
              "stops": r["best"]["stops"] if r["best"] else None}
             for r in results]
    summary = (f"From {origin}, {adults} adults + {len(child_ages)} kids, "
               f"{families} family/families. Per-family best options: "
               + json.dumps(bests))
    prompt = (
        "You are a savvy travel planner for someone with FLEXIBLE dates who wants the most "
        "cost-effective vacation. The data below was COLLECTED FROM LIVE FLIGHT APIs (prices are "
        "CAD per family). Analyze ONLY this data — do not invent prices. Pick the single "
        "best-value trip and explain in 2-3 short sentences why (balance price, stops/nonstop, "
        "and dates). Then give a one-line runner-up. Be concise.\n\n" + summary
    )
    try:
        return ollama_chat(prompt)
    except Exception as e:
        # graceful fallback: cheapest by price
        valid = [b for b in bests if b["price_per_family"]]
        if not valid:
            return "No priceable options found."
        top = min(valid, key=lambda b: b["price_per_family"])
        return (f"Best value: {top['city']} ({top['iata']}) at ~CA${top['price_per_family']:,}"
                f"/family, {top['dep']} → {top['ret']}, {top['chosen']}. "
                f"(AI summary unavailable: {e})")

if __name__ == "__main__":
    print(f"Whenever -> http://localhost:{PORT}  (model={OLLAMA_MODEL}, "
          f"providers={providers_configured() or 'none'})")
    app.run(host="127.0.0.1", port=PORT, debug=True)
