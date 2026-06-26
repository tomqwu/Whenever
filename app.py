#!/usr/bin/env python3
"""
Whenever - flexible-trip best-value flight finder.

Smart features (top-cities expansion, best-value recommendation) run on a local
LLM via Ollama (default model: qwen3:8b). Fares come from Amadeus if
credentials are set, otherwise from clearly-labeled AI estimates. Every price
deep-links to a real Kayak search for booking.
"""
import os, re, json, time, datetime as dt, logging, concurrent.futures, sqlite3, threading
from functools import lru_cache, wraps
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

# ----------------------- airports dataset (autocomplete) -----------------------
# Country codes whose `country` value is NOT a sovereign country, so they must
# never be offered as a "country" autocomplete suggestion (only as cities). For a
# neutral travel app: Hong Kong (HK) and Macau (MO) are SARs; Taiwan (TW) is
# politically sensitive; the rest are dependent/overseas territories present in the
# dataset. Matched by 2-letter country_code so it's robust regardless of the
# display string used as a city's location subtitle.
_NON_SOVEREIGN_COUNTRY_CODES = {
    "HK",  # Hong Kong SAR
    "MO",  # Macau SAR
    "TW",  # Taiwan
    "PR",  # Puerto Rico (US territory)
    "GU",  # Guam (US territory)
    "PF",  # French Polynesia (French overseas collectivity)
}


def _load_airports():
    """Load the bundled airports dataset (config/airports.json).

    Returns a list of ``{"iata","city","country","country_code"}`` dicts.
    Missing/malformed file -> empty list (suggest then returns no results),
    so a packaging slip degrades gracefully instead of crashing at import.
    """
    path = os.path.join(os.path.dirname(__file__), "config", "airports.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            _log.warning("airports.json parsed to non-list; autocomplete disabled")
            return []
        out = []
        for a in data:
            if not isinstance(a, dict):
                continue
            iata = str(a.get("iata", "")).strip().upper()
            city = str(a.get("city", "")).strip()
            country = str(a.get("country", "")).strip()
            if not iata or not city or not country:
                continue
            out.append({
                "iata": iata,
                "city": city,
                "country": country,
                "country_code": str(a.get("country_code", "")).strip().upper(),
            })
        return out
    except FileNotFoundError:
        _log.warning("config/airports.json not found; destination autocomplete disabled")
        return []
    except Exception as exc:
        _log.warning("Failed to parse airports.json (%s); autocomplete disabled", exc)
        return []


def _build_suggest_index(airports, seed_config):
    """Build the country + city suggestion index from the airports dataset.

    Countries are the unique country names (from the dataset plus any
    ``country_seeds.yaml`` display names), each carrying its 2-letter code when
    known. Cities are one entry per airport (city/iata/country). Returns
    ``(countries, cities)`` where each list element is a ready-to-serve dict.
    """
    countries = {}  # lower-name -> {"name","code"}
    for a in airports:
        # Non-sovereign territories (HK/MO SARs, Taiwan, overseas territories)
        # must not be offered as a "country" suggestion — they remain CITY
        # suggestions only. Matched by code so it's robust regardless of the
        # `country` display string (e.g. "Hong Kong SAR").
        if a.get("country_code", "") in _NON_SOVEREIGN_COUNTRY_CODES:
            continue
        key = a["country"].lower()
        if key not in countries:
            countries[key] = {"name": a["country"], "code": a.get("country_code", "")}
    # Fold in seed countries (display_name preferred) so a seeded country with no
    # dataset airport still autocompletes and expands via top_cities.
    for raw_key, cfg in (seed_config or {}).items():
        name = (isinstance(cfg, dict) and cfg.get("display_name")) or str(raw_key).title()
        if name.lower() not in countries:
            countries[name.lower()] = {"name": name, "code": ""}
    cities = [
        {"city": a["city"], "iata": a["iata"], "country": a["country"]}
        for a in airports
    ]
    return list(countries.values()), cities


_AIRPORTS: list = _load_airports()
_SUGGEST_COUNTRIES, _SUGGEST_CITIES = _build_suggest_index(_AIRPORTS, _SEED_CONFIG)


def suggest_destinations(q, limit=10):
    """Rank country + city suggestions for the type-ahead query ``q``.

    Matching (case-insensitive): countries whose name starts-with/contains ``q``;
    cities whose city name, IATA, or country contains ``q``. Ranking (lower score
    first): exact IATA < country prefix < city/IATA prefix < country substring <
    city substring. An exact IATA match wins over a country whose name merely
    starts with ``q`` (e.g. ``DEN``=Denver ranks ahead of Denmark, ``VIE``=Vienna
    ahead of Vietnam, ``CAN``=Guangzhou ahead of Canada). Ties break
    alphabetically for stable output. Capped at ``limit`` results. Empty/blank
    ``q`` -> [].
    """
    q = (q or "").strip().lower()
    if len(q) < 1:
        return []

    scored = []  # (rank, sort_key, suggestion_dict)
    for c in _SUGGEST_COUNTRIES:
        name = c["name"].lower()
        if name.startswith(q):
            rank = 1
        elif q in name:
            rank = 3
        else:
            continue
        scored.append((rank, name, {"type": "country", "name": c["name"], "code": c["code"]}))

    for c in _SUGGEST_CITIES:
        city = c["city"].lower()
        iata = c["iata"].lower()
        country = c["country"].lower()
        if iata == q:
            # Exact IATA beats a country whose name merely prefixes q (DEN/VIE/CAN).
            rank = 0
        elif city.startswith(q) or iata.startswith(q):
            rank = 2
        elif q in city or q in iata or q in country:
            rank = 4
        else:
            continue
        scored.append((rank, city + iata, {
            "type": "city", "city": c["city"], "iata": c["iata"], "country": c["country"],
        }))

    scored.sort(key=lambda t: (t[0], t[1]))
    return [s for _, _, s in scored[:limit]]

# ----------------------------- config -----------------------------
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY")
AMADEUS_ID = os.environ.get("AMADEUS_CLIENT_ID")
AMADEUS_SECRET = os.environ.get("AMADEUS_CLIENT_SECRET")
# Travelpayouts / Aviasales token (free signup) -> real cached market fares + booking links
TRAVELPAYOUTS_TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN")
KIWI_API_KEY = os.environ.get("KIWI_API_KEY")
SERPAPI_KEY = os.environ.get("SERPAPI_KEY")
# RapidAPI flights-sky (Skyscanner data) — preferred provider (richest data).
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY")
RAPIDAPI_HOST = os.environ.get("RAPIDAPI_HOST", "flights-sky.p.rapidapi.com")
# Skyscanner (flights-sky) async poll budget. The search session completes
# asynchronously, so we poll search-incomplete until context.status == "complete".
# Longer-but-BOUNDED: ~12 polls x 1.5s ~= 18s of waiting (enough for most routes,
# incl. long-haul) so we don't needlessly fall back to serpapi and lose Skyscanner's
# richer duration/airlines/layovers data. Still bounded — worst case is roughly
# attempts*interval plus one per-poll request timeout — so a genuinely hung session
# degrades to no-data in seconds-to-~tens-of-seconds, never minutes.
# Timeouts for the INITIAL search-roundtrip call. That call is async on
# Skyscanner's side: it kicks off the search and returns a sessionId (status
# "incomplete") for us to poll — but returning that session legitimately takes
# ~25-30s (measured 27.3s live for YYZ->PEK) and sometimes longer, all while the
# response IS progressing. We pass requests a ``(connect, read)`` tuple:
#   * SKYSCANNER_CONNECT_TIMEOUT bounds CONNECTION setup so a DEAD host fails fast.
#   * SKYSCANNER_SEARCH_TIMEOUT is the READ/INACTIVITY timeout: requests fires it
#     only after that many seconds of NO bytes arriving — it is NOT a total
#     wall-clock cap. As long as flights-sky keeps the response progressing we keep
#     waiting; we only give up after prolonged SILENCE. Default 90 is generous so a
#     slow-but-alive search completes instead of being killed mid-flight (the old
#     tight cap dropped real long-haul results every time). Distinct from the SHORT
#     poll timeout below.
SKYSCANNER_CONNECT_TIMEOUT = int(os.environ.get("SKYSCANNER_CONNECT_TIMEOUT", "10"))
SKYSCANNER_SEARCH_TIMEOUT = int(os.environ.get("SKYSCANNER_SEARCH_TIMEOUT", "90"))
SKYSCANNER_POLL_ATTEMPTS = int(os.environ.get("SKYSCANNER_POLL_ATTEMPTS", "12"))
SKYSCANNER_POLL_INTERVAL = float(os.environ.get("SKYSCANNER_POLL_INTERVAL", "1.5"))
SKYSCANNER_POLL_TIMEOUT = int(os.environ.get("SKYSCANNER_POLL_TIMEOUT", "8"))
# ----------------------------- provider retry/backoff (#41) -----------------------------
# Bounded exponential-backoff retry for TRANSIENT fare-provider failures (request
# timeouts, connection errors, HTTP 5xx, and provider-side 429 rate limits). Applied
# to the simple one-shot provider request calls (amadeus token + search, travelpayouts,
# kiwi, serpapi) via _request_with_retry. Kept strictly bounded so a blip degrades
# gracefully / recovers without ever hanging a search worker for minutes:
#   worst-case extra wait ~= sum(min(BACKOFF*2**attempt, BACKOFF_MAX) for retries)
#   default 2 retries, 0.5s backoff, 4s cap -> ~0.5 + 1.0 = ~1.5s of sleeping.
# A 429 carrying Retry-After honours it but capped at BACKOFF_MAX so a hostile/large
# header can't stall a cell. The skyscanner poll path is NOT wrapped here — it keeps
# its own purpose-built bounded 502 retry (_skyscanner_get) and poll budget (#64) so
# the two retry mechanisms never stack.
PROVIDER_RETRIES = int(os.environ.get("PROVIDER_RETRIES", "2"))
PROVIDER_BACKOFF = float(os.environ.get("PROVIDER_BACKOFF", "0.5"))
PROVIDER_BACKOFF_MAX = float(os.environ.get("PROVIDER_BACKOFF_MAX", "4"))
CURRENCY = os.environ.get("CURRENCY", "cad").lower()
# ----------------------------- demo / sample mode (#44) -----------------------------
# Opt-in, clearly-labeled SAMPLE-fare mode for no-key exploration. Default OFF.
# When ON, EVERY fare is a deterministic, obviously-fake sample tagged
# source="demo" (carrier "DemoAir"), real providers are NEVER called, and the UI
# shows a prominent persistent "DEMO DATA — NOT real prices" banner. This is the
# SOLE sanctioned exception to the real-data-only guardrail, and ONLY because it is
# explicit and unmistakably labeled: demo data is NEVER a silent fallback when
# providers fail/aren't configured (that path returns the no-data sentinel), and
# demo fares are NEVER written to the real fare cache (FARE_CACHE_PATH).
DEMO_MODE: bool = os.environ.get("DEMO_MODE", "").lower() in ("1", "true", "yes")
FARE_CACHE_TTL = int(os.environ.get("FARE_CACHE_TTL", "3600"))
# Disk-persistence path for the fare cache (#42). A JSON file in the working dir.
# Empty string disables persistence (memory-only). FARE_CACHE_TTL <= 0 disables
# caching entirely, including persistence. Real fares survive a restart within TTL.
FARE_CACHE_PATH = os.environ.get("FARE_CACHE_PATH", "whenever_fare_cache.json")
SEARCH_CONCURRENCY = int(os.environ.get("SEARCH_CONCURRENCY", "8"))
# Hard cap on search grid size. Each cell = one provider API call. A value <= 0 disables the cap.
MAX_SEARCH_CELLS = int(os.environ.get("MAX_SEARCH_CELLS", "200"))
# Generous per-direction day cap. Bounds dep_span/ret_span (and date_range count)
# BEFORE expansion so a malformed huge span can't allocate millions of dates. The
# form max is small; this is a safety ceiling, not the typical value.
MAX_DATE_SPAN = int(os.environ.get("MAX_DATE_SPAN", "60"))
# Dev-server port. Default 5001 to avoid macOS AirPlay Receiver, which holds 5000.
PORT = int(os.environ.get("PORT", "5001"))

# ----------------------------- rate limiting (#60) -----------------------------
# Lightweight in-memory per-IP sliding-window rate limiter. No new dependencies.
# RATE_LIMIT_ENABLED  — set to 0/false/no to disable entirely (tests default off).
# RATE_LIMIT_WINDOW   — sliding window size in seconds.
# SEARCH_RATE_PER_MIN — max requests per window for the "search" bucket
#                       (/api/search, /api/search/stream, /api/export/*).
# API_RATE_PER_MIN    — max requests per window for the "api" bucket
#                       (/api/top-cities, /api/suggest, /api/resolve, /api/watch*).
# TRUST_PROXY         — set true ONLY behind a trusted proxy that sets
#                       X-Forwarded-For; otherwise XFF is ignored and the bucket
#                       keys on the real socket peer (unspoofable). Default false.
# / and /api/health are exempt.
def _parse_bool_env(name, default=True):
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v not in ("0", "false", "no")

RATE_LIMIT_ENABLED: bool = _parse_bool_env("RATE_LIMIT_ENABLED", True)
RATE_LIMIT_WINDOW: int = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))
SEARCH_RATE_PER_MIN: int = int(os.environ.get("SEARCH_RATE_PER_MIN", "10"))
API_RATE_PER_MIN: int = int(os.environ.get("API_RATE_PER_MIN", "60"))

# Whether to trust the client-supplied X-Forwarded-For header for client-IP
# identity.  Default FALSE: a client can rotate/spoof XFF to bypass 429s or burn
# another user's quota, so by default we key on the real socket peer
# (request.remote_addr).  Set TRUST_PROXY=true ONLY when the app sits behind a
# trusted reverse proxy that sets X-Forwarded-For itself.
TRUST_PROXY: bool = os.environ.get("TRUST_PROXY", "").lower() in ("1", "true", "yes")

# Module-level sliding-window state: (client_ip, bucket_name) -> [timestamp, ...]
_rate_state: dict = {}

# Guards the prune -> check -> append sequence so the count-and-record is atomic
# under a threaded WSGI server (two same-IP requests can't both pass the limit
# check before either appends).
_rate_lock = threading.Lock()


def _rate_time() -> float:
    """Returns current time. Thin wrapper so tests can monkeypatch it."""
    return time.time()


def _client_ip() -> str:
    """Return the client IP used as the rate-limit bucket key.

    By default (TRUST_PROXY=False) the client-supplied X-Forwarded-For header is
    ignored entirely and the real socket peer (request.remote_addr) is used, so
    a client cannot rotate/spoof XFF to bypass limits.  Only when TRUST_PROXY is
    True (app behind a trusted proxy that sets XFF) do we use the FIRST hop of
    X-Forwarded-For.
    """
    if TRUST_PROXY:
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.remote_addr or "127.0.0.1"


def rate_limited(bucket: str, limit_fn):
    """Decorator factory: apply sliding-window rate limiting to a route.

    Args:
        bucket:   Logical bucket name (e.g. "search" or "api").
        limit_fn: Zero-arg callable returning the current per-window limit.
                  Using a callable instead of a plain int means the limit can
                  be monkeypatched in tests and the decorator picks it up live.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not RATE_LIMIT_ENABLED:
                return fn(*args, **kwargs)
            ip = _client_ip()
            key = (ip, bucket)
            now = _rate_time()
            window = RATE_LIMIT_WINDOW
            limit = limit_fn()

            # Guard prune -> check -> append so the count-and-record is atomic:
            # under a threaded WSGI server two same-IP requests must not both
            # pass the limit check before either appends.  Only the fast dict
            # ops are held under the lock.
            with _rate_lock:
                # Prune timestamps outside the sliding window
                ts_list = _rate_state.get(key, [])
                ts_list = [t for t in ts_list if now - t < window]

                if len(ts_list) >= limit:
                    # Compute seconds until the oldest timestamp ages out.
                    # With a limit <= 0 the endpoint is effectively disabled and
                    # ts_list is empty on the first request, so guard the access:
                    # fall back to the full window rather than indexing [0].
                    if ts_list:
                        oldest = ts_list[0]
                        retry_after = max(1, int(oldest + window - now) + 1)
                    else:
                        retry_after = max(1, int(window))
                    resp = jsonify({"error": "rate limit exceeded, slow down"})
                    resp.status_code = 429
                    resp.headers["Retry-After"] = str(retry_after)
                    return resp

                ts_list.append(now)
                _rate_state[key] = ts_list
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def providers_configured():
    # DEMO_MODE (#44): the app serves ONLY clearly-labeled sample fares and never
    # calls a real provider, so the configured-provider list is exactly ["demo"].
    # This keeps demo and real strictly separate (no part-real/part-demo grid) and
    # lets every existing provider-list consumer (health, meta, footer) surface the
    # demo state without special-casing.
    if DEMO_MODE:
        return ["demo"]
    p = []
    if RAPIDAPI_KEY:
        p.append("skyscanner")
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
# Maps (origin, dest, dep, ret, adults, children, compare_tag) -> (fetched_epoch, result_dict).
# `compare_tag` (#43, see _compare_cache_tag) is False for ordered fallback and
# "cmp:<sorted providers>" for compare mode, so a cross-provider-compared fare never
# serves an ordered-fallback request (or a compare request made against a different
# configured-provider set), and vice versa.
# We store WHEN the fare was fetched (not an absolute expiry) and judge freshness as
# (now - fetched) < FARE_CACHE_TTL everywhere, so lowering FARE_CACHE_TTL between runs
# takes effect immediately — old long-TTL entries are dropped on the next check/load.
# Only real priced results (cheapest_cad is truthy) are stored; no-data sentinels
# are never cached so a transient provider failure is not persisted.
_fare_cache: dict = {}
# Guards _fare_cache mutations AND disk writes (the search is concurrent — #33/#42),
# so a save never serialises a half-mutated dict and two threads never race a file.
_fare_cache_lock = threading.Lock()


def _persistence_enabled():
    """True when the fare cache should be written to / read from disk.

    Disabled (memory-only) when caching is off (FARE_CACHE_TTL <= 0) or when
    FARE_CACHE_PATH is empty/blank.
    """
    return FARE_CACHE_TTL > 0 and bool((FARE_CACHE_PATH or "").strip())


def _save_fare_cache():
    """Atomically persist the current still-fresh _fare_cache to FARE_CACHE_PATH.

    Prunes entries that are stale under the CURRENT FARE_CACHE_TTL opportunistically,
    serialises the tuple-keyed cache as a JSON list of records (storing each fare's
    fetch time, not an absolute expiry), writes to a temp file in the same dir, then
    os.replace (atomic on POSIX) so a reader never sees a partial file and no temp
    file lingers. No-op when persistence is disabled. Call with _fare_cache_lock held.
    """
    if not _persistence_enabled():
        return
    now = time.time()
    # Prune entries no longer fresh under the current TTL while we hold the lock,
    # then snapshot what survives.
    stale = [k for k, (fetched, _r) in _fare_cache.items()
             if now - fetched >= FARE_CACHE_TTL]
    for k in stale:
        del _fare_cache[k]
    records = [{"key": list(key), "fetched": fetched, "result": res}
               for key, (fetched, res) in _fare_cache.items()]
    tmp = f"{FARE_CACHE_PATH}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(records, f)
        os.replace(tmp, FARE_CACHE_PATH)
    except OSError as exc:
        _log.warning("Failed to persist fare cache to %s (%s)", FARE_CACHE_PATH, exc)
        try:
            os.remove(tmp)
        except OSError:
            pass


def _load_fare_cache():
    """Populate _fare_cache from FARE_CACHE_PATH on startup, revalidating freshness.

    Each record's stored ``fetched`` time is checked against the CURRENT FARE_CACHE_TTL
    (``now - fetched < FARE_CACHE_TTL``), so a TTL lowered between runs immediately
    drops fares that are now stale instead of honouring their old longer lifetime.
    No-op when persistence is disabled or the file does not exist. A missing,
    corrupt, or malformed file leaves the cache empty (logged, never crashes).
    Rebuilds tuple keys from the stored list-of-records.
    """
    if not _persistence_enabled() or not os.path.exists(FARE_CACHE_PATH):
        return
    try:
        with open(FARE_CACHE_PATH, encoding="utf-8") as f:
            records = json.load(f)
        if not isinstance(records, list):
            raise ValueError("fare cache file is not a list")
        now = time.time()
        loaded = {}
        skipped = 0
        for rec in records:
            try:
                if not isinstance(rec, dict):
                    skipped += 1
                    continue
                raw_key = rec.get("key")
                fetched = rec.get("fetched")
                result = rec.get("result")
                # key must be a sequence of exactly 7 elements
                # (origin, dest, dep, ret, adults, children, compare); the trailing
                # compare flag (#43) separates compared from fallback fares. Records
                # from an older 6-element layout are skipped as invalid.
                if not isinstance(raw_key, (list, tuple)) or len(raw_key) != 7:
                    skipped += 1
                    continue
                # fetched must be a number and the fare still fresh under the CURRENT
                # TTL (drops entries written with an older, longer TTL — this is the fix)
                if not isinstance(fetched, (int, float)) or now - fetched >= FARE_CACHE_TTL:
                    skipped += 1
                    continue
                # result must be a real-priced dict (same guard get_fare uses to cache)
                if not isinstance(result, dict) or not result.get("cheapest_cad"):
                    skipped += 1
                    continue
                loaded[tuple(raw_key)] = (fetched, result)
            except Exception:  # noqa: BLE001 — never let one bad record abort the load
                skipped += 1
        if skipped:
            _log.debug("_load_fare_cache: skipped %d invalid/stale record(s)", skipped)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        _log.warning("Could not load fare cache from %s (%s); starting empty",
                     FARE_CACHE_PATH, exc)
        return
    with _fare_cache_lock:
        _fare_cache.update(loaded)

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
        f"List the top {n} destination cities in the country {country} for "
        f"international leisure travelers. If {country} is actually a single city "
        f"or territory rather than a country, return just that one place. Return "
        f"ONLY a JSON array; each item: "
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
_ISO_DURATION_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?$")


def parse_iso_duration(s):
    """Parse an ISO-8601 time duration like ``PT14H35M`` to total minutes.

    Handles hours-only (``PT14H``), minutes-only (``PT35M``) and combined
    (``PT14H35M``) forms. Returns an int of total minutes, or ``None`` if the
    input is missing, malformed, or has neither hours nor minutes. Never raises.
    """
    if not s or not isinstance(s, str):
        return None
    m = _ISO_DURATION_RE.match(s.strip())
    if not m:
        return None
    hours, minutes = m.group(1), m.group(2)
    if hours is None and minutes is None:
        return None
    return int(hours or 0) * 60 + int(minutes or 0)


def _amadeus_airlines(offer):
    """Unique segment carrierCodes across all itineraries of an offer (order-preserving).

    Amadeus gives IATA carrier codes (e.g. "AC"), not display names, so these are
    codes. Returns [] when no segment carries a carrierCode (never fabricated).
    """
    codes = []
    for it in offer.get("itineraries") or []:
        if not isinstance(it, dict):
            continue
        for seg in it.get("segments") or []:
            code = seg.get("carrierCode") if isinstance(seg, dict) else None
            if code and code not in codes:
                codes.append(code)
    return codes


def _amadeus_seg_iata(point):
    """IATA code from an Amadeus segment endpoint (departure/arrival), or None."""
    return point.get("iataCode") if isinstance(point, dict) else None


def _amadeus_layovers(offer):
    """Connection layovers derived from an offer's itineraries[].segments[].

    A layover is the gap between consecutive segments within one itinerary: its IATA
    is segment N's arrival airport, and its duration is the minutes between segment
    N's arrival time and segment N+1's departure time (ISO-8601 timestamps). If the
    timestamps are missing/unparseable the duration is None (best-effort, never
    fabricated). Returns [] for an all-nonstop offer.
    """
    out = []
    for it in offer.get("itineraries") or []:
        if not isinstance(it, dict):
            continue
        segs = [s for s in (it.get("segments") or []) if isinstance(s, dict)]
        for i in range(len(segs) - 1):
            iata = _amadeus_seg_iata(segs[i].get("arrival"))
            arr_at = segs[i].get("arrival", {}).get("at") if isinstance(segs[i].get("arrival"), dict) else None
            dep_at = segs[i + 1].get("departure", {}).get("at") if isinstance(segs[i + 1].get("departure"), dict) else None
            out.append({"iata": iata, "duration_min": _iso_gap_minutes(arr_at, dep_at)})
    return out


def _iso_gap_minutes(start, end):
    """Whole-minute gap between two ISO-8601 datetime strings, or None if unparseable.

    Used for Amadeus connection layover durations (arrival of seg N → departure of
    seg N+1). A missing/malformed value (or a negative gap) yields None — never
    fabricated.
    """
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        a = dt.datetime.fromisoformat(start)
        b = dt.datetime.fromisoformat(end)
    except (ValueError, TypeError):
        return None
    secs = (b - a).total_seconds()
    if secs < 0:
        return None
    return int(round(secs / 60.0))


# ----------------------------- provider retry/backoff helper (#41) -----------------------------
def _retry_after_seconds(resp, cap):
    """Parse a Retry-After header (delta-seconds form) into a capped sleep.

    Returns the header value clamped to [0, cap] if it's a non-negative integer
    number of seconds, else None (caller falls back to exponential backoff). The
    HTTP-date form is intentionally not honoured — we only ever sleep a small,
    bounded amount, so a date would just fall back to the capped backoff anyway.
    """
    val = None
    headers = getattr(resp, "headers", None)
    if headers:
        try:
            val = headers.get("Retry-After")
        except AttributeError:
            val = None
    if val is None:
        return None
    try:
        secs = float(val)
    except (TypeError, ValueError):
        return None
    if secs < 0:
        return None
    return min(secs, cap)


def _request_with_retry(method, url, *, headers=None, params=None, data=None,
                        json=None, timeout=30, retries=None, backoff=None,
                        backoff_max=None, retry_statuses=(429, 500, 502, 503, 504)):
    """Issue an HTTP request with BOUNDED exponential-backoff retry on transients.

    Retries on ``requests.ConnectionError`` and on any response whose status is in
    ``retry_statuses`` (429 + 5xx by default). Up to ``retries + 1`` attempts total.
    Between attempts it sleeps ``backoff * 2**attempt`` seconds, each sleep capped at
    ``backoff_max`` (so total wait stays small); a 429 with a ``Retry-After``
    delta-seconds header sleeps that value instead, also capped.

    ``requests.Timeout`` is treated as **terminal** (not retried): the attempt cost
    was already paid (the full per-attempt timeout elapsed), so retrying would only
    multiply the stall. On a Timeout the helper returns ``None`` immediately, letting
    the caller fall through to the next provider exactly as it would for a network
    error on the last attempt.

    Returns the final ``requests.Response`` — even a 5xx/429 on the last attempt —
    so callers keep their existing ``status_code != 200 -> None`` contract. Returns
    ``None`` if every attempt raised a network/timeout error (mirrors providers' bare
    except). Never raises a network error past the caller; never loops unbounded.
    """
    retries = PROVIDER_RETRIES if retries is None else retries
    backoff = PROVIDER_BACKOFF if backoff is None else backoff
    backoff_max = PROVIDER_BACKOFF_MAX if backoff_max is None else backoff_max
    # Dispatch via requests.get / requests.post (not requests.request) so the
    # method-specific call sites stay patchable the way the rest of the codebase
    # and its tests already monkeypatch them.
    kwargs = {"timeout": timeout}
    if headers is not None:
        kwargs["headers"] = headers
    if params is not None:
        kwargs["params"] = params
    if method.upper() == "POST":
        sender = requests.post
        if data is not None:
            kwargs["data"] = data
        if json is not None:
            kwargs["json"] = json
    else:
        sender = requests.get
    # range(retries+1) guarantees a bounded number of attempts; the last attempt
    # (attempt == retries) always returns (success/None), so the loop never falls
    # through — there is no unbounded path and no implicit None at the end.
    for attempt in range(retries + 1):
        try:
            resp = sender(url, **kwargs)
        except requests.exceptions.Timeout:
            # Full per-attempt timeout already elapsed — retrying would only
            # multiply the stall. Treat as terminal and return None immediately.
            return None
        except requests.exceptions.ConnectionError:
            # Fast connection failure (TCP refused / DNS error). Retry while
            # attempts remain, then give up with None.
            if attempt < retries:
                time.sleep(min(backoff * (2 ** attempt), backoff_max))
                continue
            return None
        # Success / non-retryable status, or the final attempt -> return for the
        # caller to handle (e.g. 200 parsed, 404 -> None, last-attempt 5xx -> None).
        if resp.status_code not in retry_statuses or attempt >= retries:
            return resp
        # Retryable status with attempts left: prefer a (capped) Retry-After on a
        # 429, else exponential backoff (capped).
        delay = None
        if resp.status_code == 429:
            delay = _retry_after_seconds(resp, backoff_max)
        if delay is None:
            delay = min(backoff * (2 ** attempt), backoff_max)
        time.sleep(delay)


_amadeus_token = {"value": None, "exp": 0}

def amadeus_token():
    if not (AMADEUS_ID and AMADEUS_SECRET):
        return None
    if _amadeus_token["value"] and time.time() < _amadeus_token["exp"] - 30:
        return _amadeus_token["value"]
    r = _request_with_retry(
        "POST",
        "https://test.api.amadeus.com/v1/security/oauth2/token",
        data={"grant_type": "client_credentials",
              "client_id": AMADEUS_ID, "client_secret": AMADEUS_SECRET},
        timeout=20,
    )
    if r is None or r.status_code != 200:
        return None
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
    r = _request_with_retry("GET", "https://test.api.amadeus.com/v2/shopping/flight-offers",
                            headers={"Authorization": f"Bearer {tok}"},
                            params=params, timeout=30)
    if r is None or r.status_code != 200:
        return None
    offers = r.json().get("data", [])
    if not offers:
        return None
    def stops(o):
        return max(len(s["segments"]) - 1 for s in o["itineraries"])
    cheapest = min(offers, key=lambda o: float(o["price"]["grandTotal"]))
    nonstops = [o for o in offers if stops(o) == 0]
    ns = min(nonstops, key=lambda o: float(o["price"]["grandTotal"])) if nonstops else None

    def duration(o):
        """Sum each itinerary's ISO-8601 duration (outbound+return) to minutes.

        Returns None if any leg's duration is absent/unparseable (don't fabricate).
        ``o["itineraries"]`` is a list of dicts here (already validated by stops()),
        and parse_iso_duration returns None for any missing/malformed value.
        """
        legs = [parse_iso_duration(it.get("duration")) for it in o["itineraries"]]
        if not legs or any(d is None for d in legs):
            return None
        return sum(legs)

    return {
        "cheapest_cad": round(float(cheapest["price"]["grandTotal"])),
        "stops": stops(cheapest),
        "nonstop_cad": round(float(ns["price"]["grandTotal"])) if ns else None,
        "source": "amadeus",
        "duration_min": duration(cheapest),
        "nonstop_duration_min": duration(ns) if ns else None,
        "airlines": _amadeus_airlines(cheapest),
        "nonstop_airlines": _amadeus_airlines(ns) if ns else None,
        "layovers": _amadeus_layovers(cheapest),
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
    r = _request_with_retry("GET", "https://api.travelpayouts.com/aviasales/v3/prices_for_dates",
                            params=params, timeout=30)
    if r is None or r.status_code != 200:
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

    def duration(o):
        """Total trip minutes: `duration` if present, else duration_to+duration_back."""
        try:
            if o.get("duration") is not None:
                return int(round(float(o["duration"])))
            to, back = o.get("duration_to"), o.get("duration_back")
            if to is not None and back is not None:
                return int(round(float(to) + float(back)))
        except (TypeError, ValueError):
            return None
        return None

    return {
        "cheapest_cad": round(total(cheapest)),
        "stops": int(cheapest.get("transfers", 0)) + int(cheapest.get("return_transfers", 0)),
        "nonstop_cad": round(total(ns)) if ns else None,
        "source": "travelpayouts",
        "book": book,
        "duration_min": duration(cheapest),
        "nonstop_duration_min": duration(ns) if ns else None,
        # Travelpayouts gives a single airline code per result and no per-stop
        # detail. airlines -> [code] (or [] when absent); layovers -> None (the
        # provider can't supply connection airports/durations).
        "airlines": [cheapest["airline"]] if cheapest.get("airline") else [],
        # The nonstop pick carries its own single airline code: surface it so the
        # chosen-nonstop cell attributes the real carrier (mirrors `airlines`).
        # None when there's no nonstop option or it lacks an airline field.
        "nonstop_airlines": [ns["airline"]] if ns and ns.get("airline") else None,
        "layovers": None,
    }

def _kiwi_airlines(itin):
    """Unique carrier codes for a Tequila itinerary.

    Prefers the top-level ``airlines`` list (IATA codes); falls back to the
    per-segment ``route[].airline`` codes. These are codes, not display names.
    Returns [] when none are present (never fabricated).
    """
    codes = []
    top = itin.get("airlines") if isinstance(itin, dict) else None
    if isinstance(top, list):
        for c in top:
            if c and c not in codes:
                codes.append(c)
    if codes:
        return codes
    for seg in (itin.get("route") or []) if isinstance(itin, dict) else []:
        c = seg.get("airline") if isinstance(seg, dict) else None
        if c and c not in codes:
            codes.append(c)
    return codes


def _kiwi_layovers(itin):
    """Connection layovers for a Tequila itinerary, derived from route[] segments.

    A layover is the gap between consecutive segments of the SAME direction: its
    IATA is segment N's arrival airport (``flyTo``/``cityCodeTo``) and its duration
    is the minutes between segment N's arrival (``aTime``, Unix seconds) and segment
    N+1's departure (``dTime``). A missing/negative gap yields None (best-effort).
    Returns [] for a nonstop itinerary.
    """
    out = []
    route = itin.get("route") if isinstance(itin, dict) else None
    if not isinstance(route, list):
        return out
    segs = [s for s in route if isinstance(s, dict)]
    for i in range(len(segs) - 1):
        # Only a true connection: same direction (return flag) as the next segment.
        if segs[i].get("return") != segs[i + 1].get("return"):
            continue
        iata = segs[i].get("flyTo") or segs[i].get("cityCodeTo")
        out.append({"iata": iata,
                    "duration_min": _kiwi_gap_minutes(segs[i].get("aTime"),
                                                      segs[i + 1].get("dTime"))})
    return out


def _kiwi_gap_minutes(arr, dep):
    """Whole-minute gap between two Unix-second timestamps, or None if unusable.

    A missing/non-numeric value (or a negative gap) yields None — never fabricated.
    """
    if not isinstance(arr, (int, float)) or not isinstance(dep, (int, float)):
        return None
    secs = dep - arr
    if secs < 0:
        return None
    return int(round(secs / 60.0))


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
    r = _request_with_retry(
        "GET",
        "https://tequila-api.kiwi.com/v2/search",
        headers={"apikey": KIWI_API_KEY},
        params=params,
        timeout=30,
    )
    if r is None or r.status_code != 200:
        return None
    try:
        data = r.json().get("data", [])
    except Exception:
        return None
    if not data:
        return None

    def itin_duration(itin):
        """Total trip minutes from Tequila `duration`.

        `duration` is either a dict {"departure":sec,"return":sec,"total":sec} or
        a bare number of seconds. Prefer total, else departure+return. Seconds →
        minutes. Returns None if absent/unparseable (don't fabricate).
        """
        dur = itin.get("duration")
        try:
            if isinstance(dur, dict):
                if dur.get("total") is not None:
                    secs = float(dur["total"])
                elif dur.get("departure") is not None and dur.get("return") is not None:
                    secs = float(dur["departure"]) + float(dur["return"])
                else:
                    return None
            elif isinstance(dur, (int, float)):
                secs = float(dur)
            else:
                return None
        except (TypeError, ValueError):
            return None
        return int(round(secs / 60.0))

    def parse_itin(itin):
        """Return (price_int, max_stops, is_nonstop, deep_link, duration_min, itin) or raise."""
        price = int(round(float(itin["price"])))
        route = itin["route"]
        outbound = [seg for seg in route if seg["return"] == 0]
        inbound = [seg for seg in route if seg["return"] == 1]
        out_stops = len(outbound) - 1
        in_stops = len(inbound) - 1
        max_stops = max(out_stops, in_stops)
        is_ns = max_stops == 0
        return price, max_stops, is_ns, itin.get("deep_link"), itin_duration(itin), itin

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
        "duration_min": cheapest[4],
        "nonstop_duration_min": ns[4] if ns else None,
        "airlines": _kiwi_airlines(cheapest[5]),
        "nonstop_airlines": _kiwi_airlines(ns[5]) if ns else None,
        "layovers": _kiwi_layovers(cheapest[5]),
    }


def _skyscanner_headers():
    """RapidAPI auth headers for the flights-sky host."""
    return {"x-rapidapi-host": RAPIDAPI_HOST, "x-rapidapi-key": RAPIDAPI_KEY}


def _skyscanner_get(url, params=None, retries_502=2, timeout=30):
    """GET a flights-sky endpoint, retrying a transient HTTP 502 a couple times.

    Returns the requests.Response (caller checks status_code), or None if every
    attempt raised an exception (e.g. a request-level timeout). A non-502 status is
    returned immediately for the caller to handle defensively. ``timeout`` bounds
    each request; callers pass a short value for poll requests so a hung session
    degrades to no-data in seconds rather than minutes (see skyscanner_fare).
    """
    for attempt in range(retries_502 + 1):
        try:
            r = requests.get(url, headers=_skyscanner_headers(), params=params, timeout=timeout)
        except Exception:
            return None
        # Retry only a transient 502, and only while attempts remain; on the final
        # attempt (attempt == retries_502) the 502 is returned for the caller to
        # handle, so the loop always returns and never falls through.
        if r.status_code == 502 and attempt < retries_502:
            time.sleep(0.4)
            continue
        return r


def _skyscanner_flatten_items(data):
    """Flatten all buckets[].items[] from a complete flights-sky response, deduped.

    Dedupe key is the item ``id`` when present (the same itinerary can appear in
    several buckets, e.g. Best + Cheapest), else identity. Returns a list of item
    dicts; an unexpected shape yields [].
    """
    itineraries = data.get("itineraries") if isinstance(data, dict) else None
    buckets = itineraries.get("buckets") if isinstance(itineraries, dict) else None
    if not isinstance(buckets, list):
        return []
    seen = set()
    out = []
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        for item in bucket.get("items") or []:
            if not isinstance(item, dict):
                continue
            key = item.get("id")
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            out.append(item)
    return out


def _skyscanner_price(item):
    """Numeric party-total price for an item (price.raw), or None if absent/non-numeric."""
    price = item.get("price")
    raw = price.get("raw") if isinstance(price, dict) else None
    return raw if isinstance(raw, (int, float)) else None


def _skyscanner_legs(item):
    """List of leg dicts for an item, or [] if absent/malformed."""
    legs = item.get("legs")
    return [leg for leg in legs if isinstance(leg, dict)] if isinstance(legs, list) else []


def _skyscanner_item_duration(item):
    """Sum legs[].durationInMinutes for an item; None if any leg's value is missing."""
    legs = _skyscanner_legs(item)
    if not legs:
        return None
    total = 0
    for leg in legs:
        d = leg.get("durationInMinutes")
        if not isinstance(d, (int, float)):
            return None
        total += d
    return int(round(total))


def _skyscanner_max_stops(item):
    """Max legs[].stopCount for an item; None if no leg carries a stopCount."""
    counts = [leg.get("stopCount") for leg in _skyscanner_legs(item)
              if isinstance(leg.get("stopCount"), (int, float))]
    return max(int(c) for c in counts) if counts else None


def _skyscanner_is_nonstop(item):
    """True iff ALL legs have stopCount == 0 (and there is at least one leg)."""
    legs = _skyscanner_legs(item)
    if not legs:
        return False
    for leg in legs:
        if leg.get("stopCount") != 0:
            return False
    return True


def _skyscanner_airlines(item):
    """Unique marketing carrier names across the item's legs (order-preserving)."""
    names = []
    for leg in _skyscanner_legs(item):
        carriers = leg.get("carriers")
        marketing = carriers.get("marketing") if isinstance(carriers, dict) else None
        for c in marketing or []:
            name = c.get("name") if isinstance(c, dict) else None
            if name and name not in names:
                names.append(name)
    return names


def _skyscanner_layovers(item):
    """Best-effort connection layovers for an item.

    For each leg, a layover is the connection airport between two consecutive
    segments: its IATA is the first segment's destination flightPlaceId. Duration
    between segments is not directly given by the API, so it is left None unless
    derivable; we keep it None (best-effort). Returns a list of
    ``{"iata", "duration_min"}`` dicts (possibly empty).
    """
    out = []
    for leg in _skyscanner_legs(item):
        segments = leg.get("segments")
        if not isinstance(segments, list):
            continue
        segs = [s for s in segments if isinstance(s, dict)]
        for i in range(len(segs) - 1):
            dest = segs[i].get("destination")
            iata = None
            if isinstance(dest, dict):
                iata = dest.get("flightPlaceId") or dest.get("displayCode")
            out.append({"iata": iata, "duration_min": None})
    return out


def skyscanner_fare(origin, dest, dep, ret, adults, children):
    """Real round-trip fares from RapidAPI flights-sky (Skyscanner data).

    3-step async flow: (1) search-roundtrip, (2) poll search-incomplete until the
    session is complete, (3) parse buckets[].items[]. Place IDs are IATA codes.
    Price is the party total for the passed adults AND children (both sent to the
    search so family quotes are not adults-only; verified live that ``children``
    changes the returned price.raw). Defensive: missing key, any non-200 (except a
    retried 502), no buckets/items, or unexpected shape → None. Never fabricates.
    """
    if not RAPIDAPI_KEY:
        return None
    base = f"https://{RAPIDAPI_HOST}"
    params = {
        "placeIdFrom": origin, "placeIdTo": dest,
        "departDate": dep, "returnDate": ret,
        "adults": adults, "children": children,
        "currency": "CAD", "market": "CA",
        "locale": "en-US", "cabinClass": "economy",
    }
    r = _skyscanner_get(
        f"{base}/web/flights/search-roundtrip",
        params=params,
        # (connect, read): short connect so a dead host fails fast; generous read is
        # an INACTIVITY timeout (fires only after that many seconds of NO data), not
        # a total cap — a slow-but-progressing search keeps running to completion.
        timeout=(SKYSCANNER_CONNECT_TIMEOUT, SKYSCANNER_SEARCH_TIMEOUT),
    )
    if r is None or r.status_code != 200:
        return None
    try:
        data = r.json().get("data", {})
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    # Step 2: poll until the session is complete (bounded).
    context = data.get("context") if isinstance(data.get("context"), dict) else {}
    if context.get("status") != "complete":
        session_id = context.get("sessionId")
        if not session_id:
            return None
        poll_url = f"{base}/web/flights/search-incomplete"
        completed = False
        # Longer-but-bounded budget: SKYSCANNER_POLL_ATTEMPTS x SKYSCANNER_POLL_INTERVAL
        # (default ~12 x 1.5s ~= 18s) gives most routes — incl. long-haul — time to
        # reach "complete" instead of falling back to serpapi. Still bounded: a short
        # per-poll request timeout plus break-on-Timeout means a hung session can't run
        # for minutes.
        for _ in range(SKYSCANNER_POLL_ATTEMPTS):
            time.sleep(SKYSCANNER_POLL_INTERVAL)
            # Short poll timeout so a hung/slow session can't tie up a search
            # worker for attempts x 30s; without it one fare cell could stall for minutes.
            pr = _skyscanner_get(
                poll_url,
                params={"sessionId": session_id},
                timeout=SKYSCANNER_POLL_TIMEOUT,
                # No inner 502 retry on polls: each configured poll attempt makes
                # exactly ONE HTTP call. The OUTER poll loop already provides the
                # retry/attempt budget, so a 502 just falls through to the next
                # attempt. This bounds total poll work at SKYSCANNER_POLL_ATTEMPTS x
                # (interval + per-poll timeout) instead of attempts x 3 HTTP calls.
                retries_502=0,
            )
            if pr is None:
                # A request-level timeout/network error returns None. Don't keep
                # retrying after a timeout — bail out and let _get_fare_uncached
                # fall through to the next provider quickly.
                break
            if pr.status_code != 200:
                continue
            try:
                data = pr.json().get("data", {})
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            ctx = data.get("context") if isinstance(data.get("context"), dict) else {}
            if ctx.get("status") == "complete":
                completed = True
                break
        if not completed:
            return None

    # Step 3: parse buckets[].items[].
    items = [it for it in _skyscanner_flatten_items(data)
             if _skyscanner_price(it) is not None]
    if not items:
        return None

    cheapest = min(items, key=_skyscanner_price)
    nonstops = [it for it in items if _skyscanner_is_nonstop(it)]
    ns = min(nonstops, key=_skyscanner_price) if nonstops else None

    book = None
    link = cheapest.get("deepLink") or cheapest.get("url")
    if isinstance(link, str) and link.startswith("http"):
        book = link

    return {
        "cheapest_cad": round(_skyscanner_price(cheapest)),
        "stops": _skyscanner_max_stops(cheapest),
        "nonstop_cad": round(_skyscanner_price(ns)) if ns else None,
        "duration_min": _skyscanner_item_duration(cheapest),
        "nonstop_duration_min": _skyscanner_item_duration(ns) if ns else None,
        "airlines": _skyscanner_airlines(cheapest),
        "nonstop_airlines": _skyscanner_airlines(ns) if ns else None,
        "layovers": _skyscanner_layovers(cheapest),
        "source": "skyscanner",
        "book": book,
    }


def _serpapi_airlines(entry):
    """Unique carrier names from a SerpApi flight entry's flights[].airline (order-preserving).

    ``entry`` is one best/other flight dict. Each segment in ``flights`` carries an
    ``airline`` display name. Returns a de-duplicated list of names (empty when the
    entry has no usable airline names — never fabricated).
    """
    names = []
    flights = entry.get("flights")
    if not isinstance(flights, list):
        return names
    for seg in flights:
        name = seg.get("airline") if isinstance(seg, dict) else None
        if name and name not in names:
            names.append(name)
    return names


def _serpapi_layovers(entry):
    """Per-connection layovers from a SerpApi entry's layovers[] (outbound leg).

    Maps each SerpApi layover ``{id, name, duration}`` (duration in minutes) to the
    app's ``{iata, name, duration_min}`` shape. Returns [] for a nonstop entry (no
    layovers key) and skips any malformed (non-dict) layover defensively.
    """
    out = []
    layovers = entry.get("layovers")
    if not isinstance(layovers, list):
        return out
    for lo in layovers:
        if not isinstance(lo, dict):
            continue
        out.append({
            "iata": lo.get("id"),
            "name": lo.get("name"),
            "duration_min": lo.get("duration"),
        })
    return out


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
    r = _request_with_retry("GET", "https://serpapi.com/search.json", params=params, timeout=30)
    if r is None or r.status_code != 200:
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
        airlines = _serpapi_airlines(cheapest)
        layovers = _serpapi_layovers(cheapest)
    except Exception:
        return None
    # SerpApi's round-trip response only describes the outbound leg, so we cannot
    # confirm a true round-trip nonstop without an extra departure_token request;
    # to avoid mislabeling we don't claim nonstop for this provider.
    nonstop_cad = None
    # SerpApi's single-call round-trip response is outbound-only, so total round-trip
    # duration is not available (matching its nonstop_cad=None contract); a future
    # return-leg fetch (departure_token) could populate it.
    duration_min = None
    nonstop_duration_min = None
    return {
        "cheapest_cad": cheapest_cad,
        "stops": stops,
        "nonstop_cad": nonstop_cad,
        "source": "serpapi",
        "book": None,
        "duration_min": duration_min,
        "nonstop_duration_min": nonstop_duration_min,
        "airlines": airlines,
        # No confirmed round-trip nonstop for serpapi (see nonstop_cad above), so
        # there is no nonstop itinerary to attribute carriers to.
        "nonstop_airlines": None,
        "layovers": layovers,
    }


# Single source of truth for the provider chain: (name, fare_function) in the
# ordered-fallback priority order. The name MUST match the label producible by
# providers_configured() so compare mode can skip unconfigured providers.
_PROVIDER_CHAIN = (
    ("skyscanner", lambda *a: skyscanner_fare(*a)),
    ("serpapi", lambda *a: serpapi_fare(*a)),
    ("amadeus", lambda *a: amadeus_fare(*a)),
    ("travelpayouts", lambda *a: travelpayouts_fare(*a)),
    ("kiwi", lambda *a: kiwi_fare(*a)),
)


def _no_data_fare():
    """The no-data sentinel returned when no provider yields a real price."""
    return {"cheapest_cad": None, "stops": None, "nonstop_cad": None,
            "source": "no-data", "duration_min": None,
            "nonstop_duration_min": None, "airlines": None,
            "nonstop_airlines": None, "layovers": None, "alternatives": None}


def _demo_seed(origin, dest, dep, ret, adults, children):
    """A stable non-negative integer derived from the cell inputs.

    Uses md5 over the joined inputs so the same cell always yields the same
    sample numbers across processes and restarts (no randomness — required so a
    resumed/streamed search is deterministic), yet varies by route AND date so
    the demo grid looks realistic rather than uniform.
    """
    import hashlib
    raw = "|".join(str(x) for x in (origin, dest, dep, ret, adults, children))
    return int(hashlib.md5(raw.encode("utf-8")).hexdigest(), 16)


def demo_fare(origin, dest, dep, ret, adults, children):
    """Return a DETERMINISTIC, obviously-fake SAMPLE fare for demo mode (#44).

    NOT a real price and NEVER derived from any provider: every value is computed
    from a hash of the cell inputs so the grid is plausible-but-clearly-sample and
    stable across restarts/streaming. The carrier is the obviously-fake "DemoAir"
    and the result is tagged ``source="demo"`` so it can never be mistaken for or
    silently substituted for a real fare. Always priced (never None-priced) and in
    the normalized get_fare dict shape. Children/adults nudge the party total so a
    family quote differs, like the real providers. Produced ONLY when DEMO_MODE is
    on (callers gate on it); this function itself calls no provider and no network.
    """
    seed = _demo_seed(origin, dest, dep, ret, adults, children)
    pax = max(1, adults + children)
    # Per-person base 600..1399, scaled to the party — clearly sample magnitudes.
    base_pp = 600 + (seed % 800)
    cheapest = base_pp * pax
    stops = (seed >> 8) % 3  # 0..2 connections on the cheapest sample
    # Durations: cheapest 8h..23h59m; nonstop a bit shorter. Minutes, deterministic.
    duration = 480 + (seed >> 12) % 960
    nonstop_duration = max(300, duration - 60 - (seed >> 16) % 180)
    # A clearly-fake connection airport for any non-nonstop sample.
    layover_codes = ["DMO", "FAK", "SMP", "XXX"]
    layover_iata = layover_codes[(seed >> 20) % len(layover_codes)]
    layovers = ([{"iata": layover_iata, "duration_min": 60 + (seed >> 24) % 180}]
                if stops else [])
    if stops == 0:
        # Cheapest IS already nonstop: nonstop fields must agree exactly so
        # _chosen_price_under_threshold never promotes a fabricated higher price.
        nonstop = cheapest
        nonstop_duration = duration
        nonstop_airlines = ["DemoAir"]
    else:
        # Connecting cheapest: a pricier nonstop alternative is realistic.
        nonstop = round(cheapest * (1 + (5 + (seed >> 4) % 20) / 100.0))
        nonstop_airlines = ["DemoAir"]
    return {
        "cheapest_cad": cheapest,
        "stops": stops,
        "nonstop_cad": nonstop,
        "source": "demo",
        "book": None,  # omit so _build_cell falls back to child-aware kayak_link (#44)
        "duration_min": duration,
        "nonstop_duration_min": nonstop_duration,
        "airlines": ["DemoAir"],
        "nonstop_airlines": nonstop_airlines,
        "layovers": layovers,
    }


def _chosen_price_under_threshold(fare, nonstop_threshold):
    """The post-threshold "chosen" price for a fare dict (#43).

    nonstop_threshold is a float fraction (e.g. 0.25 for 25%). Returns
    ``(chosen_label, chosen_cad)`` where chosen_label is "nonstop" when the
    nonstop fare is within the threshold of the cheapest fare
    (i.e. ``nonstop_cad <= cheapest_cad * (1 + nonstop_threshold)``) — in which
    case chosen_cad is nonstop_cad — and "cheapest" otherwise (chosen_cad =
    cheapest_cad). This is the SINGLE source of truth shared by _build_cell
    (display) and the compare-winner selection so both agree on best value.

    chosen_cad can be None when cheapest_cad is None (no-data sentinel).
    """
    cheap = fare.get("cheapest_cad")
    ns = fare.get("nonstop_cad")
    if ns and cheap and ns <= cheap * (1 + nonstop_threshold):
        return "nonstop", ns
    return "cheapest", cheap


def _get_fare_compare(origin, dest, dep, ret, adults, children, nonstop_threshold=0.0):
    """Query ALL configured providers concurrently; pick the cheapest result.

    True cross-provider comparison (opt-in, #43). Only providers whose keys are
    configured (see providers_configured()) are queried, so we never call a
    provider with unset credentials. Providers run concurrently in a small pool
    bounded by the provider count so comparison isn't N× slower in wall-clock;
    each provider call already has its own bounded timeouts/retries.

    Concurrency note: under run_search's outer grid pool (SEARCH_CONCURRENCY
    workers) this inner pool nests, so peak in-flight provider requests are
    bounded by SEARCH_CONCURRENCY × len(configured providers) (≤ 5). That is the
    deliberate cost of compare mode (the #37 cell cap still bounds total cells);
    each provider's own timeout/retry budget keeps any single request bounded.

    Returns the dict for the lowest POST-THRESHOLD chosen price (the same metric
    _build_cell uses to pick the displayed fare, via _chosen_price_under_threshold
    with nonstop_threshold), with an extra "alternatives" list of
    {"source", "cheapest_cad", "chosen_cad"} for the OTHER real results (ascending
    by chosen price). Picking by the chosen price — not raw cheapest_cad — keeps
    the compared winner consistent with what _build_cell will actually show (#43,
    codex P2): otherwise a provider that's cheaper on cheapest_cad but pricier on
    its post-threshold chosen fare could win yet display a worse value than an
    already-fetched rival. All-no-data -> the no-data sentinel (alternatives []).
    """
    configured = set(providers_configured())
    chain = [(name, fn) for name, fn in _PROVIDER_CHAIN if name in configured]

    def _call(item):
        name, fn = item
        try:
            res = fn(origin, dest, dep, ret, adults, children)
            if res and res.get("cheapest_cad"):
                return res
        except Exception:
            pass
        return None

    results = []
    if chain:
        workers = max(1, min(len(chain), SEARCH_CONCURRENCY))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for res in pool.map(_call, chain):
                if res is not None:
                    results.append(res)

    if not results:
        sentinel = _no_data_fare()
        sentinel["alternatives"] = []
        return sentinel

    # Lowest POST-THRESHOLD chosen price wins; ties resolve to the earlier
    # provider (stable sort keeps the chain/priority order on equal prices). We
    # rank by the same _chosen_price_under_threshold metric _build_cell applies,
    # so the winner is the provider _build_cell would actually display cheapest.
    results.sort(
        key=lambda r: _chosen_price_under_threshold(r, nonstop_threshold)[1]
    )
    chosen = dict(results[0])
    chosen["alternatives"] = [
        {"source": r.get("source"), "cheapest_cad": r.get("cheapest_cad"),
         "chosen_cad": _chosen_price_under_threshold(r, nonstop_threshold)[1]}
        for r in results[1:]
    ]
    return chosen


def _get_fare_uncached(origin, dest, dep, ret, adults, children, compare=False,
                       nonstop_threshold=0.0):
    """Resolve a fare for one cell from the configured providers.

    compare=False (default): ordered-fallback — try each provider in priority
    order and return the FIRST real priced result (1 call/cell). nonstop_threshold
    is ignored on this path (no cross-provider winner to pick).
    compare=True: query ALL configured providers and return the one with the
    lowest POST-THRESHOLD chosen price (nonstop_threshold, a float fraction), with
    an "alternatives" list (see _get_fare_compare). Uses (configured) calls/cell.

    DEMO_MODE short-circuits BOTH paths: it returns a clearly-labeled
    ``demo_fare`` (source="demo") and NEVER calls a real provider — even with
    compare on (a demo result has no real cross-provider comparison, so it's
    returned as-is). This is the sole sanctioned exception to real-data-only and is
    gated solely on the explicit DEMO_MODE flag, never a provider-failure fallback.
    """
    if DEMO_MODE:
        return demo_fare(origin, dest, dep, ret, adults, children)
    if compare:
        return _get_fare_compare(origin, dest, dep, ret, adults, children,
                                 nonstop_threshold)
    for provider in (skyscanner_fare, serpapi_fare, amadeus_fare, travelpayouts_fare, kiwi_fare):
        try:
            res = provider(origin, dest, dep, ret, adults, children)
            if res and res.get("cheapest_cad"):
                return res
        except Exception:
            continue
    return _no_data_fare()


def _compare_cache_tag(compare, nonstop_threshold=0.0):
    """The 7th fare-cache-key element: distinguishes fallback from compare AND,
    for compare, pins the EXACT configured-provider set + the nonstop threshold.

    Ordered fallback -> ``False`` (a compared result never serves a fallback
    request and vice-versa); the fallback key is INDEPENDENT of nonstop_threshold
    (the threshold never affects the fallback result, so its key must not change).
    Compare -> ``"cmp:" + sorted provider names + ":th=" + threshold``, so a
    compared result computed against one provider set is NOT reused after a
    provider key is added/removed (which would otherwise silently skip a
    newly-configured provider until TTL expiry, defeating "compare ALL
    providers"), AND two compare searches with DIFFERENT thresholds cache
    separately — the compared winner now depends on nonstop_threshold (#43), so a
    25%-threshold result must not serve a 0%-threshold request. Always a single
    hashable, JSON-round-trippable scalar, so the persisted-cache key stays
    exactly 7 elements.
    """
    if not compare:
        return False
    return ("cmp:" + ",".join(sorted(providers_configured()))
            + ":th=" + repr(float(nonstop_threshold)))


def get_fare(origin, dest, dep, ret, adults, children, compare=False,
             nonstop_threshold=0.0):
    """Collect REAL pricing from configured flight APIs. No AI here.

    Results with real prices are cached in _fare_cache for FARE_CACHE_TTL seconds
    and persisted to FARE_CACHE_PATH so they survive a restart (within TTL).
    No-data sentinels are never cached.  Set FARE_CACHE_TTL <= 0 to disable caching.

    compare gates ordered-fallback (False) vs cross-provider comparison (True);
    it is part of the cache key (via _compare_cache_tag, which also pins the
    configured-provider set AND the nonstop_threshold in compare mode) so a
    compared result never serves a fallback request — nor a request made against a
    different provider set or a different nonstop threshold. nonstop_threshold (a
    float fraction) only affects compare mode (the post-threshold winner, #43);
    the fallback path and its cache key are unaffected.
    """
    # DEMO_MODE: return the clearly-labeled sample fare WITHOUT touching the real
    # fare cache (neither reading nor writing FARE_CACHE_PATH), so demo data can
    # never land in / be served from the persistent real-fare cache. demo_fare is
    # itself deterministic, so a per-cell cache buys nothing here.
    if DEMO_MODE:
        return _get_fare_uncached(origin, dest, dep, ret, adults, children,
                                  compare, nonstop_threshold)

    if FARE_CACHE_TTL <= 0:
        return _get_fare_uncached(origin, dest, dep, ret, adults, children,
                                  compare, nonstop_threshold)

    key = (origin, dest, dep, ret, adults, children,
           _compare_cache_tag(compare, nonstop_threshold))
    now = time.time()
    entry = _fare_cache.get(key)
    if entry is not None:
        fetched, cached_result = entry
        if now - fetched < FARE_CACHE_TTL:  # fresh under the current TTL
            return cached_result  # in-memory fast path; hits never write to disk

    result = _get_fare_uncached(origin, dest, dep, ret, adults, children,
                                compare, nonstop_threshold)
    if result.get("cheapest_cad"):
        with _fare_cache_lock:
            _fare_cache[key] = (now, result)  # store fetch time, not absolute expiry
            _save_fare_cache()  # persist atomically; no-op when persistence disabled
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
    # `demo` is an explicit flag (in ADDITION to providers==["demo"]) so the UI can
    # unambiguously show the prominent "DEMO DATA — NOT real prices" banner (#44).
    return jsonify({"ollama": ollama_ok(), "model": OLLAMA_MODEL,
                    "providers": providers_configured(), "demo": DEMO_MODE})

@app.route("/api/top-cities", methods=["POST"])
@rate_limited("api", lambda: API_RATE_PER_MIN)
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

@app.route("/api/suggest")
@rate_limited("api", lambda: API_RATE_PER_MIN)
def api_suggest():
    """GET /api/suggest?q=<text> — type-ahead destination suggestions.

    Returns ``{"suggestions": [...]}`` with up to ~10 country/city matches.
    A blank/too-short query returns an empty list.
    """
    q = request.args.get("q", "")
    return jsonify({"suggestions": suggest_destinations(q)})


@app.route("/api/resolve", methods=["POST"])
@rate_limited("api", lambda: API_RATE_PER_MIN)
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

    fare: dict from get_fare() with keys cheapest_cad, stops, nonstop_cad, source, book,
    duration_min, nonstop_duration_min.
    threshold: float fraction (e.g. 0.25 for 25 %).
    Returns dict with keys: dep, ret, cheapest_cad, stops, nonstop_cad, chosen,
    chosen_cad, source, duration_min, nonstop_duration_min, chosen_duration_min,
    airlines, nonstop_airlines, chosen_airlines, layovers, chosen_layovers, book.
    """
    cheap = fare.get("cheapest_cad")
    ns = fare.get("nonstop_cad")
    # Same helper the compare-winner selection uses (#43), so the displayed pick
    # is consistent with how compare ranks providers (post-threshold chosen price).
    chosen, chosen_cad = _chosen_price_under_threshold(fare, threshold)
    # Keep each price line paired with ITS OWN stops/duration (codex P2):
    #   - duration_min ALWAYS = cheapest itinerary's duration (pairs with cheapest_cad + stops)
    #   - nonstop_duration_min = nonstop itinerary's duration (pairs with nonstop_cad, 0 stops)
    #   - chosen_duration_min = duration of the SELECTED fare (pairs with chosen_cad)
    # Each carries None (no fabrication) when its itinerary's duration is unavailable.
    duration_min = fare.get("duration_min")
    nonstop_duration_min = fare.get("nonstop_duration_min")
    chosen_duration_min = nonstop_duration_min if chosen == "nonstop" else duration_min
    stops = fare.get("stops")
    # chosen_stops pairs with chosen_cad + chosen_duration_min: a nonstop has 0 stops
    # by definition, otherwise it's the cheapest itinerary's stop count (codex P2:
    # never pair the nonstop's duration with the connecting fare's stop count).
    chosen_stops = 0 if chosen == "nonstop" else stops
    # airlines + layovers describe the CHEAPEST itinerary (the cell's primary price
    # line shows cheapest_cad with its stops/duration, so the layover list pairs with
    # that line). airlines is the fare's carrier list (names or codes; None when the
    # provider can't supply it). layovers is the fare's per-connection list ([] for a
    # nonstop cheapest, None when the provider gives no per-stop detail, e.g.
    # travelpayouts). chosen_layovers pairs with the CHOSEN pick: [] when the nonstop
    # line is selected (a nonstop has no layovers by definition).
    airlines = fare.get("airlines")
    layovers = fare.get("layovers")
    chosen_layovers = [] if chosen == "nonstop" else layovers
    # chosen_airlines pairs with the CHOSEN pick (codex P2): when the nonstop line
    # is selected its carriers come from the nonstop itinerary (nonstop_airlines),
    # otherwise the cheapest itinerary's airlines. Keeps the chosen/best summary +
    # recommendation from showing the connecting fare's carriers next to the nonstop
    # price/stops/duration. (None when the provider gives no nonstop carrier detail.)
    nonstop_airlines = fare.get("nonstop_airlines")
    chosen_airlines = nonstop_airlines if chosen == "nonstop" else airlines
    return {
        "dep": dep, "ret": ret,
        "cheapest_cad": cheap, "stops": stops,
        "nonstop_cad": ns, "chosen": chosen, "chosen_cad": chosen_cad,
        "chosen_stops": chosen_stops,
        "source": fare.get("source"),
        "duration_min": duration_min,
        "nonstop_duration_min": nonstop_duration_min,
        "chosen_duration_min": chosen_duration_min,
        "airlines": airlines,
        "nonstop_airlines": nonstop_airlines,
        "chosen_airlines": chosen_airlines,
        "layovers": layovers,
        "chosen_layovers": chosen_layovers,
        # alternatives (#43): other providers' real prices for this cell in compare
        # mode, [{"source","cheapest_cad"}] ascending. None/absent in fallback mode.
        "alternatives": fare.get("alternatives"),
        "book": fare.get("book") or kayak_link(origin, code, dep, ret, adults, child_ages),
    }


def run_search(origin, dests, adults, child_ages, dep_dates, ret_dates,
               threshold_pct=25, families=1, compare=False):
    """Core best-value search shared by the web route and the CLI.

    origin: IATA str; dests: list of {"city","iata"}; child_ages: list[int];
    threshold_pct: nonstop premium % (e.g. 25). compare: when True, every cell
    queries ALL configured providers and keeps the cheapest, attaching each
    cell's `alternatives` (#43); default False = 1 provider call/cell (ordered
    fallback). Returns dict with keys origin, adults, child_ages, families,
    dep_dates, ret_dates, results, recommendation, providers.

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
        return (di, dpi, ri), get_fare(origin, code, dep, ret, adults, children,
                                       compare, threshold)

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
    # Opt-in cross-provider comparison (#43). Default False: ordered fallback,
    # one provider call per cell. True multiplies calls by configured-provider
    # count, so it must be explicitly requested.
    compare = bool(b.get("compare_providers", False))

    if not origin or not dests or not dep_dates or not ret_dates:
        return None

    return dict(
        origin=origin, dests=dests, adults=adults, child_ages=child_ages,
        dep_dates=dep_dates, ret_dates=ret_dates,
        threshold_pct=threshold_pct, families=families, compare=compare,
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
@rate_limited("search", lambda: SEARCH_RATE_PER_MIN)
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
@rate_limited("search", lambda: SEARCH_RATE_PER_MIN)
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
    compare = args["compare"]

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
                fare = get_fare(origin, code, dep, ret, adults, children,
                                compare, threshold)
            except Exception:
                fare = _no_data_fare()
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
                    "chosen_stops": None,
                    "duration_min": None, "nonstop_duration_min": None,
                    "chosen_duration_min": None,
                    "airlines": None, "nonstop_airlines": None,
                    "chosen_airlines": None,
                    "layovers": None, "chosen_layovers": None,
                    "alternatives": None,
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
@rate_limited("search", lambda: SEARCH_RATE_PER_MIN)
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
@rate_limited("search", lambda: SEARCH_RATE_PER_MIN)
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
        "children": row.get("children") or 0,
        "child_ages": row.get("child_ages") or [],
        "threshold_pct": row.get("threshold_pct"),
        "last_price": row.get("last_price"),
        "last_source": row.get("last_source"),
    }


@app.route("/api/watch", methods=["POST"])
@rate_limited("api", lambda: API_RATE_PER_MIN)
def api_watch_add():
    # request.get_json() can return None (no body) or a non-dict (client posts
    # `null`, `[]`, a bare string/number); a subsequent b.get(...) would raise
    # AttributeError -> 500. Reject anything that isn't a JSON object up front.
    b = request.get_json(silent=True)
    if not isinstance(b, dict):
        return jsonify({"error": "watch payload required"}), 400
    # Required text fields are .strip()ed below; a non-string value (e.g.
    # {"origin": 123} or a list/bool) would make .strip() raise AttributeError
    # -> 500. Validate each is a string up front and reject with a clean 400,
    # consistent with the other field validations.
    for _field in ("origin", "dest_iata", "dep_date", "ret_date"):
        if not isinstance(b.get(_field), str):
            return jsonify(
                {"error": "origin, dest_iata, dep_date and ret_date must be strings"}
            ), 400
    origin = b["origin"].strip().upper()
    dest_iata = b["dest_iata"].strip().upper()
    dep_date = b["dep_date"].strip()
    ret_date = b["ret_date"].strip()
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
    # (get_fare). The watch baseline always uses ordered fallback (compare=False)
    # — a cheap single provider call the scheduler can re-price the same way. For
    # a default (non-compare) search the cell is usually a cache HIT so this is
    # free; if the user searched in compare mode the fallback key differs, so this
    # does one real ordered-fallback fetch (still cheap, one call). The client's
    # last_price/last_source are ignored entirely.
    # DEMO_MODE (#44): demo fares are clearly-labeled SAMPLE data and must NEVER be
    # persisted (here, into WATCH_DB) — a fake baseline would drive bogus scheduler
    # drop alerts and let demo data leak out of the demo path. Skip the lookup
    # entirely in demo mode so the watch is created with NO baseline (the scheduler,
    # which uses real providers, seeds the first real price later). Demo and real
    # never mix.
    fare = (_no_data_fare() if DEMO_MODE
            else get_fare(origin, dest_iata, dep_date, ret_date, adults, len(child_ages)))
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
@rate_limited("api", lambda: API_RATE_PER_MIN)
def api_watch_list():
    db = _watch_db()
    try:
        rows = db.list_watches()
    finally:
        db.close()
    return jsonify({"watches": [_watch_to_json(r) for r in rows]})


@app.route("/api/watch/<int:watch_id>", methods=["DELETE"])
@rate_limited("api", lambda: API_RATE_PER_MIN)
def api_watch_remove(watch_id):
    db = _watch_db()
    try:
        db.remove_watch(watch_id)
    finally:
        db.close()
    return jsonify({"ok": True})


def _fmt_duration(minutes):
    """Render total minutes as a human "Xh Ym" string, or None if minutes is None."""
    if minutes is None:
        return None
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m}m"


def _layover_summary(layovers):
    """Compact human string for a cell's layovers, or None when there are none.

    ``[]`` (nonstop / no connections) → None. Each layover renders as its IATA with
    an optional "(Xh Ym)" when the duration is known: "PEK (1h 20m), NRT". A None
    layovers value (provider gave no per-stop detail) → None.
    """
    if not layovers:
        return None
    parts = []
    for lo in layovers:
        iata = lo.get("iata") or "?"
        dur = _fmt_duration(lo.get("duration_min"))
        parts.append(f"{iata} ({dur})" if dur else iata)
    return ", ".join(parts)


def build_recommendation(origin, results, adults, child_ages, families):
    bests = [{"city": r["city"], "iata": r["iata"],
              "price_per_family": r["best"]["chosen_cad"] if r["best"] else None,
              "dep": r["best"]["dep"] if r["best"] else None,
              "ret": r["best"]["ret"] if r["best"] else None,
              "chosen": r["best"]["chosen"] if r["best"] else None,
              "stops": r["best"].get("chosen_stops") if r["best"] else None,
              "duration_min": r["best"].get("chosen_duration_min") if r["best"] else None,
              "duration": _fmt_duration(r["best"].get("chosen_duration_min")) if r["best"] else None,
              "airlines": r["best"].get("chosen_airlines") if r["best"] else None,
              "layovers": _layover_summary(r["best"].get("chosen_layovers")) if r["best"] else None}
             for r in results]
    summary = (f"From {origin}, {adults} adults + {len(child_ages)} kids, "
               f"{families} family/families. Per-family best options: "
               + json.dumps(bests))
    # In demo mode the data is locally generated SAMPLE fares, NOT real prices, so
    # the prompt must say so — never tell the LLM it came from live flight APIs (#44).
    data_provenance = (
        "The data below is LOCALLY GENERATED SAMPLE DATA for a product demo — these are "
        "NOT real fares and were NOT collected from any flight API; treat them as illustrative only"
        if DEMO_MODE else
        "The data below was COLLECTED FROM LIVE FLIGHT APIs"
    )
    prompt = (
        "You are a savvy travel planner for someone with FLEXIBLE dates who wants the most "
        "cost-effective vacation. " + data_provenance + " (prices are "
        "CAD per family; `duration_min` is total round-trip flight time in minutes, `duration` is "
        "the same as a human-readable string, and may be null when unknown; `airlines` lists the "
        "operating carrier(s) and `layovers` summarizes connection airports/durations, either of "
        "which may be null when the provider does not supply it). Analyze ONLY this "
        "data — do not invent prices, durations, airlines or layovers. Pick the single best-value "
        "trip and explain in 2-3 short sentences why, balancing price, stops/nonstop, dates, total "
        "flight duration AND airline/layover convenience. "
        "Do NOT recommend a much-longer flight (e.g. one taking roughly 2x the fastest comparable "
        "option) merely because it is the cheapest — a modest saving rarely justifies a vastly "
        "longer trip. Then give a one-line runner-up. Be concise.\n\n" + summary
    )
    # In demo mode, prefix the summary so even the AI/fallback text is unmistakably
    # labeled as based on SAMPLE data, never real fares (#44).
    demo_prefix = "(DEMO — sample fares, NOT real prices) " if DEMO_MODE else ""
    try:
        return demo_prefix + ollama_chat(prompt)
    except Exception as e:
        # graceful fallback: cheapest by price
        valid = [b for b in bests if b["price_per_family"]]
        if not valid:
            return demo_prefix + "No priceable options found."
        top = min(valid, key=lambda b: b["price_per_family"])
        return (f"{demo_prefix}Best value: {top['city']} ({top['iata']}) at ~CA${top['price_per_family']:,}"
                f"/family, {top['dep']} → {top['ret']}, {top['chosen']}. "
                f"(AI summary unavailable: {e})")

# Load any disk-persisted fare cache at import (#42), after the cache, config, and
# helpers are defined. Guarded/no-op when persistence is disabled or the file is
# missing/corrupt, so import never fails on a bad cache file.
_load_fare_cache()

if __name__ == "__main__":
    print(f"Whenever -> http://localhost:{PORT}  (model={OLLAMA_MODEL}, "
          f"providers={providers_configured() or 'none'})")
    app.run(host="127.0.0.1", port=PORT, debug=True)
