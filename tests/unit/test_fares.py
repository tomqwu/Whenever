import app as appmod


def _amadeus_offer(grand_total, segs):
    return {
        "price": {"grandTotal": str(grand_total)},
        "itineraries": [{"segments": [{} for _ in range(segs)]}],
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
        _amadeus_offer(8000, 2),   # 1 stop, cheapest
        _amadeus_offer(14000, 1),  # nonstop
    ]
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp({"data": offers}, status=200))
    res = appmod.amadeus_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res == {"cheapest_cad": 8000, "stops": 1, "nonstop_cad": 14000, "source": "amadeus"}


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
        {"price": 1000, "transfers": 1, "return_transfers": 0, "link": "/deal/abc"},
        {"price": 1500, "transfers": 0, "return_transfers": 0, "link": "/ns"},
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
    assert res == {"cheapest_cad": None, "stops": None, "nonstop_cad": None, "source": "no-data"}


# ---------------------------------------------------------------------------
# kiwi_fare tests
# ---------------------------------------------------------------------------

def _kiwi_itinerary(price, route_segments):
    """Build a minimal Tequila-shaped itinerary dict.

    route_segments: list of (return_flag,) tuples, e.g.
      [(0,), (0,), (1,), (1,)] = 2 outbound + 2 return segments (each 1 stop per direction)
    """
    return {
        "price": price,
        "deep_link": f"https://www.kiwi.com/deep?p={price}",
        "route": [{"return": flag} for flag in route_segments],
    }


def test_kiwi_fare_none_without_key(monkeypatch):
    """kiwi_fare returns None immediately when KIWI_API_KEY is not set."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", None)
    assert appmod.kiwi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0) is None


def test_kiwi_fare_happy_path(monkeypatch, fake_resp):
    """Happy path: two itineraries (1-stop cheapest + nonstop pricier) normalize correctly."""
    monkeypatch.setattr(appmod, "KIWI_API_KEY", "k")
    # 1-stop outbound (2 segs), 1-stop return (2 segs) = max stops = 1
    itin_cheap = _kiwi_itinerary(7000, [0, 0, 1, 1])
    # nonstop: 1 outbound + 1 return = max stops = 0
    itin_nonstop = _kiwi_itinerary(9500, [0, 1])
    payload = {"data": [itin_cheap, itin_nonstop]}
    monkeypatch.setattr(appmod.requests, "get",
                        lambda *a, **k: fake_resp(payload, status=200))
    res = appmod.kiwi_fare("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, 0)
    assert res["cheapest_cad"] == 7000
    assert res["stops"] == 1
    assert res["nonstop_cad"] == 9500
    assert res["source"] == "kiwi"
    assert res["book"] == "https://www.kiwi.com/deep?p=7000"


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
