import app as appmod


def _amadeus_offer(grand_total, segs, durations=("PT2H", "PT2H")):
    """Build a 2-itinerary (out+return) offer. `durations` are ISO-8601 leg durations."""
    return {
        "price": {"grandTotal": str(grand_total)},
        "itineraries": [
            {"segments": [{} for _ in range(segs)], "duration": durations[0]},
            {"segments": [{} for _ in range(segs)], "duration": durations[1]},
        ],
    }


def test_amadeus_token_none_without_creds(monkeypatch):
    monkeypatch.setattr(appmod, "AMADEUS_ID", None)
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", None)
    assert appmod.amadeus_token() is None


def test_amadeus_token_fetch_then_cache(monkeypatch, fake_resp):
    monkeypatch.setattr(appmod, "AMADEUS_ID", "id")
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", "secret")
    calls = {"n": 0}

    def fake_post(url, data=None, timeout=None):
        calls["n"] += 1
        return fake_resp({"access_token": "T123", "expires_in": 1799})

    monkeypatch.setattr(appmod.requests, "post", fake_post)
    assert appmod.amadeus_token() == "T123"
    # second call uses the cached token, no new POST
    monkeypatch.setattr(appmod.requests, "post",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("refetched")))
    assert appmod.amadeus_token() == "T123"
    assert calls["n"] == 1


def test_amadeus_token_persistent_503_returns_none(monkeypatch, fake_resp):
    """Persistent 503 on the token endpoint must return None (not raise)."""
    monkeypatch.setattr(appmod, "AMADEUS_ID", "id")
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", "secret")
    monkeypatch.setattr(appmod, "_request_with_retry",
                        lambda *a, **k: fake_resp({}, status=503))
    assert appmod.amadeus_token() is None


def test_amadeus_token_persistent_429_returns_none(monkeypatch, fake_resp):
    """Persistent 429 on the token endpoint must return None (not raise)."""
    monkeypatch.setattr(appmod, "AMADEUS_ID", "id")
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", "secret")
    monkeypatch.setattr(appmod, "_request_with_retry",
                        lambda *a, **k: fake_resp({}, status=429))
    assert appmod.amadeus_token() is None


def test_amadeus_fare_token_503_gracefully_degrades(monkeypatch, fake_resp):
    """If token retrieval gets a persistent 5xx, amadeus_fare returns None (no raise)."""
    monkeypatch.setattr(appmod, "AMADEUS_ID", "id")
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", "secret")
    monkeypatch.setattr(appmod, "_request_with_retry",
                        lambda *a, **k: fake_resp({}, status=503))
    result = appmod.amadeus_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert result is None


def test_amadeus_fare_none_without_token(monkeypatch):
    monkeypatch.setattr(appmod, "amadeus_token", lambda: None)
    assert appmod.amadeus_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_amadeus_fare_picks_cheapest_and_nonstop(monkeypatch, fake_resp):
    monkeypatch.setattr(appmod, "amadeus_token", lambda: "T")
    offers = [
        _amadeus_offer(8000, 2, durations=("PT5H", "PT5H")),   # 1 stop, cheapest -> 600
        _amadeus_offer(14000, 1, durations=("PT2H", "PT2H")),  # nonstop -> 240
    ]
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": offers}, status=200))
    res = appmod.amadeus_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    # duration_min = cheapest itinerary (PT5H+PT5H=600); nonstop_duration_min = chosen
    # NONSTOP offer's own duration (PT2H+PT2H=240), distinct from the cheapest.
    # The cheapest 1-stop offer's segments are bare {} (no carrierCode/iataCode/at),
    # so airlines is [] and the single connection per itinerary has iata/duration None.
    assert res == {"cheapest_cad": 8000, "stops": 1, "nonstop_cad": 14000,
                   "source": "amadeus", "duration_min": 600,
                   "nonstop_duration_min": 240, "airlines": [],
                   # bare-{} nonstop segments carry no carrierCode → [] (not fabricated)
                   "nonstop_airlines": [],
                   "layovers": [{"iata": None, "duration_min": None},
                                {"iata": None, "duration_min": None}]}


def test_amadeus_fare_non_200(monkeypatch, fake_resp):
    monkeypatch.setattr(appmod, "amadeus_token", lambda: "T")
    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: fake_resp({}, status=429))
    assert appmod.amadeus_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_amadeus_fare_empty(monkeypatch, fake_resp):
    monkeypatch.setattr(appmod, "amadeus_token", lambda: "T")
    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: fake_resp({"data": []}))
    assert appmod.amadeus_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_travelpayouts_none_without_token(monkeypatch):
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", None)
    assert appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_travelpayouts_scales_and_builds_book_link(monkeypatch, fake_resp):
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "tok")
    data = [
        {"price": 1000, "transfers": 1, "return_transfers": 0, "link": "/deal/abc",
         "duration": 900},
        {"price": 1500, "transfers": 0, "return_transfers": 0, "link": "/ns",
         "duration": 600},
    ]
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": data}, status=200))
    res = appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 1)
    # pax = 3, cheapest 1000*3=3000, nonstop 1500*3=4500
    assert res["cheapest_cad"] == 3000
    assert res["nonstop_cad"] == 4500
    assert res["stops"] == 1
    assert res["source"] == "travelpayouts"
    assert res["book"] == "https://www.aviasales.com/deal/abc"
    # duration_min = cheapest itinerary (900); nonstop_duration_min = chosen nonstop (600)
    assert res["duration_min"] == 900
    assert res["nonstop_duration_min"] == 600
    # nonstop option here carries no airline field → nonstop_airlines None
    assert res["nonstop_airlines"] is None


def test_travelpayouts_nonstop_airlines_from_nonstop_option(monkeypatch, fake_resp):
    """travelpayouts: nonstop_airlines = the NONSTOP option's own carrier code,
    distinct from the cheapest's, and the nonstop-chosen cell uses it (codex P2)."""
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "tok")
    data = [
        {"price": 1000, "transfers": 1, "return_transfers": 0, "airline": "AC"},
        {"price": 1100, "transfers": 0, "return_transfers": 0, "airline": "AF"},
    ]
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": data}, status=200))
    res = appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["airlines"] == ["AC"]                 # cheapest's carrier
    assert res["nonstop_airlines"] == ["AF"]         # nonstop option's own carrier
    # nonstop is within threshold → chosen=nonstop → chosen_airlines uses nonstop_airlines
    cell = appmod._build_cell("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, [], res, 0.25)
    assert cell["chosen"] == "nonstop"
    assert cell["chosen_airlines"] == ["AF"]


def test_travelpayouts_no_nonstop_airlines_none(monkeypatch, fake_resp):
    """travelpayouts: no nonstop option → nonstop_airlines None."""
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "tok")
    data = [{"price": 1000, "transfers": 1, "return_transfers": 0, "airline": "AC"}]
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": data}, status=200))
    res = appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["nonstop_airlines"] is None


def test_travelpayouts_non_200(monkeypatch, fake_resp):
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "tok")
    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: fake_resp({}, status=500))
    assert appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_travelpayouts_empty(monkeypatch, fake_resp):
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "tok")
    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: fake_resp({"data": []}))
    assert appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_get_fare_returns_first_valid(monkeypatch):
    monkeypatch.setattr(appmod, "amadeus_fare", lambda *a: None)
    monkeypatch.setattr(appmod, "travelpayouts_fare",
                        lambda *a: {"cheapest_cad": 4200, "source": "travelpayouts"})
    res = appmod.get_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["source"] == "travelpayouts"


def test_get_fare_provider_order(monkeypatch):
    """amadeus must be tried BEFORE travelpayouts (provider tuple ordering contract)."""
    call_order = []
    monkeypatch.setattr(appmod, "amadeus_fare",
                        lambda *a: call_order.append("amadeus") or None)
    monkeypatch.setattr(appmod, "travelpayouts_fare",
                        lambda *a: call_order.append("travelpayouts") or {"cheapest_cad": 1, "source": "travelpayouts"})
    appmod.get_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert call_order == ["amadeus", "travelpayouts"]


def test_get_fare_skips_exceptions(monkeypatch):
    def boom(*a):
        raise RuntimeError("provider down")

    monkeypatch.setattr(appmod, "amadeus_fare", boom)
    monkeypatch.setattr(appmod, "travelpayouts_fare",
                        lambda *a: {"cheapest_cad": 100, "source": "travelpayouts"})
    assert appmod.get_fare("YYZ", "PVG", "d", "r", 1, 0)["cheapest_cad"] == 100


def test_get_fare_no_data(monkeypatch):
    monkeypatch.setattr(appmod, "amadeus_fare", lambda *a: None)
    monkeypatch.setattr(appmod, "travelpayouts_fare", lambda *a: None)
    monkeypatch.setattr(appmod, "kiwi_fare", lambda *a: None)
    res = appmod.get_fare("YYZ", "PVG", "d", "r", 1, 0)
    assert res == {"cheapest_cad": None, "stops": None, "nonstop_cad": None,
                   "source": "no-data", "duration_min": None,
                   "nonstop_duration_min": None, "airlines": None,
                   "nonstop_airlines": None, "layovers": None,
                   "alternatives": None}


# ---------------------------------------------------------------------------
# cross-provider compare mode (#43)
# ---------------------------------------------------------------------------

def _configure_all(monkeypatch):
    """Mark every provider as configured so compare mode queries all of them."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")       # skyscanner
    monkeypatch.setattr(appmod, "SERPAPI_KEY", "k")        # serpapi
    monkeypatch.setattr(appmod, "AMADEUS_ID", "k")
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", "k")     # amadeus
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "k")  # travelpayouts
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")       # kiwi
    # RAPIDAPI_KEY also configures the (primary) google provider; default it to
    # no-data so compare tests that only exercise the OTHER providers stay
    # deterministic (no live call) unless a test overrides it explicitly.
    monkeypatch.setattr(appmod, "google_flights_fare", lambda *a: None)


def test_compare_picks_cheapest_with_alternatives(monkeypatch):
    """compare=True queries all configured providers and returns the cheapest,
    attaching the OTHER real results as ascending alternatives."""
    _configure_all(monkeypatch)
    monkeypatch.setattr(appmod, "skyscanner_fare",
                        lambda *a: {"cheapest_cad": 900, "source": "skyscanner"})
    monkeypatch.setattr(appmod, "serpapi_fare", lambda *a: None)
    monkeypatch.setattr(appmod, "amadeus_fare", lambda *a: None)
    monkeypatch.setattr(appmod, "travelpayouts_fare",
                        lambda *a: {"cheapest_cad": 800, "source": "travelpayouts"})
    monkeypatch.setattr(appmod, "kiwi_fare",
                        lambda *a: {"cheapest_cad": 850, "source": "kiwi"})

    res = appmod._get_fare_uncached("YYZ", "PVG", "d", "r", 2, 0, compare=True)

    assert res["source"] == "travelpayouts"
    assert res["cheapest_cad"] == 800
    assert res["alternatives"] == [
        {"source": "kiwi", "cheapest_cad": 850, "chosen_cad": 850},
        {"source": "skyscanner", "cheapest_cad": 900, "chosen_cad": 900},
    ]


def test_compare_skips_unconfigured_providers(monkeypatch):
    """Only configured providers are queried; unconfigured ones are never called."""
    # Configure only travelpayouts + kiwi.
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", None)
    monkeypatch.setattr(appmod, "SERPAPI_KEY", None)
    monkeypatch.setattr(appmod, "AMADEUS_ID", None)
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", None)
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "k")
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")

    called = []

    def _sky(*a):
        called.append("skyscanner")
        return {"cheapest_cad": 1, "source": "skyscanner"}

    monkeypatch.setattr(appmod, "skyscanner_fare", _sky)
    monkeypatch.setattr(appmod, "serpapi_fare",
                        lambda *a: called.append("serpapi") or None)
    monkeypatch.setattr(appmod, "amadeus_fare",
                        lambda *a: called.append("amadeus") or None)
    monkeypatch.setattr(appmod, "travelpayouts_fare",
                        lambda *a: {"cheapest_cad": 800, "source": "travelpayouts"})
    monkeypatch.setattr(appmod, "kiwi_fare",
                        lambda *a: {"cheapest_cad": 850, "source": "kiwi"})

    res = appmod._get_fare_uncached("YYZ", "PVG", "d", "r", 2, 0, compare=True)

    assert called == [], "unconfigured providers must not be called"
    assert res["source"] == "travelpayouts"
    assert res["alternatives"] == [
        {"source": "kiwi", "cheapest_cad": 850, "chosen_cad": 850}]


def test_compare_all_no_data_returns_sentinel(monkeypatch):
    """All providers returning no real price -> no-data sentinel with alternatives []."""
    _configure_all(monkeypatch)
    for name in ("skyscanner_fare", "serpapi_fare", "amadeus_fare",
                 "travelpayouts_fare", "kiwi_fare"):
        monkeypatch.setattr(appmod, name, lambda *a: None)

    res = appmod._get_fare_uncached("YYZ", "PVG", "d", "r", 2, 0, compare=True)

    assert res["source"] == "no-data"
    assert res["cheapest_cad"] is None
    assert res["alternatives"] == []


def test_compare_no_configured_providers_returns_sentinel(monkeypatch):
    """No providers configured at all -> sentinel (empty chain, no calls)."""
    for name in ("RAPIDAPI_KEY", "SERPAPI_KEY", "AMADEUS_ID", "AMADEUS_SECRET",
                 "TRAVELPAYOUTS_TOKEN", "KIWI_API_KEY"):
        monkeypatch.setattr(appmod, name, None)

    res = appmod._get_fare_uncached("YYZ", "PVG", "d", "r", 2, 0, compare=True)

    assert res["source"] == "no-data"
    assert res["alternatives"] == []


def test_compare_swallows_provider_exception(monkeypatch):
    """A raising provider is ignored; the rest still produce a winner."""
    _configure_all(monkeypatch)

    def boom(*a):
        raise RuntimeError("provider down")

    monkeypatch.setattr(appmod, "skyscanner_fare", boom)
    monkeypatch.setattr(appmod, "serpapi_fare", lambda *a: None)
    monkeypatch.setattr(appmod, "amadeus_fare", lambda *a: None)
    monkeypatch.setattr(appmod, "travelpayouts_fare",
                        lambda *a: {"cheapest_cad": 800, "source": "travelpayouts"})
    monkeypatch.setattr(appmod, "kiwi_fare", lambda *a: None)

    res = appmod._get_fare_uncached("YYZ", "PVG", "d", "r", 2, 0, compare=True)

    assert res["source"] == "travelpayouts"
    assert res["alternatives"] == []


def test_compare_tie_resolves_to_earlier_provider(monkeypatch):
    """Equal cheapest_cad ties resolve to the earlier provider in the chain order."""
    _configure_all(monkeypatch)
    monkeypatch.setattr(appmod, "skyscanner_fare",
                        lambda *a: {"cheapest_cad": 800, "source": "skyscanner"})
    monkeypatch.setattr(appmod, "serpapi_fare", lambda *a: None)
    monkeypatch.setattr(appmod, "amadeus_fare", lambda *a: None)
    monkeypatch.setattr(appmod, "travelpayouts_fare",
                        lambda *a: {"cheapest_cad": 800, "source": "travelpayouts"})
    monkeypatch.setattr(appmod, "kiwi_fare", lambda *a: None)

    res = appmod._get_fare_uncached("YYZ", "PVG", "d", "r", 2, 0, compare=True)

    assert res["source"] == "skyscanner", "tie resolves to earlier chain provider"
    assert res["alternatives"] == [
        {"source": "travelpayouts", "cheapest_cad": 800, "chosen_cad": 800}]


def test_compare_false_is_ordered_fallback(monkeypatch):
    """compare=False keeps the first-match ordered fallback (cheapest is NOT chosen)."""
    _configure_all(monkeypatch)
    # skyscanner is first in the chain and returns a real (pricier) fare; ordered
    # fallback must return IT, not the cheaper travelpayouts result.
    monkeypatch.setattr(appmod, "skyscanner_fare",
                        lambda *a: {"cheapest_cad": 900, "source": "skyscanner"})
    monkeypatch.setattr(appmod, "serpapi_fare", lambda *a: None)
    monkeypatch.setattr(appmod, "amadeus_fare", lambda *a: None)
    monkeypatch.setattr(appmod, "travelpayouts_fare",
                        lambda *a: {"cheapest_cad": 800, "source": "travelpayouts"})
    monkeypatch.setattr(appmod, "kiwi_fare", lambda *a: None)

    res = appmod._get_fare_uncached("YYZ", "PVG", "d", "r", 2, 0, compare=False)

    assert res["source"] == "skyscanner"
    assert res.get("alternatives") is None


def test_cache_key_includes_compare(monkeypatch):
    """A compared result must NOT serve a fallback request and vice versa: each
    caches independently under a key that includes the compare flag."""
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", 3600)
    monkeypatch.setattr(appmod, "FARE_CACHE_PATH", "")  # memory-only
    appmod._fare_cache.clear()

    calls = {"fallback": 0, "compare": 0}

    def fake_uncached(o, d, dep, ret, ad, ch, compare=False, nonstop_threshold=0.0):
        if compare:
            calls["compare"] += 1
            return {"cheapest_cad": 800, "source": "travelpayouts",
                    "alternatives": [{"source": "kiwi", "cheapest_cad": 850}]}
        calls["fallback"] += 1
        return {"cheapest_cad": 900, "source": "skyscanner"}

    monkeypatch.setattr(appmod, "_get_fare_uncached", fake_uncached)

    fb1 = appmod.get_fare("YYZ", "PVG", "d", "r", 2, 0, compare=False)
    cmp1 = appmod.get_fare("YYZ", "PVG", "d", "r", 2, 0, compare=True)
    # Second round: both must be cache hits (no extra uncached calls).
    fb2 = appmod.get_fare("YYZ", "PVG", "d", "r", 2, 0, compare=False)
    cmp2 = appmod.get_fare("YYZ", "PVG", "d", "r", 2, 0, compare=True)

    assert calls == {"fallback": 1, "compare": 1}, "each compare flavour cached separately"
    assert fb1["source"] == "skyscanner" and fb2["source"] == "skyscanner"
    assert cmp1["source"] == "travelpayouts" and cmp2["source"] == "travelpayouts"
    assert (appmod.get_fare("YYZ", "PVG", "d", "r", 2, 0, compare=True)["alternatives"]
            == [{"source": "kiwi", "cheapest_cad": 850}])
    appmod._fare_cache.clear()


def test_compare_cache_key_includes_provider_set(monkeypatch):
    """A compared result is NOT reused after the configured-provider set changes:
    adding a provider must re-run comparison (so the new provider is included),
    not serve the stale result computed against the old set."""
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", 3600)
    monkeypatch.setattr(appmod, "FARE_CACHE_PATH", "")  # memory-only
    appmod._fare_cache.clear()

    calls = {"n": 0}

    def fake_uncached(o, d, dep, ret, ad, ch, compare=False, nonstop_threshold=0.0):
        calls["n"] += 1
        return {"cheapest_cad": 800, "source": "travelpayouts", "alternatives": []}

    monkeypatch.setattr(appmod, "_get_fare_uncached", fake_uncached)

    # Provider set A = {travelpayouts}.
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", None)
    monkeypatch.setattr(appmod, "SERPAPI_KEY", None)
    monkeypatch.setattr(appmod, "AMADEUS_ID", None)
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", None)
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "k")
    monkeypatch.setattr(appmod, "KIWI_API_KEY", None)

    appmod.get_fare("YYZ", "PVG", "d", "r", 2, 0, compare=True)
    appmod.get_fare("YYZ", "PVG", "d", "r", 2, 0, compare=True)  # cache hit
    assert calls["n"] == 1, "same provider set -> cached"

    # Provider set B = {travelpayouts, kiwi}: a NEW provider was configured.
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")
    appmod.get_fare("YYZ", "PVG", "d", "r", 2, 0, compare=True)
    assert calls["n"] == 2, "provider set changed -> compare must re-run, not serve stale"
    appmod._fare_cache.clear()


def test_get_fare_compare_uncached_when_ttl_disabled(monkeypatch):
    """TTL<=0 bypasses the cache but still honours compare (no-cache compare path)."""
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", 0)
    _configure_all(monkeypatch)
    monkeypatch.setattr(appmod, "skyscanner_fare",
                        lambda *a: {"cheapest_cad": 900, "source": "skyscanner"})
    monkeypatch.setattr(appmod, "serpapi_fare", lambda *a: None)
    monkeypatch.setattr(appmod, "amadeus_fare", lambda *a: None)
    monkeypatch.setattr(appmod, "travelpayouts_fare",
                        lambda *a: {"cheapest_cad": 800, "source": "travelpayouts"})
    monkeypatch.setattr(appmod, "kiwi_fare", lambda *a: None)

    res = appmod.get_fare("YYZ", "PVG", "d", "r", 2, 0, compare=True)
    assert res["source"] == "travelpayouts"
    assert res["alternatives"] == [
        {"source": "skyscanner", "cheapest_cad": 900, "chosen_cad": 900}]


def test_compare_picks_post_threshold_winner_not_raw_cheapest(monkeypatch):
    """Codex P2 (#43): the compare winner is the provider with the lowest
    POST-THRESHOLD chosen price, NOT raw cheapest_cad.

    Provider A: cheapest=1000, nonstop=1200; under a 25% threshold A's nonstop is
    promoted (1200 <= 1000*1.25) so A's chosen price is 1200. Provider B:
    cheapest=1100, nonstop=1100 -> chosen 1100. Picking by raw cheapest would pick
    A (1000<1100) then _build_cell would display 1200, beaten by B's already-fetched
    1100. Picking by the chosen price correctly selects B (1100 < 1200)."""
    _configure_all(monkeypatch)
    a = {"cheapest_cad": 1000, "nonstop_cad": 1200, "source": "skyscanner"}
    b = {"cheapest_cad": 1100, "nonstop_cad": 1100, "source": "travelpayouts"}
    monkeypatch.setattr(appmod, "skyscanner_fare", lambda *x: a)
    monkeypatch.setattr(appmod, "serpapi_fare", lambda *x: None)
    monkeypatch.setattr(appmod, "amadeus_fare", lambda *x: None)
    monkeypatch.setattr(appmod, "travelpayouts_fare", lambda *x: b)
    monkeypatch.setattr(appmod, "kiwi_fare", lambda *x: None)

    # threshold 25% -> A's nonstop promoted to 1200, so B (chosen 1100) wins.
    res = appmod._get_fare_uncached("YYZ", "PVG", "d", "r", 2, 0, compare=True,
                                    nonstop_threshold=0.25)
    assert res["source"] == "travelpayouts"
    assert res["cheapest_cad"] == 1100
    # A is the (only) alternative, ordered/labelled by its chosen price (1200).
    assert res["alternatives"] == [
        {"source": "skyscanner", "cheapest_cad": 1000, "chosen_cad": 1200}]
    # _build_cell on the winner agrees: B's nonstop == cheapest (1100), so chosen
    # price is 1100 either way — the winning displayed value, beating A's 1200.
    cell = appmod._build_cell("YYZ", "PVG", "d", "r", 2, [], res, 0.25)
    assert cell["chosen_cad"] == 1100
    assert cell["source"] == "travelpayouts"

    # threshold 0% -> no nonstop promotion, chosen == cheapest, so A (1000) wins.
    res0 = appmod._get_fare_uncached("YYZ", "PVG", "d", "r", 2, 0, compare=True,
                                     nonstop_threshold=0.0)
    assert res0["source"] == "skyscanner"
    assert res0["cheapest_cad"] == 1000
    assert res0["alternatives"] == [
        {"source": "travelpayouts", "cheapest_cad": 1100, "chosen_cad": 1100}]
    cell0 = appmod._build_cell("YYZ", "PVG", "d", "r", 2, [], res0, 0.0)
    assert cell0["chosen"] == "cheapest" and cell0["chosen_cad"] == 1000


def test_chosen_price_under_threshold_helper(monkeypatch):
    """The shared helper promotes nonstop iff within threshold, else cheapest."""
    within = {"cheapest_cad": 1000, "nonstop_cad": 1200}
    assert appmod._chosen_price_under_threshold(within, 0.25) == ("nonstop", 1200)
    assert appmod._chosen_price_under_threshold(within, 0.0) == ("cheapest", 1000)
    no_ns = {"cheapest_cad": 1000, "nonstop_cad": None}
    assert appmod._chosen_price_under_threshold(no_ns, 0.25) == ("cheapest", 1000)
    nodata = {"cheapest_cad": None, "nonstop_cad": None}
    assert appmod._chosen_price_under_threshold(nodata, 0.25) == ("cheapest", None)


def test_compare_cache_key_includes_threshold(monkeypatch):
    """Compare results with DIFFERENT thresholds cache separately (the winner now
    depends on the threshold). The FALLBACK key is independent of threshold."""
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", 3600)
    monkeypatch.setattr(appmod, "FARE_CACHE_PATH", "")  # memory-only
    appmod._fare_cache.clear()
    _configure_all(monkeypatch)

    calls = {"compare": 0, "fallback": 0}

    def fake_uncached(o, d, dep, ret, ad, ch, compare=False, nonstop_threshold=0.0):
        if compare:
            calls["compare"] += 1
            return {"cheapest_cad": 800, "source": "travelpayouts", "alternatives": []}
        calls["fallback"] += 1
        return {"cheapest_cad": 900, "source": "skyscanner"}

    monkeypatch.setattr(appmod, "_get_fare_uncached", fake_uncached)

    appmod.get_fare("YYZ", "PVG", "d", "r", 2, 0, compare=True, nonstop_threshold=0.25)
    appmod.get_fare("YYZ", "PVG", "d", "r", 2, 0, compare=True, nonstop_threshold=0.25)  # hit
    assert calls["compare"] == 1, "same threshold -> cached"
    appmod.get_fare("YYZ", "PVG", "d", "r", 2, 0, compare=True, nonstop_threshold=0.0)
    assert calls["compare"] == 2, "different threshold -> separate cache entry"

    # Fallback key must NOT vary with threshold (one fetch, then hits).
    appmod.get_fare("YYZ", "PVG", "d", "r", 2, 0, compare=False, nonstop_threshold=0.25)
    appmod.get_fare("YYZ", "PVG", "d", "r", 2, 0, compare=False, nonstop_threshold=0.0)
    assert calls["fallback"] == 1, "fallback key independent of threshold"
    appmod._fare_cache.clear()


def test_compare_cache_tag_fallback_ignores_threshold(monkeypatch):
    """_compare_cache_tag: fallback tag is False regardless of threshold; compare
    tag varies with threshold."""
    assert appmod._compare_cache_tag(False, 0.25) is False
    assert appmod._compare_cache_tag(False, 0.0) is False
    _configure_all(monkeypatch)
    assert (appmod._compare_cache_tag(True, 0.25)
            != appmod._compare_cache_tag(True, 0.0))


def test_build_cell_carries_alternatives(monkeypatch):
    """_build_cell must propagate the fare's alternatives into the cell dict."""
    fare = {"cheapest_cad": 800, "stops": 1, "nonstop_cad": None,
            "source": "travelpayouts",
            "alternatives": [{"source": "kiwi", "cheapest_cad": 850}]}
    cell = appmod._build_cell("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, [], fare, 0.25)
    assert cell["alternatives"] == [{"source": "kiwi", "cheapest_cad": 850}]


def test_build_cell_alternatives_none_in_fallback(monkeypatch):
    """A fallback fare (no alternatives key) yields a cell with alternatives None."""
    fare = {"cheapest_cad": 800, "stops": 1, "nonstop_cad": None, "source": "skyscanner"}
    cell = appmod._build_cell("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, [], fare, 0.25)
    assert cell["alternatives"] is None


# ---------------------------------------------------------------------------
# kiwi_fare tests
# ---------------------------------------------------------------------------

def _kiwi_itinerary(price, route_segments, duration=None, airlines=None):
    """Build a minimal Tequila-shaped itinerary dict.

    route_segments: list of (return_flag,) tuples, e.g.
      [(0,), (0,), (1,), (1,)] = 2 outbound + 2 return segments (each 1 stop per direction)
    duration: optional Tequila `duration` value (dict/seconds) for duration_min.
    airlines: optional top-level Tequila `airlines` carrier-code list.
    """
    itin = {
        "price": price,
        "deep_link": f"https://www.kiwi.com/deep?p={price}",
        "route": [{"return": flag} for flag in route_segments],
    }
    if duration is not None:
        itin["duration"] = duration
    if airlines is not None:
        itin["airlines"] = airlines
    return itin


def test_kiwi_fare_none_without_key(monkeypatch):
    """kiwi_fare returns None immediately when KIWI_API_KEY is not set."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", None)
    assert appmod.kiwi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_kiwi_fare_happy_path(monkeypatch, fake_resp):
    """Happy path: two itineraries (1-stop cheapest + nonstop pricier) normalize correctly."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")
    # 1-stop outbound (2 segs), 1-stop return (2 segs) = max stops = 1; 52500s -> 875 min
    itin_cheap = _kiwi_itinerary(7000, [0, 0, 1, 1], duration={"total": 52500},
                                 airlines=["AC", "NH"])
    # nonstop: 1 outbound + 1 return = max stops = 0; 36000s -> 600 min
    itin_nonstop = _kiwi_itinerary(9500, [0, 1], duration={"total": 36000},
                                   airlines=["UA"])
    payload = {"data": [itin_cheap, itin_nonstop]}
    captured_urls = []

    def fake_get(url, *a, **k):
        captured_urls.append(url)
        return fake_resp(payload, status=200)

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    res = appmod.kiwi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    # Verify the correct Tequila API host is used (regression guard for codex-review P1 bug)
    assert captured_urls == ["https://tequila-api.kiwi.com/v2/search"]
    assert res["cheapest_cad"] == 7000
    assert res["stops"] == 1
    assert res["nonstop_cad"] == 9500
    assert res["source"] == "kiwi"
    assert res["book"] == "https://www.kiwi.com/deep?p=7000"
    # duration_min = cheapest itinerary (875); nonstop_duration_min = chosen nonstop (600)
    assert res["duration_min"] == 875
    assert res["nonstop_duration_min"] == 600
    # airlines = cheapest itinerary's carriers; nonstop_airlines = the NONSTOP's (codex P2)
    assert res["airlines"] == ["AC", "NH"]
    assert res["nonstop_airlines"] == ["UA"]


def test_kiwi_fare_non_200(monkeypatch, fake_resp):
    """Non-200 response → None."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")
    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: fake_resp({}, status=503))
    assert appmod.kiwi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_kiwi_fare_empty_data(monkeypatch, fake_resp):
    """Empty data list → None."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": []}, status=200))
    assert appmod.kiwi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_kiwi_fare_malformed_missing_price(monkeypatch, fake_resp):
    """Itinerary missing 'price' field → None (defensive guard)."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")
    bad_itin = {"deep_link": "https://kiwi.com", "route": [{"return": 0}, {"return": 1}]}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": [bad_itin]}, status=200))
    assert appmod.kiwi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_kiwi_fare_no_nonstop(monkeypatch, fake_resp):
    """When there is no nonstop itinerary, nonstop_cad is None."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")
    # both itineraries have stops
    itin1 = _kiwi_itinerary(6000, [0, 0, 1, 1])   # 1 stop each dir
    itin2 = _kiwi_itinerary(8000, [0, 0, 0, 1, 1]) # 2 outbound stops, 1 return stop
    payload = {"data": [itin1, itin2]}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(payload, status=200))
    res = appmod.kiwi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["cheapest_cad"] == 6000
    assert res["nonstop_cad"] is None
    assert res["source"] == "kiwi"
    # no nonstop itinerary → nonstop_duration_min / nonstop_airlines are None
    assert res["nonstop_duration_min"] is None
    assert res["nonstop_airlines"] is None


def test_providers_configured_includes_kiwi_when_set(monkeypatch):
    """providers_configured() includes 'kiwi' only when KIWI_API_KEY is set."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "somekey")
    monkeypatch.setattr(appmod, "AMADEUS_ID", None)
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", None)
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", None)
    assert "kiwi" in appmod.providers_configured()


def test_providers_configured_excludes_kiwi_when_unset(monkeypatch):
    """providers_configured() does NOT include 'kiwi' when KIWI_API_KEY is None."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", None)
    monkeypatch.setattr(appmod, "AMADEUS_ID", None)
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", None)
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", None)
    assert "kiwi" not in appmod.providers_configured()


# ---------------------------------------------------------------------------
# serpapi_fare tests
# ---------------------------------------------------------------------------

def test_serpapi_fare_none_without_key(monkeypatch):
    """serpapi_fare returns None immediately when SERPAPI_KEY is not set."""
    monkeypatch.setattr(appmod, "SERPAPI_KEY", None)
    assert appmod.serpapi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_serpapi_fare_happy_path(monkeypatch, fake_resp):
    """Happy path: one 1-stop best_flight (cheaper) + one nonstop other_flight (pricier).

    Price is the party total as-is — NOT scaled. book is None.
    nonstop_cad is always None for SerpApi: the single-call round-trip response only
    describes the outbound leg, so we can't confirm a true round-trip nonstop.
    """
    monkeypatch.setattr(appmod, "SERPAPI_KEY", "k")
    payload = {
        "best_flights": [
            {"price": 2675, "layovers": [{"duration": 90}], "flights": [], "type": "Round trip"},
        ],
        "other_flights": [
            {"price": 3500, "flights": [], "type": "Round trip"},  # no layovers key → nonstop
        ],
    }
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return fake_resp(payload, status=200)

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    res = appmod.serpapi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)

    assert res is not None
    assert res["cheapest_cad"] == 2675          # party total, not scaled
    assert res["stops"] == 1                    # 1 outbound layover (cheapest entry)
    assert res["nonstop_cad"] is None           # never claimed: single-call response is outbound-only
    assert res["nonstop_airlines"] is None      # no confirmed nonstop → no nonstop carriers
    assert res["source"] == "serpapi"
    assert res["book"] is None                  # no booking URL in SerpApi response
    assert captured["url"] == "https://serpapi.com/search.json"
    assert captured["params"]["engine"] == "google_flights"
    assert captured["params"]["api_key"] == "k"
    assert captured["params"]["sort_by"] == 2   # Price sort: ensure cheapest itinerary is returned


def test_serpapi_fare_non_200(monkeypatch, fake_resp):
    """Non-200 response → None."""
    monkeypatch.setattr(appmod, "SERPAPI_KEY", "k")
    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: fake_resp({}, status=503))
    assert appmod.serpapi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_serpapi_fare_error_key(monkeypatch, fake_resp):
    """Response with top-level 'error' key → None."""
    monkeypatch.setattr(appmod, "SERPAPI_KEY", "k")
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"error": "Invalid API key."}, status=200))
    assert appmod.serpapi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_serpapi_fare_both_lists_empty(monkeypatch, fake_resp):
    """Both best_flights and other_flights empty → None."""
    monkeypatch.setattr(appmod, "SERPAPI_KEY", "k")
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(
                            {"best_flights": [], "other_flights": []}, status=200))
    assert appmod.serpapi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_serpapi_fare_all_entries_missing_price(monkeypatch, fake_resp):
    """Every flight entry lacks a usable price (missing key / None) → None (defensive)."""
    monkeypatch.setattr(appmod, "SERPAPI_KEY", "k")
    bad_payload = {
        "best_flights": [{"flights": [], "type": "Round trip"}],          # no price key
        "other_flights": [{"price": None, "flights": [], "type": "Round trip"}],  # price None
    }
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(bad_payload, status=200))
    assert appmod.serpapi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_serpapi_fare_mixed_priced_and_priceless(monkeypatch, fake_resp):
    """LIVE-bug regression: real responses mix entries WITH a numeric price and entries
    WITHOUT one (missing key or price: None). serpapi_fare must ignore the priceless
    entries and return the cheapest PRICED fare — NOT None, and NOT raise KeyError.
    """
    monkeypatch.setattr(appmod, "SERPAPI_KEY", "k")
    payload = {
        "best_flights": [
            {"price": 3100, "layovers": [{"duration": 60}], "flights": [], "type": "Round trip"},
            {"flights": [], "type": "Round trip"},  # no price key
        ],
        "other_flights": [
            {"price": None, "flights": [], "type": "Round trip"},          # price None
            {"price": 2675, "layovers": [{"duration": 90}], "flights": [], "type": "Round trip"},
            {"flights": [], "type": "Round trip"},                          # no price key
        ],
    }
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(payload, status=200))
    res = appmod.serpapi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)

    assert res is not None                       # priceless entries no longer kill the result
    assert res["cheapest_cad"] == 2675           # cheapest among the PRICED entries
    assert res["stops"] == 1                      # 1 layover on the cheapest priced entry
    assert res["source"] == "serpapi"


def test_serpapi_fare_priced_but_unprocessable(monkeypatch, fake_resp):
    """A priced entry that survives the filter but blows up downstream (e.g. a
    non-iterable 'layovers') is handled defensively → None instead of crashing."""
    monkeypatch.setattr(appmod, "SERPAPI_KEY", "k")
    bad_payload = {"best_flights": [{"price": 2675, "layovers": 5, "flights": []}]}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(bad_payload, status=200))
    assert appmod.serpapi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_serpapi_fare_json_decode_error(monkeypatch):
    """If r.json() raises (body is not JSON), serpapi_fare returns None."""
    monkeypatch.setattr(appmod, "SERPAPI_KEY", "k")

    class BrokenResp:
        status_code = 200
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: BrokenResp())
    assert appmod.serpapi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_providers_configured_includes_serpapi_when_set(monkeypatch):
    """providers_configured() includes 'serpapi' only when SERPAPI_KEY is set."""
    monkeypatch.setattr(appmod, "SERPAPI_KEY", "somekey")
    assert "serpapi" in appmod.providers_configured()


def test_providers_configured_excludes_serpapi_when_unset(monkeypatch):
    """providers_configured() does NOT include 'serpapi' when SERPAPI_KEY is None."""
    monkeypatch.setattr(appmod, "SERPAPI_KEY", None)
    assert "serpapi" not in appmod.providers_configured()


def test_serpapi_tried_before_amadeus_in_provider_chain(monkeypatch):
    """serpapi_fare is tried before amadeus/travelpayouts/kiwi (skyscanner None)."""
    call_order = []
    serpapi_result = {"cheapest_cad": 2675, "stops": 1, "nonstop_cad": 3500,
                      "source": "serpapi", "book": None}
    monkeypatch.setattr(appmod, "skyscanner_fare",
                        lambda *a: call_order.append("skyscanner") or None)
    monkeypatch.setattr(appmod, "serpapi_fare",
                        lambda *a: call_order.append("serpapi") or serpapi_result)
    monkeypatch.setattr(appmod, "amadeus_fare",
                        lambda *a: call_order.append("amadeus") or None)
    monkeypatch.setattr(appmod, "travelpayouts_fare",
                        lambda *a: call_order.append("travelpayouts") or None)
    monkeypatch.setattr(appmod, "kiwi_fare",
                        lambda *a: call_order.append("kiwi") or None)
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", 0)

    res = appmod.get_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)

    assert call_order == ["skyscanner", "serpapi"]
    assert res["source"] == "serpapi"
    # amadeus/travelpayouts/kiwi never called because serpapi succeeded
    assert "amadeus" not in call_order
    assert "travelpayouts" not in call_order
    assert "kiwi" not in call_order


def test_kiwi_fare_invalid_date_format_falls_through(monkeypatch, fake_resp):
    """When dep/ret are not valid ISO dates the raw strings are passed to the API."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")
    itin = _kiwi_itinerary(6000, [0, 1])
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": [itin]}, status=200))
    # "not-a-date" is not ISO-parseable; the function should still call the API and succeed
    res = appmod.kiwi_fare("YYZ", "PVG", "not-a-date", "not-a-date", 2, 0)
    assert res is not None
    assert res["source"] == "kiwi"


def test_kiwi_fare_json_decode_error(monkeypatch):
    """If r.json() raises (e.g. body is not JSON), kiwi_fare returns None."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")

    class BrokenResp:
        status_code = 200
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: BrokenResp())
    assert appmod.kiwi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


# ---------------------------------------------------------------------------
# duration_min: ISO parser + per-provider normalization (present / absent)
# ---------------------------------------------------------------------------

import pytest


@pytest.mark.parametrize("iso,expected", [
    ("PT14H35M", 875),     # combined
    ("PT14H", 840),        # hours only
    ("PT35M", 35),         # minutes only
    ("PT0H0M", 0),         # explicit zero
    (" PT2H30M ", 150),    # surrounding whitespace tolerated
])
def test_parse_iso_duration_valid(iso, expected):
    assert appmod.parse_iso_duration(iso) == expected


@pytest.mark.parametrize("bad", [None, "", "14H35M", "PT", "garbage", 875, "P1D"])
def test_parse_iso_duration_invalid(bad):
    assert appmod.parse_iso_duration(bad) is None


def test_amadeus_duration_sums_iso_legs(monkeypatch, fake_resp):
    """amadeus duration_min = sum of each itinerary's ISO duration (PT14H35M -> 875)."""
    monkeypatch.setattr(appmod, "amadeus_token", lambda: "T")
    # cheapest offer: outbound PT14H35M (875) + return PT0H0M (0) -> 875
    offers = [_amadeus_offer(8000, 1, durations=("PT14H35M", "PT0H0M"))]
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": offers}, status=200))
    res = appmod.amadeus_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["duration_min"] == 875


def test_amadeus_duration_none_when_absent(monkeypatch, fake_resp):
    """A missing itinerary duration → duration_min None (no fabrication)."""
    monkeypatch.setattr(appmod, "amadeus_token", lambda: "T")
    offer = {
        "price": {"grandTotal": "8000"},
        "itineraries": [
            {"segments": [{}]},  # no 'duration' key
            {"segments": [{}], "duration": "PT3H"},
        ],
    }
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": [offer]}, status=200))
    res = appmod.amadeus_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["duration_min"] is None


def test_travelpayouts_duration_field(monkeypatch, fake_resp):
    """travelpayouts uses `duration` (minutes) of the cheapest item."""
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "tok")
    data = [{"price": 1000, "transfers": 1, "return_transfers": 0,
             "link": "/d", "duration": 1230}]
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": data}, status=200))
    res = appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["duration_min"] == 1230


def test_travelpayouts_duration_to_back_sum(monkeypatch, fake_resp):
    """When `duration` is absent, sum duration_to + duration_back."""
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "tok")
    data = [{"price": 1000, "transfers": 0, "return_transfers": 0,
             "duration_to": 600, "duration_back": 630}]
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": data}, status=200))
    res = appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["duration_min"] == 1230


def test_travelpayouts_duration_none_when_absent(monkeypatch, fake_resp):
    """No duration fields at all → duration_min None."""
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "tok")
    data = [{"price": 1000, "transfers": 0, "return_transfers": 0}]
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": data}, status=200))
    res = appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["duration_min"] is None


def _kiwi_itin_dur(price, route_segments, duration):
    it = _kiwi_itinerary(price, route_segments)
    if duration is not None:
        it["duration"] = duration
    return it


def test_kiwi_duration_total_seconds_to_minutes(monkeypatch, fake_resp):
    """kiwi `duration` dict total seconds -> minutes for the cheapest itinerary."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")
    itin = _kiwi_itin_dur(7000, [0, 1],
                          {"departure": 1000, "return": 2000, "total": 52500})
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": [itin]}, status=200))
    res = appmod.kiwi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["duration_min"] == 875  # 52500s / 60


def test_kiwi_duration_departure_plus_return(monkeypatch, fake_resp):
    """No `total`: sum departure+return seconds -> minutes."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")
    itin = _kiwi_itin_dur(7000, [0, 1], {"departure": 27000, "return": 25500})
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": [itin]}, status=200))
    res = appmod.kiwi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["duration_min"] == 875  # (27000+25500)/60


def test_kiwi_duration_bare_number(monkeypatch, fake_resp):
    """A bare numeric `duration` (seconds) -> minutes."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")
    itin = _kiwi_itin_dur(7000, [0, 1], 52500)
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": [itin]}, status=200))
    res = appmod.kiwi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["duration_min"] == 875


def test_kiwi_duration_none_when_absent(monkeypatch, fake_resp):
    """No `duration` key → duration_min None."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")
    itin = _kiwi_itin_dur(7000, [0, 1], None)
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": [itin]}, status=200))
    res = appmod.kiwi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["duration_min"] is None


def test_kiwi_duration_empty_dict_none(monkeypatch, fake_resp):
    """A `duration` dict with no usable keys → duration_min None."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")
    itin = _kiwi_itin_dur(7000, [0, 1], {"foo": 1})
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": [itin]}, status=200))
    res = appmod.kiwi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["duration_min"] is None


def test_amadeus_duration_partial_legs_none(monkeypatch, fake_resp):
    """If one leg's duration is malformed (unparseable), the total is None."""
    monkeypatch.setattr(appmod, "amadeus_token", lambda: "T")
    offer = {
        "price": {"grandTotal": "8000"},
        "itineraries": [
            {"segments": [{}], "duration": "PT3H"},
            {"segments": [{}], "duration": "bogus"},  # unparseable → None
        ],
    }
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": [offer]}, status=200))
    res = appmod.amadeus_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["duration_min"] is None


def test_travelpayouts_duration_unparseable_none(monkeypatch, fake_resp):
    """A non-numeric `duration` → duration_min None (ValueError swallowed)."""
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "tok")
    data = [{"price": 1000, "transfers": 0, "return_transfers": 0, "duration": "abc"}]
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": data}, status=200))
    res = appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["duration_min"] is None


def test_travelpayouts_duration_to_back_unparseable_none(monkeypatch, fake_resp):
    """Non-numeric duration_to/back → duration_min None."""
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "tok")
    data = [{"price": 1000, "transfers": 0, "return_transfers": 0,
             "duration_to": "x", "duration_back": "y"}]
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": data}, status=200))
    res = appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["duration_min"] is None


def test_kiwi_duration_unparseable_dict_none(monkeypatch, fake_resp):
    """A `duration` dict with non-numeric total → duration_min None (ValueError swallowed)."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")
    itin = _kiwi_itin_dur(7000, [0, 1], {"total": "not-a-number"})
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": [itin]}, status=200))
    res = appmod.kiwi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["duration_min"] is None


def test_serpapi_duration_always_none_outbound_only(monkeypatch, fake_resp):
    """serpapi duration_min/nonstop_duration_min are always None: the single-call
    round-trip response is outbound-only, so total_duration would understate the true
    round-trip flight time (matches the nonstop_cad=None contract). Even when entries
    carry total_duration, the normalized dict must NOT expose it."""
    monkeypatch.setattr(appmod, "SERPAPI_KEY", "k")
    payload = {
        "best_flights": [
            {"price": 3100, "layovers": [{"duration": 60}], "total_duration": 1200,
             "flights": [], "type": "Round trip"},
        ],
        "other_flights": [
            {"price": 2675, "layovers": [{"duration": 90}], "total_duration": 875,
             "flights": [], "type": "Round trip"},
        ],
    }
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(payload, status=200))
    res = appmod.serpapi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["cheapest_cad"] == 2675
    assert res["duration_min"] is None          # outbound-only: not exposed as round-trip
    assert res["nonstop_duration_min"] is None


def test_fallback_chain_reaches_kiwi(monkeypatch):
    """When amadeus and travelpayouts return None, get_fare falls through to kiwi_fare."""
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", 0)
    monkeypatch.setattr(appmod, "amadeus_fare", lambda *a: None)
    monkeypatch.setattr(appmod, "travelpayouts_fare", lambda *a: None)
    kiwi_result = {
        "cheapest_cad": 7200, "stops": 1, "nonstop_cad": None,
        "source": "kiwi", "book": "https://kiwi.com/deep",
    }
    monkeypatch.setattr(appmod, "kiwi_fare", lambda *a: kiwi_result)
    res = appmod.get_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["source"] == "kiwi"
    assert res["cheapest_cad"] == 7200


# ---------------------------------------------------------------------------
# skyscanner_fare (RapidAPI flights-sky) tests
# ---------------------------------------------------------------------------

def _sky_leg(stop_count, duration_min, carriers, place_codes):
    """Build a flights-sky leg dict.

    carriers: list of marketing airline name strings.
    place_codes: list of segment endpoint IATA codes, len = stops+2, so segments
    are consecutive pairs (A->B, B->C, ...); a connection = the middle code(s).
    """
    segments = []
    for i in range(len(place_codes) - 1):
        segments.append({
            "origin": {"flightPlaceId": place_codes[i], "displayCode": place_codes[i]},
            "destination": {"flightPlaceId": place_codes[i + 1],
                            "displayCode": place_codes[i + 1]},
            "durationInMinutes": 100,
        })
    return {
        "stopCount": stop_count,
        "durationInMinutes": duration_min,
        "carriers": {"marketing": [{"name": n} for n in carriers]},
        "segments": segments,
    }


def _sky_item(item_id, price, out_leg, ret_leg, deep_link=None):
    item = {"id": item_id, "price": {"raw": price}, "legs": [out_leg, ret_leg]}
    if deep_link is not None:
        item["deepLink"] = deep_link
    return item


def _sky_complete_payload():
    """A complete flights-sky payload: Best bucket (1-stop, cheaper) + Direct bucket (nonstop, pricier)."""
    # 1-stop outbound via NRT (3 codes), nonstop return; max stops = 1; dur = 500+330 = 830
    best_item = _sky_item(
        "BEST1", 4400.0,
        _sky_leg(1, 500, ["Air Canada"], ["YYZ", "NRT", "LAX"]),
        _sky_leg(0, 330, ["Air Canada"], ["LAX", "YYZ"]),
        deep_link="https://www.skyscanner.ca/book/best1",
    )
    # nonstop both directions; max stops = 0; dur = 320+330 = 650
    direct_item = _sky_item(
        "DIR1", 5200.0,
        _sky_leg(0, 320, ["WestJet"], ["YYZ", "LAX"]),
        _sky_leg(0, 330, ["WestJet"], ["LAX", "YYZ"]),
    )
    return {
        "status": True,
        "data": {
            "context": {"status": "complete", "sessionId": "sid"},
            "itineraries": {"buckets": [
                {"id": "Best", "items": [best_item]},
                {"id": "Direct", "items": [direct_item]},
            ]},
        },
    }


def test_skyscanner_fare_none_without_key(monkeypatch):
    """skyscanner_fare returns None immediately when RAPIDAPI_KEY is not set."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", None)
    assert appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0) is None


def test_skyscanner_fare_happy_path_search_then_poll(monkeypatch, fake_resp):
    """search returns incomplete+sessionId; poll returns complete with buckets.

    Asserts cheapest_cad, stops, duration_min (sum of legs), nonstop_cad +
    nonstop_duration_min (from Direct item), airlines, layovers, source.
    """
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    complete = _sky_complete_payload()
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append((url, params))
        if url.endswith("/web/flights/search-roundtrip"):
            # auth headers + IATA place ids present
            assert headers["x-rapidapi-host"] == appmod.RAPIDAPI_HOST
            assert headers["x-rapidapi-key"] == "k"
            assert params["placeIdFrom"] == "YYZ" and params["placeIdTo"] == "LAX"
            assert params["adults"] == 1
            assert params["children"] == 0
            return fake_resp(
                {"status": True,
                 "data": {"context": {"status": "incomplete", "sessionId": "sid-244"}}},
                status=200)
        # poll endpoint: raw sessionId passed through (requests encodes once)
        assert url.endswith("/web/flights/search-incomplete")
        assert params["sessionId"] == "sid-244"
        return fake_resp(complete, status=200)

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    res = appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0)

    assert res is not None
    assert res["source"] == "skyscanner"
    assert res["cheapest_cad"] == 4400        # Best item is cheaper
    assert res["stops"] == 1                  # max(legs stopCount) on cheapest
    assert res["duration_min"] == 830         # 500 + 330
    assert res["nonstop_cad"] == 5200         # Direct item price
    assert res["nonstop_duration_min"] == 650  # 320 + 330
    assert res["airlines"] == ["Air Canada"]   # cheapest (1-stop) item's carriers
    assert res["nonstop_airlines"] == ["WestJet"]  # Direct item's own carriers (codex P2)
    assert res["layovers"] == [{"iata": "NRT", "duration_min": None}]
    assert res["book"] == "https://www.skyscanner.ca/book/best1"
    # search then exactly one poll
    assert len(calls) == 2


def test_skyscanner_fare_complete_on_first_call_no_poll(monkeypatch, fake_resp):
    """If search-roundtrip is already complete, no poll request is made."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    complete = _sky_complete_payload()
    urls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        urls.append(url)
        return fake_resp(complete, status=200)

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    res = appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0)
    assert res["cheapest_cad"] == 4400
    assert urls == ["https://" + appmod.RAPIDAPI_HOST + "/web/flights/search-roundtrip"]


def test_skyscanner_fare_non_200_returns_none(monkeypatch, fake_resp):
    """A non-200 search response → None."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({}, status=403))
    assert appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0) is None


def test_skyscanner_fare_retries_502_then_200(monkeypatch, fake_resp):
    """A transient 502 on search is retried, then a 200 completes successfully."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    complete = _sky_complete_payload()
    seq = [fake_resp({}, status=502), complete and fake_resp(complete, status=200)]
    state = {"i": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        r = seq[state["i"]]
        state["i"] += 1
        return r

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    res = appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0)
    assert res is not None
    assert res["cheapest_cad"] == 4400
    assert state["i"] == 2  # one 502 retry, then success


def test_skyscanner_fare_persistent_502_returns_none(monkeypatch, fake_resp):
    """If every attempt is 502, the final 502 is returned and handled → None."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({}, status=502))
    assert appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0) is None


def test_skyscanner_fare_request_exception_returns_none(monkeypatch):
    """A requests-level exception on search → None (never raises)."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")

    def boom(*a, **k):
        raise appmod.requests.RequestException("boom")

    monkeypatch.setattr(appmod.requests, "get", boom)
    assert appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0) is None


def test_skyscanner_fare_json_decode_error(monkeypatch):
    """If r.json() raises on the search response, skyscanner_fare returns None."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")

    class BrokenResp:
        status_code = 200
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: BrokenResp())
    assert appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0) is None


def test_skyscanner_fare_empty_buckets_returns_none(monkeypatch, fake_resp):
    """Complete response but no buckets/items → None."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    payload = {"data": {"context": {"status": "complete", "sessionId": "s"},
                        "itineraries": {"buckets": []}}}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(payload, status=200))
    assert appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0) is None


def test_skyscanner_fare_malformed_item_no_price_skipped(monkeypatch, fake_resp):
    """An item with no numeric price.raw is skipped; the priced one is used."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    good = _sky_item("G", 3000.0,
                     _sky_leg(0, 300, ["AC"], ["YYZ", "LAX"]),
                     _sky_leg(0, 310, ["AC"], ["LAX", "YYZ"]))
    bad = {"id": "B", "price": {}, "legs": []}  # no usable price.raw
    nonitem = "garbage"  # non-dict item is ignored
    payload = {"data": {"context": {"status": "complete"},
                        "itineraries": {"buckets": [
                            {"items": [bad, nonitem, good]},
                            "not-a-bucket",
                        ]}}}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(payload, status=200))
    res = appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0)
    assert res["cheapest_cad"] == 3000
    assert res["nonstop_cad"] == 3000  # the good item is nonstop both legs


def test_skyscanner_fare_dedupes_items_across_buckets(monkeypatch, fake_resp):
    """The same item id in Best + Cheapest buckets is counted once."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    item = _sky_item("DUP", 4000.0,
                     _sky_leg(1, 400, ["AC"], ["YYZ", "NRT", "LAX"]),
                     _sky_leg(1, 410, ["AC"], ["LAX", "NRT", "YYZ"]))
    payload = {"data": {"context": {"status": "complete"},
                        "itineraries": {"buckets": [
                            {"id": "Best", "items": [item]},
                            {"id": "Cheapest", "items": [item]},
                        ]}}}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(payload, status=200))
    res = appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0)
    assert res["cheapest_cad"] == 4000
    assert res["stops"] == 1
    assert res["nonstop_cad"] is None  # no nonstop item
    assert res["nonstop_duration_min"] is None
    assert res["nonstop_airlines"] is None
    # two layovers (one connection per leg)
    assert res["layovers"] == [{"iata": "NRT", "duration_min": None},
                               {"iata": "NRT", "duration_min": None}]


def test_skyscanner_fare_poll_never_completes_returns_none(monkeypatch, fake_resp):
    """Search is incomplete and every poll stays incomplete → bounded → None."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    poll_count = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/web/flights/search-roundtrip"):
            return fake_resp(
                {"data": {"context": {"status": "incomplete", "sessionId": "sid"}}},
                status=200)
        poll_count["n"] += 1
        # poll always returns incomplete
        return fake_resp({"data": {"context": {"status": "incomplete"}}}, status=200)

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    res = appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0)
    assert res is None
    assert poll_count["n"] >= 1  # polling was bounded, not infinite


def test_skyscanner_fare_incomplete_no_session_id_returns_none(monkeypatch, fake_resp):
    """Incomplete search with no sessionId → cannot poll → None."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(
                            {"data": {"context": {"status": "incomplete"}}}, status=200))
    assert appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0) is None


def test_skyscanner_fare_poll_recovers_from_transient_failures(monkeypatch, fake_resp):
    """Poll tolerates a non-200, a json error, and a non-dict before completing."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    complete = _sky_complete_payload()

    class BrokenJson:
        status_code = 200
        def json(self):
            raise ValueError("x")

    poll_responses = [
        fake_resp({}, status=500),                       # non-200
        BrokenJson(),                                     # json raises
        fake_resp({"data": "not-a-dict"}, status=200),   # data not a dict
        fake_resp(complete, status=200),                 # finally complete
    ]
    state = {"i": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/web/flights/search-roundtrip"):
            return fake_resp(
                {"data": {"context": {"status": "incomplete", "sessionId": "sid"}}},
                status=200)
        r = poll_responses[state["i"]]
        state["i"] += 1
        return r

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    res = appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0)
    assert res is not None
    assert res["cheapest_cad"] == 4400
    assert state["i"] == 4


def test_skyscanner_fare_data_not_dict_returns_none(monkeypatch, fake_resp):
    """If data is not a dict, return None defensively."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": [1, 2, 3]}, status=200))
    assert appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0) is None


def test_skyscanner_fare_no_book_link(monkeypatch, fake_resp):
    """When the cheapest item has no deep link, book is None."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    item = _sky_item("NOLINK", 3000.0,
                     _sky_leg(0, 300, ["AC"], ["YYZ", "LAX"]),
                     _sky_leg(0, 310, ["AC"], ["LAX", "YYZ"]))
    payload = {"data": {"context": {"status": "complete"},
                        "itineraries": {"buckets": [{"items": [item]}]}}}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(payload, status=200))
    res = appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0)
    assert res["book"] is None


def test_skyscanner_fare_missing_duration_and_stops_none(monkeypatch, fake_resp):
    """Legs without durationInMinutes/stopCount → duration_min/stops None (no fabrication)."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    leg = {"carriers": {"marketing": [{"name": "AC"}]}, "segments": []}
    item = {"id": "X", "price": {"raw": 2500.0}, "legs": [leg, leg]}
    payload = {"data": {"context": {"status": "complete"},
                        "itineraries": {"buckets": [{"items": [item]}]}}}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(payload, status=200))
    res = appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0)
    assert res["cheapest_cad"] == 2500
    assert res["duration_min"] is None
    assert res["stops"] is None
    assert res["layovers"] == []  # no segments → no connections
    assert res["nonstop_cad"] is None  # stopCount not 0 (missing) → not nonstop


def test_skyscanner_fare_unexpected_itineraries_shape_none(monkeypatch, fake_resp):
    """itineraries not a dict (buckets unreachable) → None."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    payload = {"data": {"context": {"status": "complete"}, "itineraries": []}}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(payload, status=200))
    assert appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0) is None


def test_skyscanner_fare_search_passes_children_when_present(monkeypatch, fake_resp):
    """Family searches send children=<n> so the Skyscanner quote counts kids.

    Without this, price.raw is adults-only and underprices child itineraries
    (and skyscanner is tried first, so it wins + caches the wrong total).
    Captures the search-roundtrip params from the mocked requests.get.
    """
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    complete = _sky_complete_payload()
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/web/flights/search-roundtrip"):
            captured.update(params)
            return fake_resp(complete, status=200)
        return fake_resp(complete, status=200)

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    res = appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 2, 2)

    assert res is not None
    assert captured["adults"] == 2
    assert captured["children"] == 2


def test_skyscanner_fare_poll_uses_raw_session_id(monkeypatch, fake_resp):
    """A sessionId containing /, +, = is passed RAW to the poll request.

    requests' params= encodes once; pre-quoting would double-encode (e.g. / ->
    %252F) so the poll could not find the session. The mock must receive the
    original raw sessionId, not a percent-encoded form.
    """
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    raw_sid = "a/b+c=d=="
    complete = _sky_complete_payload()
    poll_params = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/web/flights/search-roundtrip"):
            return fake_resp(
                {"status": True,
                 "data": {"context": {"status": "incomplete", "sessionId": raw_sid}}},
                status=200)
        assert url.endswith("/web/flights/search-incomplete")
        poll_params.update(params)
        return fake_resp(complete, status=200)

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    res = appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0)

    assert res is not None
    # raw, not %252F / %252B / %253D etc.
    assert poll_params["sessionId"] == raw_sid


def test_skyscanner_fare_poll_bails_after_consecutive_stalls(monkeypatch, fake_resp):
    """Polls that keep timing out (silence) bail after SKYSCANNER_POLL_MAX_STALLS
    consecutive stalls — NOT on the very first one (a single slow poll must not
    abort a session that may simply be taking a while), and NOT looping forever.

    Also asserts the poll request uses the configured SKYSCANNER_POLL_TIMEOUT.
    """
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod, "SKYSCANNER_POLL_MAX_STALLS", 3)
    monkeypatch.setattr(appmod, "SKYSCANNER_POLL_TIMEOUT", 20)
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    poll_count = {"n": 0}
    poll_timeouts = []

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/web/flights/search-roundtrip"):
            return fake_resp(
                {"data": {"context": {"status": "incomplete", "sessionId": "sid"}}},
                status=200)
        poll_count["n"] += 1
        poll_timeouts.append(timeout)
        # Every poll hangs and times out (the session goes silent).
        raise appmod.requests.exceptions.Timeout("poll hung")

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    res = appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0)

    assert res is None
    # Bailed after exactly MAX_STALLS consecutive silences — tolerant of one slow
    # poll, but bounded (not all 24 attempts, not infinite).
    assert poll_count["n"] == 3
    assert poll_timeouts == [20, 20, 20]


def test_skyscanner_fare_poll_tolerates_a_transient_stall(monkeypatch, fake_resp):
    """A single slow poll (timeout) does NOT abort the search: the loop keeps
    polling and still succeeds when the session reaches 'complete' afterwards."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod, "SKYSCANNER_POLL_MAX_STALLS", 3)
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    complete = _sky_complete_payload()
    state = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/web/flights/search-roundtrip"):
            return fake_resp(
                {"data": {"context": {"status": "incomplete", "sessionId": "sid"}}},
                status=200)
        state["n"] += 1
        if state["n"] == 1:
            raise appmod.requests.exceptions.Timeout("one slow poll")
        return fake_resp(complete, status=200)

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    res = appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0)

    # The transient stall was tolerated; the next poll completed → real result.
    assert res is not None
    assert state["n"] == 2


def test_skyscanner_verbose_traces_each_call(monkeypatch, fake_resp, caplog):
    """SKYSCANNER_VERBOSE logs the search + each poll + the outcome at INFO, so the
    async flow is visible instead of a silent None."""
    import logging
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod, "SKYSCANNER_VERBOSE", True)
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    complete = _sky_complete_payload()
    state = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/web/flights/search-roundtrip"):
            return fake_resp(
                {"data": {"context": {"status": "incomplete", "sessionId": "sid"}}},
                status=200)
        state["n"] += 1
        if state["n"] == 1:
            return fake_resp(
                {"data": {"context": {"status": "incomplete", "sessionId": "sid"}}},
                status=200)
        return fake_resp(complete, status=200)

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    with caplog.at_level(logging.INFO, logger="app"):
        res = appmod.skyscanner_fare("YYZ", "PEK", "2026-12-12", "2027-01-04", 2, 0)

    assert res is not None
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "[skyscanner] search YYZ->PEK" in text          # initial call traced
    assert "status=incomplete" in text                      # a poll trace
    assert "poll 1/" in text and "poll 2/" in text          # per-poll numbering
    assert "DONE" in text                                   # outcome with price


def test_skyscanner_verbose_off_is_silent(monkeypatch, fake_resp, caplog):
    """With SKYSCANNER_VERBOSE off (default) no [skyscanner] trace lines are emitted."""
    import logging
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod, "SKYSCANNER_VERBOSE", False)
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(_sky_complete_payload(), status=200))
    with caplog.at_level(logging.INFO, logger="app"):
        appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0)
    assert "[skyscanner]" not in " ".join(r.getMessage() for r in caplog.records)


def test_skyscanner_unwrap_single_double_and_error():
    """_skyscanner_unwrap descends 1 or 2 envelope layers to the payload and surfaces
    an inner upstream error (status False)."""
    payload = {"context": {"status": "complete", "sessionId": "s"}, "itineraries": {"buckets": []}}
    # single-nested (older shape): {data: payload}
    p1, e1 = appmod._skyscanner_unwrap({"status": True, "data": payload})
    assert p1 is payload and e1 is None
    # double-nested (current shape): {data: {data: payload, status: True}}
    p2, e2 = appmod._skyscanner_unwrap({"data": {"data": payload, "status": True}, "status": True})
    assert p2 is payload and e2 is None
    # upstream error: inner envelope status False with a message, empty payload
    p3, e3 = appmod._skyscanner_unwrap(
        {"data": {"data": {}, "status": False, "message": "Connection aborted"}, "status": True})
    assert e3 == "Connection aborted" and p3 == {}
    # non-dict → ({}, None)
    assert appmod._skyscanner_unwrap(None) == ({}, None)


def test_skyscanner_fare_parses_double_nested_envelope(monkeypatch, fake_resp):
    """A complete result wrapped in the NEW extra data envelope still yields a fare."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    inner = _sky_complete_payload()["data"]          # {context, itineraries}
    double = {"status": True, "data": {"data": inner, "status": True, "message": "Successful"}}
    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: fake_resp(double, status=200))
    res = appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0)
    assert res is not None and res["cheapest_cad"] == 4400 and res["source"] == "skyscanner"


def test_skyscanner_fare_surfaces_upstream_error(monkeypatch, fake_resp, caplog):
    """When flights-sky's own upstream fails (inner status False), return None AND log
    the real error instead of a silent empty result."""
    import logging
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod, "SKYSCANNER_VERBOSE", True)
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    errenv = {"status": True, "data": {"data": {}, "status": False,
                                       "message": "Connection aborted. RemoteDisconnected"}}
    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: fake_resp(errenv, status=200))
    with caplog.at_level(logging.INFO, logger="app"):
        res = appmod.skyscanner_fare("YYZ", "PEK", "2026-12-12", "2027-01-04", 2, 0)
    assert res is None
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "UPSTREAM error" in text and "Connection aborted" in text


def test_skyscanner_fare_search_uses_connect_read_timeout_tuple(monkeypatch, fake_resp):
    """The initial search-roundtrip request uses a (connect, read) timeout tuple.

    The async search legitimately takes ~25-30s (sometimes more) to return its
    session, all while the response IS progressing. So the initial call passes
    requests a ``(SKYSCANNER_CONNECT_TIMEOUT, SKYSCANNER_SEARCH_TIMEOUT)`` tuple: a
    short connect timeout (dead host fails fast) and a generous READ/INACTIVITY
    timeout (fires only after that many seconds of NO data, NOT a total cap) so a
    slow-but-alive search completes instead of being killed mid-flight.
    """
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod, "SKYSCANNER_CONNECT_TIMEOUT", 10)
    monkeypatch.setattr(appmod, "SKYSCANNER_SEARCH_TIMEOUT", 90)
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    complete = _sky_complete_payload()
    search_timeout = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/web/flights/search-roundtrip"):
            search_timeout["t"] = timeout
        return fake_resp(complete, status=200)

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    res = appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0)

    assert res is not None
    assert search_timeout["t"] == (
        appmod.SKYSCANNER_CONNECT_TIMEOUT,
        appmod.SKYSCANNER_SEARCH_TIMEOUT,
    ) == (10, 90)


def test_skyscanner_search_timeout_honours_env_override(monkeypatch, fake_resp):
    """Monkeypatched connect/read timeouts flow through to the request tuple."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod, "SKYSCANNER_CONNECT_TIMEOUT", 7)
    monkeypatch.setattr(appmod, "SKYSCANNER_SEARCH_TIMEOUT", 55)
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    complete = _sky_complete_payload()
    search_timeout = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/web/flights/search-roundtrip"):
            search_timeout["t"] = timeout
        return fake_resp(complete, status=200)

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    res = appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0)

    assert res is not None
    assert search_timeout["t"] == (7, 55)


def test_skyscanner_fare_poll_attempts_is_configurable(monkeypatch, fake_resp):
    """The poll loop honours SKYSCANNER_POLL_ATTEMPTS: with attempts=3 and a session
    that never completes, at most 3 poll requests are made (bounded by config)."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod, "SKYSCANNER_POLL_ATTEMPTS", 3)
    monkeypatch.setattr(appmod, "SKYSCANNER_POLL_INTERVAL", 0.0)
    sleeps = []
    monkeypatch.setattr(appmod.time, "sleep", lambda s, *a, **k: sleeps.append(s))
    poll_count = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/web/flights/search-roundtrip"):
            return fake_resp(
                {"data": {"context": {"status": "incomplete", "sessionId": "sid"}}},
                status=200)
        poll_count["n"] += 1
        return fake_resp({"data": {"context": {"status": "incomplete"}}}, status=200)

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    res = appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0)

    assert res is None
    assert poll_count["n"] == 3  # exactly SKYSCANNER_POLL_ATTEMPTS, not the old 8
    # sleeps use the configured interval
    assert sleeps == [0.0, 0.0, 0.0]


def test_skyscanner_fare_poll_no_inner_502_retry(monkeypatch, fake_resp):
    """Each poll attempt makes exactly ONE HTTP call even on 502: the poll request
    passes retries_502=0, so the inner 502 retry is disabled and the outer poll loop
    alone bounds the work. With attempts=3 and every poll returning 502, exactly 3
    poll HTTP calls are made (NOT 3 x 3 = 9 as the default retries_502=2 would give)."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod, "SKYSCANNER_POLL_ATTEMPTS", 3)
    monkeypatch.setattr(appmod, "SKYSCANNER_POLL_INTERVAL", 0.0)
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    poll_count = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/web/flights/search-roundtrip"):
            return fake_resp(
                {"data": {"context": {"status": "incomplete", "sessionId": "sid"}}},
                status=200)
        poll_count["n"] += 1
        return fake_resp({}, status=502)

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    res = appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0)

    assert res is None
    # No inner 502 retry on polls: <= SKYSCANNER_POLL_ATTEMPTS calls, not attempts x 3.
    assert poll_count["n"] == 3


def test_skyscanner_fare_poll_interval_is_used(monkeypatch, fake_resp):
    """The poll loop sleeps SKYSCANNER_POLL_INTERVAL between attempts."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod, "SKYSCANNER_POLL_ATTEMPTS", 4)
    monkeypatch.setattr(appmod, "SKYSCANNER_POLL_INTERVAL", 2.5)
    sleeps = []
    monkeypatch.setattr(appmod.time, "sleep", lambda s, *a, **k: sleeps.append(s))
    complete = _sky_complete_payload()
    poll_count = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/web/flights/search-roundtrip"):
            return fake_resp(
                {"data": {"context": {"status": "incomplete", "sessionId": "sid"}}},
                status=200)
        poll_count["n"] += 1
        # complete on the 2nd poll → stops early
        if poll_count["n"] >= 2:
            return fake_resp(complete, status=200)
        return fake_resp({"data": {"context": {"status": "incomplete"}}}, status=200)

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    res = appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0)

    assert res is not None
    # Completed on poll 2 → only 2 polls, only 2 sleeps, each the configured interval.
    assert poll_count["n"] == 2
    assert sleeps == [2.5, 2.5]


def test_skyscanner_fare_poll_timeout_is_configurable(monkeypatch, fake_resp):
    """The poll request timeout honours SKYSCANNER_POLL_TIMEOUT (default 20)."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod, "SKYSCANNER_POLL_TIMEOUT", 11)
    monkeypatch.setattr(appmod, "SKYSCANNER_POLL_INTERVAL", 0.0)
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    complete = _sky_complete_payload()
    poll_timeouts = []

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/web/flights/search-roundtrip"):
            return fake_resp(
                {"data": {"context": {"status": "incomplete", "sessionId": "sid"}}},
                status=200)
        poll_timeouts.append(timeout)
        return fake_resp(complete, status=200)

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    res = appmod.skyscanner_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0)

    assert res is not None
    assert poll_timeouts == [11]


def test_skyscanner_fare_poll_config_defaults():
    """The shipped defaults are the longer-but-bounded budget (12 x 1.5s, 8s timeout).

    These are read from the environment at import time, so assert them in a
    subprocess that neutralizes dotenv.load_dotenv and clears any
    SKYSCANNER_POLL_* vars — the result depends only on the shipped defaults,
    never on the developer/CI shell or a repo-local .env.
    """
    import os
    import subprocess
    import sys

    repo = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    script = (
        "import dotenv; dotenv.load_dotenv = lambda *a, **k: None; import app; "
        "print(app.SKYSCANNER_POLL_ATTEMPTS, app.SKYSCANNER_POLL_INTERVAL, "
        "app.SKYSCANNER_POLL_TIMEOUT)"
    )
    env = {
        k: v for k, v in os.environ.items()
        if k not in {"SKYSCANNER_POLL_ATTEMPTS", "SKYSCANNER_POLL_INTERVAL",
                     "SKYSCANNER_POLL_TIMEOUT"}
    }
    env["PYTHONPATH"] = repo
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "24 1.5 20"


def test_skyscanner_tried_first_in_provider_chain(monkeypatch):
    """skyscanner_fare is called FIRST — before serpapi, amadeus, travelpayouts, kiwi."""
    call_order = []
    sky_result = {"cheapest_cad": 4400, "stops": 1, "nonstop_cad": 5200,
                  "source": "skyscanner", "book": None}
    monkeypatch.setattr(appmod, "skyscanner_fare",
                        lambda *a: call_order.append("skyscanner") or sky_result)
    monkeypatch.setattr(appmod, "serpapi_fare",
                        lambda *a: call_order.append("serpapi") or None)
    monkeypatch.setattr(appmod, "amadeus_fare",
                        lambda *a: call_order.append("amadeus") or None)
    monkeypatch.setattr(appmod, "travelpayouts_fare",
                        lambda *a: call_order.append("travelpayouts") or None)
    monkeypatch.setattr(appmod, "kiwi_fare",
                        lambda *a: call_order.append("kiwi") or None)
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", 0)

    res = appmod.get_fare("YYZ", "LAX", "2026-08-07", "2026-08-09", 1, 0)

    assert call_order[0] == "skyscanner"
    assert res["source"] == "skyscanner"
    # later providers never called because skyscanner succeeded
    assert call_order == ["skyscanner"]


def test_skyscanner_helpers_empty_leg_edge_cases():
    """Direct helper coverage: empty/malformed legs and segments degrade gracefully."""
    # _skyscanner_item_duration: no legs → None
    assert appmod._skyscanner_item_duration({"legs": []}) is None
    # _skyscanner_max_stops: no stopCount anywhere → None
    assert appmod._skyscanner_max_stops({"legs": [{"durationInMinutes": 10}]}) is None
    # _skyscanner_is_nonstop: no legs → False (cannot claim nonstop)
    assert appmod._skyscanner_is_nonstop({"legs": []}) is False
    # _skyscanner_legs: legs not a list → []
    assert appmod._skyscanner_legs({"legs": "nope"}) == []
    # _skyscanner_layovers: a leg whose segments is not a list is skipped
    item = {"legs": [{"segments": "nope"},
                     {"segments": [
                         {"destination": {"flightPlaceId": "NRT"}},
                         {"destination": {"flightPlaceId": "LAX"}},
                     ]}]}
    assert appmod._skyscanner_layovers(item) == [{"iata": "NRT", "duration_min": None}]
    # _skyscanner_price: price not a dict → None
    assert appmod._skyscanner_price({"price": "x"}) is None
    # _skyscanner_airlines: carriers missing / non-dict → no names
    assert appmod._skyscanner_airlines({"legs": [{"carriers": None}]}) == []


def test_skyscanner_get_returns_502_when_no_retries_left(monkeypatch, fake_resp):
    """With retries_502=0 a 502 is returned immediately (no retry, final attempt)."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    resp = fake_resp({}, status=502)
    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: resp)
    got = appmod._skyscanner_get("https://h/x", params={}, retries_502=0)
    assert got is resp


def test_providers_configured_includes_skyscanner_when_set(monkeypatch):
    """providers_configured() includes 'skyscanner' only when RAPIDAPI_KEY is set."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "somekey")
    assert "skyscanner" in appmod.providers_configured()


def test_providers_configured_excludes_skyscanner_when_unset(monkeypatch):
    """providers_configured() does NOT include 'skyscanner' when RAPIDAPI_KEY is None."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", None)
    assert "skyscanner" not in appmod.providers_configured()


# ---------------------------------------------------------------------------
# airlines + layovers normalization across providers (#56, #57)
# ---------------------------------------------------------------------------

def test_serpapi_airlines_and_layovers_mapped(monkeypatch, fake_resp):
    """serpapi: flights[].airline → unique names; layovers[] → {iata,name,duration_min}."""
    monkeypatch.setattr(appmod, "SERPAPI_KEY", "k")
    payload = {
        "best_flights": [
            {"price": 2675,
             "flights": [
                 {"airline": "Air Canada"},
                 {"airline": "Air Canada"},   # duplicate → deduped
                 {"airline": "United"},
             ],
             "layovers": [
                 {"id": "PEK", "name": "Beijing Capital", "duration": 80},
             ],
             "type": "Round trip"},
        ],
        "other_flights": [
            {"price": 3500, "flights": [{"airline": "ANA"}], "type": "Round trip"},
        ],
    }
    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: fake_resp(payload, status=200))
    res = appmod.serpapi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["airlines"] == ["Air Canada", "United"]
    assert res["layovers"] == [{"iata": "PEK", "name": "Beijing Capital", "duration_min": 80}]


def test_serpapi_nonstop_entry_empty_layovers_and_no_airlines(monkeypatch, fake_resp):
    """serpapi cheapest entry with no layovers key and no usable airline names."""
    monkeypatch.setattr(appmod, "SERPAPI_KEY", "k")
    payload = {
        "best_flights": [
            {"price": 2000, "flights": [{}], "type": "Round trip"},  # no airline name
        ],
    }
    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: fake_resp(payload, status=200))
    res = appmod.serpapi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["airlines"] == []
    assert res["layovers"] == []


def test_serpapi_airlines_flights_not_a_list(monkeypatch, fake_resp):
    """serpapi: a non-list flights value yields [] airlines (defensive, no crash)."""
    monkeypatch.setattr(appmod, "SERPAPI_KEY", "k")
    payload = {"best_flights": [{"price": 2000, "flights": None, "layovers": [5]}]}
    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: fake_resp(payload, status=200))
    res = appmod.serpapi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["airlines"] == []
    # a non-dict layover entry (5) is skipped defensively
    assert res["layovers"] == []


def _amadeus_offer_full(grand_total, out_segs, ret_segs):
    """Offer with explicit segments carrying carrierCode + arrival/departure times."""
    return {
        "price": {"grandTotal": str(grand_total)},
        "itineraries": [
            {"duration": "PT5H", "segments": out_segs},
            {"duration": "PT5H", "segments": ret_segs},
        ],
    }


def test_amadeus_airlines_codes_and_segment_layovers(monkeypatch, fake_resp):
    """amadeus: dedupe carrierCodes; derive layover iata + minute gap from segments."""
    monkeypatch.setattr(appmod, "amadeus_token", lambda: "T")
    out_segs = [
        {"carrierCode": "AC",
         "departure": {"iataCode": "YYZ", "at": "2026-12-12T10:00:00"},
         "arrival": {"iataCode": "NRT", "at": "2026-12-12T14:00:00"}},
        {"carrierCode": "NH",
         "departure": {"iataCode": "NRT", "at": "2026-12-12T15:20:00"},
         "arrival": {"iataCode": "PVG", "at": "2026-12-12T18:00:00"}},
    ]
    ret_segs = [
        {"carrierCode": "AC",
         "departure": {"iataCode": "PVG", "at": "2027-01-04T09:00:00"},
         "arrival": {"iataCode": "YYZ", "at": "2027-01-04T14:00:00"}},
    ]
    offers = [_amadeus_offer_full(8000, out_segs, ret_segs)]
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": offers}, status=200))
    res = appmod.amadeus_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["airlines"] == ["AC", "NH"]
    # connection at NRT: 14:00 → 15:20 = 80 min; return leg is nonstop (no layover)
    assert res["layovers"] == [{"iata": "NRT", "duration_min": 80}]
    # single 1-stop offer → no nonstop itinerary → nonstop_airlines None
    assert res["nonstop_airlines"] is None


def test_amadeus_nonstop_airlines_from_nonstop_offer(monkeypatch, fake_resp):
    """amadeus: nonstop_airlines = the NONSTOP offer's carriers, distinct from the
    cheapest connecting offer's airlines (codex P2 carrier/itinerary pairing)."""
    monkeypatch.setattr(appmod, "amadeus_token", lambda: "T")
    # cheapest: 1-stop via NRT on AC/NH
    cheap_out = [
        {"carrierCode": "AC", "arrival": {"iataCode": "NRT", "at": "2026-12-12T14:00:00"}},
        {"carrierCode": "NH", "departure": {"iataCode": "NRT", "at": "2026-12-12T15:20:00"}},
    ]
    # nonstop (pricier) on UA, single segment per direction
    ns_seg = [{"carrierCode": "UA",
               "departure": {"iataCode": "YYZ", "at": "2026-12-12T10:00:00"},
               "arrival": {"iataCode": "PVG", "at": "2026-12-12T22:00:00"}}]
    offers = [
        _amadeus_offer_full(8000, cheap_out, [{"carrierCode": "AC"}]),
        _amadeus_offer_full(9500, ns_seg, ns_seg),
    ]
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": offers}, status=200))
    res = appmod.amadeus_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["airlines"] == ["AC", "NH"]       # cheapest (connecting) carriers
    assert res["nonstop_airlines"] == ["UA"]     # the nonstop offer's own carrier


def test_amadeus_layover_unparseable_times_duration_none(monkeypatch, fake_resp):
    """amadeus: a connection with a missing departure 'at' → duration_min None."""
    monkeypatch.setattr(appmod, "amadeus_token", lambda: "T")
    out_segs = [
        {"carrierCode": "AC", "arrival": {"iataCode": "NRT", "at": "2026-12-12T14:00:00"}},
        {"carrierCode": "AC", "departure": {"iataCode": "NRT"}},  # no 'at'
    ]
    offers = [_amadeus_offer_full(8000, out_segs, [{"carrierCode": "AC"}])]
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": offers}, status=200))
    res = appmod.amadeus_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["layovers"] == [{"iata": "NRT", "duration_min": None}]


def test_amadeus_airlines_layovers_skip_non_dict_itineraries():
    """_amadeus_airlines/_amadeus_layovers skip a non-dict itinerary defensively."""
    offer = {"itineraries": [None, {"segments": [
        {"carrierCode": "AC", "arrival": {"iataCode": "NRT", "at": "2026-01-01T10:00:00"}},
        {"carrierCode": "AC", "departure": {"iataCode": "NRT", "at": "2026-01-01T11:00:00"}},
    ]}]}
    assert appmod._amadeus_airlines(offer) == ["AC"]
    assert appmod._amadeus_layovers(offer) == [{"iata": "NRT", "duration_min": 60}]


def test_iso_gap_minutes_edge_cases():
    """_iso_gap_minutes: bad types, malformed strings, and negative gaps → None."""
    assert appmod._iso_gap_minutes(None, "2026-01-01T00:00:00") is None
    assert appmod._iso_gap_minutes("nope", "also-nope") is None
    # negative gap (end before start) → None (never fabricated)
    assert appmod._iso_gap_minutes("2026-01-01T05:00:00", "2026-01-01T04:00:00") is None
    assert appmod._iso_gap_minutes("2026-01-01T04:00:00", "2026-01-01T05:30:00") == 90


def test_travelpayouts_airline_to_list_and_layovers_none(monkeypatch, fake_resp):
    """travelpayouts: airline code → [code]; layovers always None (no per-stop detail)."""
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "t")
    data = [{"price": 1200, "transfers": 1, "return_transfers": 0, "airline": "TK",
             "link": "/deep"}]
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": data}, status=200))
    res = appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["airlines"] == ["TK"]
    assert res["layovers"] is None


def test_travelpayouts_no_airline_empty_list(monkeypatch, fake_resp):
    """travelpayouts: a result without an 'airline' key → airlines []."""
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "t")
    data = [{"price": 1200, "transfers": 0, "return_transfers": 0}]
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": data}, status=200))
    res = appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["airlines"] == []
    assert res["layovers"] is None


def _kiwi_itin_rich(price, route):
    """Tequila itinerary with explicit route segments (airline/flyTo/aTime/dTime)."""
    return {"price": price, "deep_link": f"https://www.kiwi.com/deep?p={price}", "route": route}


def test_kiwi_airlines_top_level_and_route_layovers(monkeypatch, fake_resp):
    """kiwi: top-level airlines list used; route connection → iata + minute gap."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")
    route = [
        {"return": 0, "airline": "AC", "flyTo": "NRT", "aTime": 1000, "dTime": 0},
        {"return": 0, "airline": "NH", "flyTo": "PVG", "dTime": 5800},
        {"return": 1, "airline": "AC", "flyTo": "YYZ"},
    ]
    itin = _kiwi_itin_rich(7000, route)
    itin["airlines"] = ["AC", "NH", "AC"]   # deduped → ["AC","NH"]
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": [itin]}, status=200))
    res = appmod.kiwi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["airlines"] == ["AC", "NH"]
    # connection at NRT: aTime 1000 → next dTime 5800 = 4800s = 80 min;
    # the return-direction boundary (different return flag) is NOT a layover.
    assert res["layovers"] == [{"iata": "NRT", "duration_min": 80}]


def test_kiwi_airlines_fallback_to_route_codes(monkeypatch, fake_resp):
    """kiwi: when no top-level airlines list, fall back to route[].airline codes."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")
    route = [
        {"return": 0, "airline": "LH", "flyTo": "FRA"},
        {"return": 0, "airline": "LH", "flyTo": "PVG"},
        {"return": 1, "airline": "OS", "flyTo": "YYZ"},
    ]
    itin = _kiwi_itin_rich(6000, route)   # no "airlines" key
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": [itin]}, status=200))
    res = appmod.kiwi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["airlines"] == ["LH", "OS"]
    # FRA connection has no aTime/dTime → duration_min None
    assert res["layovers"] == [{"iata": "FRA", "duration_min": None}]


def test_kiwi_nonstop_no_layovers(monkeypatch, fake_resp):
    """kiwi: a nonstop itinerary (1 seg each direction) has [] layovers."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")
    route = [
        {"return": 0, "airline": "AC", "flyTo": "PVG"},
        {"return": 1, "airline": "AC", "flyTo": "YYZ"},
    ]
    itin = _kiwi_itin_rich(9000, route)
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": [itin]}, status=200))
    res = appmod.kiwi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["layovers"] == []


def test_kiwi_helpers_defensive_non_dict():
    """_kiwi_airlines/_kiwi_layovers/_kiwi_gap_minutes handle malformed input."""
    assert appmod._kiwi_airlines({}) == []
    assert appmod._kiwi_airlines({"route": [None]}) == []
    assert appmod._kiwi_layovers({}) == []
    assert appmod._kiwi_layovers({"route": "nope"}) == []
    assert appmod._kiwi_gap_minutes(None, 5) is None
    assert appmod._kiwi_gap_minutes(10, 5) is None  # negative
    assert appmod._kiwi_gap_minutes(0, 120) == 2


# ----------------------------- provider retry/backoff (#41) -----------------------------
def _seq_get(responses, calls, exc_for_none=None):
    """Build a fake requests.get that yields `responses` in order.

    Each entry is either a FakeResp-like object (returned) or an Exception
    instance (raised). `calls` is a dict whose "n" is incremented per call.
    """
    def fake_get(*a, **k):
        i = calls["n"]
        calls["n"] += 1
        item = responses[i]
        if isinstance(item, Exception):
            raise item
        return item
    return fake_get


def test_retry_get_503_then_200(monkeypatch, fake_resp):
    """A 503 followed by a 200 is retried and returns the 200 result (travelpayouts)."""
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "tok")
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    calls = {"n": 0}
    offers = {"data": [{"price": 100, "transfers": 0, "return_transfers": 0,
                        "airline": "AC", "link": "/x"}]}
    monkeypatch.setattr(appmod.requests, "get", _seq_get(
        [fake_resp({}, status=503), fake_resp(offers, status=200)], calls))
    res = appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 1, 0)
    assert res is not None and res["source"] == "travelpayouts"
    assert calls["n"] == 2  # one 503 retry, then success


def test_retry_get_timeout_is_terminal(monkeypatch, fake_resp):
    """A request Timeout is NOT retried — exactly 1 attempt, no sleep, returns None.

    Retrying a timeout would re-pay the full per-attempt cost (e.g. 20–30s) and
    stack stalls across providers. Instead, the Timeout is terminal: the helper
    returns None immediately and the provider falls through to the next one.
    """
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")
    sleeps = []
    monkeypatch.setattr(appmod.time, "sleep", lambda s, *a, **k: sleeps.append(s))
    monkeypatch.setattr(appmod, "PROVIDER_RETRIES", 2)
    calls = {"n": 0}

    def always_timeout(*a, **k):
        calls["n"] += 1
        raise appmod.requests.exceptions.Timeout("slow")

    monkeypatch.setattr(appmod.requests, "get", always_timeout)
    res = appmod.kiwi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 1, 0)
    assert res is None
    assert calls["n"] == 1   # terminal: exactly one attempt, no retry
    assert sleeps == []       # no backoff sleep for a Timeout


def test_retry_get_429_honours_retry_after_capped(monkeypatch, fake_resp):
    """A 429 with Retry-After sleeps the capped header value, then retries."""
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "tok")
    sleeps = []
    monkeypatch.setattr(appmod.time, "sleep", lambda s, *a, **k: sleeps.append(s))
    monkeypatch.setattr(appmod, "PROVIDER_BACKOFF_MAX", 4.0)
    calls = {"n": 0}
    offers = {"data": [{"price": 100, "transfers": 0, "return_transfers": 0}]}
    responses = [
        fake_resp({}, status=429, headers={"Retry-After": "1"}),
        fake_resp(offers, status=200),
    ]
    monkeypatch.setattr(appmod.requests, "get", _seq_get(responses, calls))
    res = appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 1, 0)
    assert res is not None
    assert sleeps == [1.0]  # Retry-After honoured (<= cap), not exp-backoff
    assert calls["n"] == 2


def test_retry_get_429_retry_after_clamped_to_cap(monkeypatch, fake_resp):
    """A huge Retry-After is clamped to PROVIDER_BACKOFF_MAX (bounded)."""
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "tok")
    sleeps = []
    monkeypatch.setattr(appmod.time, "sleep", lambda s, *a, **k: sleeps.append(s))
    monkeypatch.setattr(appmod, "PROVIDER_BACKOFF_MAX", 4.0)
    calls = {"n": 0}
    offers = {"data": [{"price": 100, "transfers": 0, "return_transfers": 0}]}
    responses = [
        fake_resp({}, status=429, headers={"Retry-After": "999"}),
        fake_resp(offers, status=200),
    ]
    monkeypatch.setattr(appmod.requests, "get", _seq_get(responses, calls))
    appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 1, 0)
    assert sleeps == [4.0]  # clamped to cap


def test_retry_get_429_bad_retry_after_falls_back_to_backoff(monkeypatch, fake_resp):
    """A non-numeric / negative Retry-After is ignored; exp-backoff is used instead."""
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "tok")
    sleeps = []
    monkeypatch.setattr(appmod.time, "sleep", lambda s, *a, **k: sleeps.append(s))
    monkeypatch.setattr(appmod, "PROVIDER_BACKOFF", 0.5)
    monkeypatch.setattr(appmod, "PROVIDER_BACKOFF_MAX", 4.0)
    calls = {"n": 0}
    offers = {"data": [{"price": 100, "transfers": 0, "return_transfers": 0}]}
    # First 429: non-numeric header -> backoff 0.5; second 429: negative -> backoff 1.0.
    responses = [
        fake_resp({}, status=429, headers={"Retry-After": "soon"}),
        fake_resp({}, status=429, headers={"Retry-After": "-5"}),
        fake_resp(offers, status=200),
    ]
    monkeypatch.setattr(appmod, "PROVIDER_RETRIES", 2)
    monkeypatch.setattr(appmod.requests, "get", _seq_get(responses, calls))
    appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 1, 0)
    assert sleeps == [0.5, 1.0]  # exponential backoff, capped


def test_retry_get_persistent_503_returns_none_bounded(monkeypatch, fake_resp):
    """Every attempt is 503 -> provider returns None after exactly retries+1 calls."""
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "tok")
    sleeps = []
    monkeypatch.setattr(appmod.time, "sleep", lambda s, *a, **k: sleeps.append(s))
    monkeypatch.setattr(appmod, "PROVIDER_RETRIES", 2)
    monkeypatch.setattr(appmod, "PROVIDER_BACKOFF", 0.5)
    monkeypatch.setattr(appmod, "PROVIDER_BACKOFF_MAX", 4.0)
    calls = {"n": 0}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1)
                                         or fake_resp({}, status=503)))
    res = appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 1, 0)
    assert res is None
    assert calls["n"] == 3  # retries(2) + 1, bounded — no infinite loop
    assert sleeps == [0.5, 1.0]  # backed-off, capped, then give up (no sleep after last)


def test_retry_get_persistent_connection_error_returns_none_bounded(monkeypatch):
    """Every attempt raises ConnectionError -> None after retries+1 attempts with backoff.

    ConnectionError is still retryable (it fails fast — no time was wasted) so
    the helper backs off and tries again, bounded to retries+1 total.
    """
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")
    sleeps = []
    monkeypatch.setattr(appmod.time, "sleep", lambda s, *a, **k: sleeps.append(s))
    monkeypatch.setattr(appmod, "PROVIDER_RETRIES", 2)
    monkeypatch.setattr(appmod, "PROVIDER_BACKOFF", 0.5)
    calls = {"n": 0}

    def always_connection_error(*a, **k):
        calls["n"] += 1
        raise appmod.requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(appmod.requests, "get", always_connection_error)
    res = appmod.kiwi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 1, 0)
    assert res is None
    assert calls["n"] == 3  # bounded: retries(2)+1 = 3 attempts
    assert sleeps == [0.5, 1.0]  # backed-off between attempts, no sleep after last


def test_retry_single_success_is_one_request(monkeypatch, fake_resp):
    """A first-try 200 makes exactly ONE request (retry wrapper adds no overhead)."""
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "tok")
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    calls = {"n": 0}
    offers = {"data": [{"price": 100, "transfers": 0, "return_transfers": 0}]}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1)
                                         or fake_resp(offers, status=200)))
    appmod.travelpayouts_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 1, 0)
    assert calls["n"] == 1


def test_retry_post_amadeus_token_retries(monkeypatch, fake_resp):
    """The amadeus token POST retries a transient 502 then succeeds."""
    monkeypatch.setattr(appmod, "AMADEUS_ID", "id")
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", "secret")
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    calls = {"n": 0}
    responses = [fake_resp({}, status=502),
                 fake_resp({"access_token": "TOK", "expires_in": 1799}, status=200)]
    monkeypatch.setattr(appmod.requests, "post", _seq_get(responses, calls))
    assert appmod.amadeus_token() == "TOK"
    assert calls["n"] == 2


def test_retry_post_amadeus_token_timeout_is_terminal(monkeypatch):
    """A Timeout on the token POST is NOT retried — exactly 1 attempt, no sleep.

    Timeout is terminal in _request_with_retry so amadeus_token returns None
    immediately without sleeping, regardless of PROVIDER_RETRIES.
    """
    monkeypatch.setattr(appmod, "AMADEUS_ID", "id")
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", "secret")
    monkeypatch.setattr(appmod, "PROVIDER_RETRIES", 2)
    sleeps = []
    monkeypatch.setattr(appmod.time, "sleep", lambda s, *a, **k: sleeps.append(s))
    calls = {"n": 0}

    def timeout_post(*a, **k):
        calls["n"] += 1
        raise appmod.requests.exceptions.Timeout("t")

    monkeypatch.setattr(appmod.requests, "post", timeout_post)
    assert appmod.amadeus_token() is None
    assert calls["n"] == 1   # terminal: one attempt only
    assert sleeps == []       # no backoff sleep


def test_request_with_retry_no_headers_attr(monkeypatch):
    """_retry_after_seconds tolerates a response object with no .headers attr."""
    class NoHeaders:
        status_code = 200
    assert appmod._retry_after_seconds(NoHeaders(), 4.0) is None


def test_request_with_retry_headers_without_get(monkeypatch):
    """_retry_after_seconds tolerates a .headers that lacks .get (AttributeError)."""
    class Weird:
        headers = object()  # no .get
        status_code = 429
    assert appmod._retry_after_seconds(Weird(), 4.0) is None


def test_request_with_retry_missing_retry_after(monkeypatch, fake_resp):
    """A 429 with NO Retry-After header falls back to capped exponential backoff."""
    sleeps = []
    monkeypatch.setattr(appmod.time, "sleep", lambda s, *a, **k: sleeps.append(s))
    calls = {"n": 0}
    responses = [fake_resp({}, status=429), fake_resp({"ok": 1}, status=200)]
    monkeypatch.setattr(appmod.requests, "get", _seq_get(responses, calls))
    r = appmod._request_with_retry("GET", "http://x", retries=2, backoff=0.5,
                                   backoff_max=4.0)
    assert r.status_code == 200
    assert sleeps == [0.5]  # no Retry-After -> exp backoff


def test_skyscanner_poll_not_double_retried(monkeypatch, fake_resp):
    """The skyscanner poll path keeps its own bounded 502 retry and is NOT wrapped
    by _request_with_retry. With attempts=2 and every poll a 502, exactly 2 poll
    HTTP calls happen (retries_502=0 on polls), not multiplied by PROVIDER_RETRIES."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "rk")
    monkeypatch.setattr(appmod, "SKYSCANNER_POLL_ATTEMPTS", 2)
    monkeypatch.setattr(appmod, "PROVIDER_RETRIES", 5)  # must NOT influence polls
    monkeypatch.setattr(appmod.time, "sleep", lambda *_a, **_k: None)
    state = {"i": 0}
    incomplete = {"data": {"context": {"status": "incomplete", "sessionId": "S"}}}

    def fake_get(url, *a, **k):
        if url.endswith("search-roundtrip"):
            return fake_resp(incomplete, status=200)
        state["i"] += 1
        return fake_resp({}, status=502)  # poll always 502

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    res = appmod.skyscanner_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 1, 0)
    assert res is None
    assert state["i"] == 2  # exactly SKYSCANNER_POLL_ATTEMPTS, not x PROVIDER_RETRIES


def test_request_with_retry_post_json_kwarg(monkeypatch, fake_resp):
    """A POST with a json= body forwards it to requests.post (json kwarg path)."""
    captured = {}

    def fake_post(url, **k):
        captured.update(k)
        return fake_resp({"ok": 1}, status=200)

    monkeypatch.setattr(appmod.requests, "post", fake_post)
    r = appmod._request_with_retry("POST", "http://x", json={"a": 1}, data={"b": 2},
                                   timeout=5)
    assert r.status_code == 200
    assert captured["json"] == {"a": 1} and captured["data"] == {"b": 2}


# ---------------------------------------------------------------------------
# Google Flights provider (synchronous /google/flights/search-roundtrip)
# ---------------------------------------------------------------------------

def _g_segment(dep_code, arr_code, dur, airline_name="Korean Air", airline_code="KE"):
    """One Google Flights segment mirroring the live /tmp/gsample.json shape."""
    return {
        "departureAirportCode": dep_code,
        "departureAirportName": dep_code + " Airport",
        "arrivalAirportName": arr_code + " Airport",
        "arrivalAirportCode": arr_code,
        "durationMinutes": dur,
        "departureTime": "10:00",
        "arrivalTime": "13:17",
        "cabinClass": 1,
        "airline": {"airlineCode": airline_code, "flightNumber": "78",
                    "airlineName": airline_name},
        "overnight": False,
    }


def _g_flight(price, stops, duration, segments, airline_names,
              transfer=None, available=True):
    """One Google Flights item (topFlights/otherFlights) mirroring the live shape."""
    return {
        "price": price,
        "returningToken": "tok-%s" % price,
        "airlineCode": "KE",
        "airlineNames": list(airline_names),
        "segments": segments,
        "departureAirportCode": segments[0]["departureAirportCode"],
        "arrivalAirportCode": segments[-1]["arrivalAirportCode"],
        "duration": duration,
        "stops": stops,
        "transferAirports": transfer,
        "fareId": None,
        "airline": [{"airlineCode": "KE", "airlineName": n,
                     "link": "https://x"} for n in airline_names],
        "isAvailable": available,
    }


def _g_body():
    """A realistic success body: a cheaper 2-stop Korean item (otherFlights),
    a 1-stop Air Canada item (topFlights), and a pricier nonstop item."""
    korean_2stop = _g_flight(
        5208, 2, 1965,
        [_g_segment("YYZ", "ICN", 780, "Korean Air", "KE"),
         _g_segment("ICN", "NRT", 120, "Korean Air", "KE"),
         _g_segment("NRT", "PEK", 200, "Korean Air", "KE")],
        ["Korean Air"])
    ac_1stop = _g_flight(
        5495, 1, 1160,
        [_g_segment("YYZ", "YVR", 317, "Air Canada", "AC"),
         _g_segment("YVR", "PEK", 775, "Air Canada", "AC")],
        ["Air Canada"])
    nonstop = _g_flight(
        7200, 0, 800,
        [_g_segment("YYZ", "PEK", 800, "Hainan Airlines", "HU")],
        ["Hainan Airlines"])
    return {
        "status": True,
        "message": "Successful",
        "data": {
            "topFlights": [ac_1stop],
            "otherFlights": [korean_2stop, nonstop],
            "filters": {},
            "priceHistory": [],
        },
    }


def test_google_flights_fare_none_without_key(monkeypatch):
    """No RAPIDAPI_KEY → None immediately (no request)."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", None)
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: pytest.fail("must not call when no key"))
    assert appmod.google_flights_fare("YYZ", "PEK", "2026-12-12",
                                      "2027-01-04", 2, 0) is None


def test_google_flights_fare_happy_path(monkeypatch, fake_resp):
    """Parses topFlights+otherFlights, picks the cheapest (2-stop Korean @5208),
    derives nonstop from the nonstop item, and normalizes the shared dict."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["params"] = params
        captured["timeout"] = timeout
        return fake_resp(_g_body(), status=200)

    monkeypatch.setattr(appmod.requests, "get", fake_get)
    res = appmod.google_flights_fare("YYZ", "PEK", "2026-12-12",
                                     "2027-01-04", 2, 2)

    # endpoint + auth + sync params
    assert captured["url"].endswith("/google/flights/search-roundtrip")
    assert captured["headers"]["x-rapidapi-host"] == appmod.RAPIDAPI_HOST
    assert captured["headers"]["x-rapidapi-key"] == "k"
    assert captured["params"]["departureId"] == "YYZ"
    assert captured["params"]["arrivalId"] == "PEK"
    assert captured["params"]["departureDate"] == "2026-12-12"
    assert captured["params"]["arrivalDate"] == "2027-01-04"
    assert captured["params"]["currency"] == "CAD"
    assert captured["params"]["adults"] == 2
    # children is a COUNT and is sent when > 0
    assert captured["params"]["children"] == 2
    # generous read timeout (connect, read) for the ~13s sync call
    assert captured["timeout"] == (10, 40)

    # normalized result
    assert res["source"] == "google"
    assert res["cheapest_cad"] == 5208           # party total, used as-is
    assert res["stops"] == 2                      # outbound stops on cheapest
    assert res["duration_min"] == 1965            # outbound minutes on cheapest
    assert res["airlines"] == ["Korean Air"]
    assert res["nonstop_cad"] == 7200             # the nonstop item
    assert res["nonstop_duration_min"] == 800
    assert res["nonstop_airlines"] == ["Hainan Airlines"]
    # layovers from cheapest segments: all arrivalAirportCodes except the last
    assert res["layovers"] == [
        {"iata": "ICN", "duration_min": None},
        {"iata": "NRT", "duration_min": None},
    ]
    assert res["book"] is None


def test_google_flights_fare_children_zero_omitted(monkeypatch, fake_resp):
    """children=0 → the children param is OMITTED (not sent)."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    captured = {}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda url, headers=None, params=None, timeout=None:
                        captured.update(params=params) or fake_resp(_g_body(), 200))
    appmod.google_flights_fare("YYZ", "PEK", "2026-12-12", "2027-01-04", 1, 0)
    assert "children" not in captured["params"]
    assert captured["params"]["adults"] == 1


def test_google_flights_fare_party_total_used_as_is(monkeypatch, fake_resp):
    """The item price is the PARTY TOTAL and is used verbatim (only rounded)."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    only = _g_flight(8987.4, 1, 1160,
                     [_g_segment("YYZ", "YVR", 317, "Air Canada", "AC"),
                      _g_segment("YVR", "PEK", 775, "Air Canada", "AC")],
                     ["Air Canada"])
    body = {"status": True, "message": "ok",
            "data": {"topFlights": [only], "otherFlights": []}}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(body, 200))
    res = appmod.google_flights_fare("YYZ", "PEK", "d", "r", 2, 2)
    assert res["cheapest_cad"] == 8987            # round(8987.4)
    assert res["nonstop_cad"] is None             # no nonstop item


def test_google_flights_fare_transfer_airports_preferred(monkeypatch, fake_resp):
    """A non-empty transferAirports list is used for layovers over segments."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    item = _g_flight(5208, 1, 1965,
                     [_g_segment("YYZ", "ICN", 780),
                      _g_segment("ICN", "PEK", 200)],
                     ["Korean Air"], transfer=["ICN"])
    body = {"status": True, "data": {"topFlights": [item], "otherFlights": []}}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(body, 200))
    res = appmod.google_flights_fare("YYZ", "PEK", "d", "r", 1, 0)
    assert res["layovers"] == [{"iata": "ICN", "duration_min": None}]


def test_google_flights_fare_nonstop_empty_layovers(monkeypatch, fake_resp):
    """A nonstop (single-segment) cheapest item → empty layovers list."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    item = _g_flight(4000, 0, 800,
                     [_g_segment("YYZ", "PEK", 800, "Hainan", "HU")],
                     ["Hainan"])
    body = {"status": True, "data": {"topFlights": [item], "otherFlights": []}}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(body, 200))
    res = appmod.google_flights_fare("YYZ", "PEK", "d", "r", 1, 0)
    assert res["layovers"] == []
    assert res["stops"] == 0
    assert res["nonstop_cad"] == 4000
    assert res["nonstop_duration_min"] == 800
    assert res["nonstop_airlines"] == ["Hainan"]


def test_google_flights_fare_airlines_from_airline_list(monkeypatch, fake_resp):
    """When airlineNames is absent, names fall back to airline[].airlineName."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    item = _g_flight(4000, 0, 800,
                     [_g_segment("YYZ", "PEK", 800)], ["Korean Air"])
    del item["airlineNames"]
    body = {"status": True, "data": {"topFlights": [item], "otherFlights": []}}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(body, 200))
    res = appmod.google_flights_fare("YYZ", "PEK", "d", "r", 1, 0)
    assert res["airlines"] == ["Korean Air"]


def test_google_flights_fare_no_response(monkeypatch):
    """_request_with_retry returning None (network/timeout) → None."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod, "_request_with_retry", lambda *a, **k: None)
    assert appmod.google_flights_fare("YYZ", "PEK", "d", "r", 1, 0) is None


def test_google_flights_fare_non_200(monkeypatch, fake_resp):
    """A non-200 status → None."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({}, status=500))
    assert appmod.google_flights_fare("YYZ", "PEK", "d", "r", 1, 0) is None


def test_google_flights_fare_unparseable_json(monkeypatch):
    """A 200 whose body is not JSON → None."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")

    class BadResp:
        status_code = 200
        def json(self):
            raise ValueError("nope")

    monkeypatch.setattr(appmod.requests, "get", lambda *a, **k: BadResp())
    assert appmod.google_flights_fare("YYZ", "PEK", "d", "r", 1, 0) is None


def test_google_flights_fare_status_false(monkeypatch, fake_resp):
    """status:false (error body) → None even with a non-null message."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    body = {"status": False, "message": "validation error",
            "data": None, "errors": ["bad"]}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(body, 200))
    assert appmod.google_flights_fare("YYZ", "PEK", "d", "r", 1, 0) is None


def test_google_flights_fare_data_not_dict(monkeypatch, fake_resp):
    """status:true but data not a dict → None."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    body = {"status": True, "message": "ok", "data": None}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(body, 200))
    assert appmod.google_flights_fare("YYZ", "PEK", "d", "r", 1, 0) is None


def test_google_flights_fare_empty_flights(monkeypatch, fake_resp):
    """topFlights/otherFlights both empty → None."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    body = {"status": True, "data": {"topFlights": [], "otherFlights": []}}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(body, 200))
    assert appmod.google_flights_fare("YYZ", "PEK", "d", "r", 1, 0) is None


def test_google_flights_fare_skips_priceless_items(monkeypatch, fake_resp):
    """Items without a numeric price are skipped; a bool price is NOT numeric."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    no_price = _g_flight(5000, 1, 900,
                         [_g_segment("YYZ", "ICN", 500),
                          _g_segment("ICN", "PEK", 300)], ["Korean Air"])
    del no_price["price"]
    bool_price = _g_flight(1, 0, 800,
                           [_g_segment("YYZ", "PEK", 800)], ["X"])
    bool_price["price"] = True            # must NOT count as a numeric price
    good = _g_flight(6000, 1, 1000,
                     [_g_segment("YYZ", "YVR", 400, "Air Canada", "AC"),
                      _g_segment("YVR", "PEK", 600, "Air Canada", "AC")],
                     ["Air Canada"])
    body = {"status": True,
            "data": {"topFlights": [no_price, bool_price],
                     "otherFlights": [good, "not-a-dict"]}}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(body, 200))
    res = appmod.google_flights_fare("YYZ", "PEK", "d", "r", 1, 0)
    assert res["cheapest_cad"] == 6000       # only the well-formed priced item
    assert res["nonstop_cad"] is None        # the bool-priced "nonstop" is ignored


def test_google_flights_fare_non_numeric_stops_duration(monkeypatch, fake_resp):
    """Non-numeric stops/duration degrade to None and never count as nonstop.

    stops=True (bool → not numeric) and duration="800" (string → not numeric) must
    yield stops=None / duration_min=None, and the item is NOT treated as nonstop.
    """
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    weird = _g_flight(4000, True, 800,
                      [_g_segment("YYZ", "PEK", 800)], ["X"])
    weird["duration"] = "800"            # stringified duration
    body = {"status": True, "data": {"topFlights": [weird], "otherFlights": []}}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(body, 200))
    res = appmod.google_flights_fare("YYZ", "PEK", "d", "r", 1, 0)
    assert res["cheapest_cad"] == 4000   # price still real → still returned
    assert res["stops"] is None          # bool stops not numeric
    assert res["duration_min"] is None   # string duration not numeric
    assert res["nonstop_cad"] is None    # bool stops != 0 → not nonstop
    assert res["nonstop_duration_min"] is None


def test_google_flights_fare_non_list_buckets(monkeypatch, fake_resp):
    """topFlights/otherFlights of a non-list type are treated as empty → None."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    body = {"status": True, "data": {"topFlights": "x", "otherFlights": 5}}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(body, 200))
    assert appmod.google_flights_fare("YYZ", "PEK", "d", "r", 1, 0) is None


def test_google_flights_fare_layovers_non_list_segments(monkeypatch, fake_resp):
    """A cheapest item whose segments is not a list → layovers []."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    item = _g_flight(4000, 0, 800,
                     [_g_segment("YYZ", "PEK", 800)], ["X"])
    item["segments"] = None
    item["transferAirports"] = None
    body = {"status": True, "data": {"topFlights": [item], "otherFlights": []}}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(body, 200))
    res = appmod.google_flights_fare("YYZ", "PEK", "d", "r", 1, 0)
    assert res["layovers"] == []


def test_google_flights_fare_verbose_trace(monkeypatch, fake_resp, caplog):
    """SKYSCANNER_VERBOSE on emits a one-line google trace at INFO."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    monkeypatch.setattr(appmod, "SKYSCANNER_VERBOSE", True)
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(_g_body(), 200))
    with caplog.at_level("INFO"):
        appmod.google_flights_fare("YYZ", "PEK", "d", "r", 1, 0)
    assert any("google" in r.message for r in caplog.records)


def test_google_tried_first_in_provider_chain(monkeypatch):
    """google_flights_fare is tried BEFORE skyscanner and every other provider."""
    call_order = []
    google_result = {"cheapest_cad": 5208, "stops": 2, "nonstop_cad": 7200,
                     "source": "google", "book": None}
    monkeypatch.setattr(appmod, "google_flights_fare",
                        lambda *a: call_order.append("google") or google_result)
    monkeypatch.setattr(appmod, "skyscanner_fare",
                        lambda *a: call_order.append("skyscanner") or None)
    monkeypatch.setattr(appmod, "serpapi_fare",
                        lambda *a: call_order.append("serpapi") or None)
    monkeypatch.setattr(appmod, "amadeus_fare",
                        lambda *a: call_order.append("amadeus") or None)
    monkeypatch.setattr(appmod, "travelpayouts_fare",
                        lambda *a: call_order.append("travelpayouts") or None)
    monkeypatch.setattr(appmod, "kiwi_fare",
                        lambda *a: call_order.append("kiwi") or None)
    monkeypatch.setattr(appmod, "FARE_CACHE_TTL", 0)

    res = appmod.get_fare("YYZ", "PEK", "2026-12-12", "2027-01-04", 2, 0)

    assert call_order == ["google"]      # google succeeded → nothing else tried
    assert res["source"] == "google"
    assert "skyscanner" not in call_order


def test_providers_configured_includes_google_when_key_set(monkeypatch):
    """providers_configured() lists 'google' FIRST (before skyscanner) when keyed."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", "k")
    p = appmod.providers_configured()
    assert "google" in p
    assert "skyscanner" in p
    assert p.index("google") < p.index("skyscanner")


def test_providers_configured_excludes_google_when_unset(monkeypatch):
    """No RAPIDAPI_KEY → neither google nor skyscanner is configured."""
    monkeypatch.setattr(appmod, "RAPIDAPI_KEY", None)
    p = appmod.providers_configured()
    assert "google" not in p
    assert "skyscanner" not in p


def test_google_first_in_provider_chain_constant(monkeypatch):
    """The _PROVIDER_CHAIN compare ordering also lists google first."""
    assert appmod._PROVIDER_CHAIN[0][0] == "google"
    # and its lambda dispatches to google_flights_fare
    called = {}
    monkeypatch.setattr(appmod, "google_flights_fare",
                        lambda *a: called.setdefault("n", a))
    appmod._PROVIDER_CHAIN[0][1]("YYZ", "PEK", "d", "r", 1, 0)
    assert called["n"] == ("YYZ", "PEK", "d", "r", 1, 0)
