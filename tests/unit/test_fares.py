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
    assert res == {"cheapest_cad": 8000, "stops": 1, "nonstop_cad": 14000,
                   "source": "amadeus", "duration_min": 600,
                   "nonstop_duration_min": 240}


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
                   "nonstop_duration_min": None}


# ---------------------------------------------------------------------------
# kiwi_fare tests
# ---------------------------------------------------------------------------

def _kiwi_itinerary(price, route_segments, duration=None):
    """Build a minimal Tequila-shaped itinerary dict.

    route_segments: list of (return_flag,) tuples, e.g.
      [(0,), (0,), (1,), (1,)] = 2 outbound + 2 return segments (each 1 stop per direction)
    duration: optional Tequila `duration` value (dict/seconds) for duration_min.
    """
    itin = {
        "price": price,
        "deep_link": f"https://www.kiwi.com/deep?p={price}",
        "route": [{"return": flag} for flag in route_segments],
    }
    if duration is not None:
        itin["duration"] = duration
    return itin


def test_kiwi_fare_none_without_key(monkeypatch):
    """kiwi_fare returns None immediately when KIWI_API_KEY is not set."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", None)
    assert appmod.kiwi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_kiwi_fare_happy_path(monkeypatch, fake_resp):
    """Happy path: two itineraries (1-stop cheapest + nonstop pricier) normalize correctly."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")
    # 1-stop outbound (2 segs), 1-stop return (2 segs) = max stops = 1; 52500s -> 875 min
    itin_cheap = _kiwi_itinerary(7000, [0, 0, 1, 1], duration={"total": 52500})
    # nonstop: 1 outbound + 1 return = max stops = 0; 36000s -> 600 min
    itin_nonstop = _kiwi_itinerary(9500, [0, 1], duration={"total": 36000})
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
    # no nonstop itinerary → nonstop_duration_min is None
    assert res["nonstop_duration_min"] is None


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


def test_serpapi_tried_first_in_provider_chain(monkeypatch):
    """serpapi_fare is called FIRST — before amadeus, travelpayouts, kiwi."""
    call_order = []
    serpapi_result = {"cheapest_cad": 2675, "stops": 1, "nonstop_cad": 3500,
                      "source": "serpapi", "book": None}
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

    assert call_order[0] == "serpapi"
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
