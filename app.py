#!/usr/bin/env python3
"""
Whenever - flexible-trip best-value flight finder.

Smart features (top-cities expansion, best-value recommendation) run on a local
LLM via Ollama (default model: deepseek-v4pro). Fares come from Amadeus if
credentials are set, otherwise from clearly-labeled AI estimates. Every price
deep-links to a real Kayak search for booking.
"""
import os, re, json, time, datetime as dt
from functools import lru_cache
import requests
from flask import Flask, request, jsonify, render_template

# ----------------------------- config -----------------------------
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "deepseek-v4pro")
AMADEUS_ID = os.environ.get("AMADEUS_CLIENT_ID")
AMADEUS_SECRET = os.environ.get("AMADEUS_CLIENT_SECRET")
# Travelpayouts / Aviasales token (free signup) -> real cached market fares + booking links
TRAVELPAYOUTS_TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN")
CURRENCY = os.environ.get("CURRENCY", "cad").lower()
FARE_CACHE_TTL = int(os.environ.get("FARE_CACHE_TTL", "3600"))

def providers_configured():
    p = []
    if AMADEUS_ID and AMADEUS_SECRET:
        p.append("amadeus")
    if TRAVELPAYOUTS_TOKEN:
        p.append("travelpayouts")
    return p

app = Flask(__name__)

# ----------------------- fare cache -----------------------
# Maps (origin, dest, dep, ret, adults, children) -> (expiry_epoch, result_dict).
# Only real priced results (cheapest_cad is truthy) are stored; no-data sentinels
# are never cached so a transient provider failure is not persisted.
_fare_cache: dict = {}

# ----------------------------- Ollama -----------------------------
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
        requests.get(f"{OLLAMA_HOST}/api/tags", timeout=3).raise_for_status()
        return True
    except Exception:
        return False

# ----------------------- top cities (GenAI) -----------------------
@lru_cache(maxsize=128)
def top_cities(country, n=6):
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
            out.append({"city": str(d.get("city", "")).strip(),
                        "iata": str(d["iata"]).strip().upper()[:3]})
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

def _get_fare_uncached(origin, dest, dep, ret, adults, children):
    """Try each configured provider in order; return first real priced result."""
    for provider in (amadeus_fare, travelpayouts_fare):
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
    return [(d + dt.timedelta(days=i)).isoformat() for i in range(count)]

@app.route("/api/search", methods=["POST"])
def api_search():
    b = request.get_json(force=True)
    origin = (b.get("origin") or "").upper()[:3]
    dests = b.get("destinations") or []          # [{city,iata}]
    adults = int(b.get("adults", 2))
    child_ages = [int(a) for a in (b.get("child_ages") or [])]
    children = len(child_ages)
    dep_dates = b.get("dep_dates") or date_range(b.get("dep_start", ""), int(b.get("dep_span", 4)))
    ret_dates = b.get("ret_dates") or date_range(b.get("ret_start", ""), int(b.get("ret_span", 4)))
    threshold = float(b.get("nonstop_threshold", 25)) / 100.0
    families = int(b.get("families", 1))

    if not origin or not dests or not dep_dates or not ret_dates:
        return jsonify({"error": "origin, destinations and dates required"}), 400

    results = []
    for dest in dests:
        code = (dest.get("iata") or "").upper()[:3]
        grid = []
        for dep in dep_dates:
            row = []
            for ret in ret_dates:
                f = get_fare(origin, code, dep, ret, adults, children)
                cheap = f.get("cheapest_cad")
                ns = f.get("nonstop_cad")
                chosen = "cheapest"
                chosen_cad = cheap
                if ns and cheap and ns <= cheap * (1 + threshold):
                    chosen, chosen_cad = "nonstop", ns
                row.append({
                    "dep": dep, "ret": ret,
                    "cheapest_cad": cheap, "stops": f.get("stops"),
                    "nonstop_cad": ns, "chosen": chosen, "chosen_cad": chosen_cad,
                    "source": f.get("source"),
                    "book": f.get("book") or kayak_link(origin, code, dep, ret, adults, child_ages),
                })
            grid.append(row)
        # best cell for this city
        flat = [c for r in grid for c in r if c["chosen_cad"]]
        best = min(flat, key=lambda c: c["chosen_cad"]) if flat else None
        results.append({"city": dest.get("city"), "iata": code,
                        "grid": grid, "best": best})

    recommendation = build_recommendation(origin, results, adults, child_ages, families)
    return jsonify({
        "origin": origin, "adults": adults, "child_ages": child_ages,
        "families": families, "dep_dates": dep_dates, "ret_dates": ret_dates,
        "results": results, "recommendation": recommendation,
        "providers": providers_configured(),
    })

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
    print(f"Whenever -> http://localhost:5000  (model={OLLAMA_MODEL}, "
          f"live_fares={bool(AMADEUS_ID and AMADEUS_SECRET)})")
    app.run(host="127.0.0.1", port=5000, debug=True)
