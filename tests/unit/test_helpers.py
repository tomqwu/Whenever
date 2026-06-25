import pytest
import app as appmod


def test_extract_json_array():
    assert appmod.extract_json('```json\n[{"a":1}]\n```') == [{"a": 1}]


def test_extract_json_object():
    assert appmod.extract_json('noise {"x": 2} tail') == {"x": 2}


def test_extract_json_raises_when_absent():
    with pytest.raises(ValueError):
        appmod.extract_json("no json here")


def test_date_range():
    assert appmod.date_range("2026-12-12", 3) == [
        "2026-12-12", "2026-12-13", "2026-12-14",
    ]


def test_date_range_caps_huge_count():
    """A huge count must not allocate millions of dates; clamp to MAX_DATE_SPAN."""
    out = appmod.date_range("2026-12-12", 10_000_000)
    assert len(out) <= appmod.MAX_DATE_SPAN
    assert len(out) == appmod.MAX_DATE_SPAN  # capped exactly at the ceiling


def test_date_range_floors_negative_count():
    """A negative count floors at 0 rather than producing junk."""
    assert appmod.date_range("2026-12-12", -5) == []


def test_date_range_non_int_count_returns_empty():
    """A non-int count returns [] (invalid), not a crash."""
    assert appmod.date_range("2026-12-12", "abc") == []


def test_kayak_link_without_children():
    url = appmod.kayak_link("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, [])
    assert url == "https://www.kayak.com/flights/YYZ-PVG/2026-12-12/2027-01-04/2adults"


def test_kayak_link_with_children():
    url = appmod.kayak_link("YYZ", "PVG", "2026-12-12", "2027-01-04", 2, [11, 9])
    assert url.endswith("/2adults/children-11-9")


def test_providers_configured_combinations(monkeypatch):
    monkeypatch.setattr(appmod, "AMADEUS_ID", "id")
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", "secret")
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", "tok")
    assert appmod.providers_configured() == ["amadeus", "travelpayouts"]

    monkeypatch.setattr(appmod, "AMADEUS_ID", None)
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", None)
    assert appmod.providers_configured() == []
