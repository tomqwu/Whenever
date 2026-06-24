import pytest
import app as appmod


class FakeResp:
    """Stand-in for a requests.Response."""
    def __init__(self, json_data=None, status=200, raise_exc=None):
        self._json = {} if json_data is None else json_data
        self.status_code = status
        self._raise = raise_exc

    def json(self):
        return self._json

    def raise_for_status(self):
        if self._raise is not None:
            raise self._raise


@pytest.fixture
def fake_resp():
    return FakeResp


@pytest.fixture
def client():
    appmod.app.config.update(TESTING=True)
    return appmod.app.test_client()


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Caches and the Amadeus token leak across tests; reset them.

    Also clear all provider-credential globals so that ambient env vars
    (e.g. KIWI_API_KEY exported in dev/CI) cannot affect provider-list
    assertions.  Each test that needs a provider must set it explicitly
    via its own monkeypatch; those patches are applied after this one and
    take effect correctly because monkeypatch stacks.
    """
    monkeypatch.setattr(appmod, "AMADEUS_ID", None)
    monkeypatch.setattr(appmod, "AMADEUS_SECRET", None)
    monkeypatch.setattr(appmod, "TRAVELPAYOUTS_TOKEN", None)
    monkeypatch.setattr(appmod, "KIWI_API_KEY", None)
    monkeypatch.setattr(appmod, "SERPAPI_KEY", None)
    appmod.top_cities.cache_clear()
    appmod.resolve_airport.cache_clear()
    appmod._amadeus_token["value"] = None
    appmod._amadeus_token["exp"] = 0
    appmod._fare_cache.clear()
    yield
    if hasattr(appmod.top_cities, "cache_clear"):
        appmod.top_cities.cache_clear()
    if hasattr(appmod.resolve_airport, "cache_clear"):
        appmod.resolve_airport.cache_clear()
    appmod._fare_cache.clear()
