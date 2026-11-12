"""Travelpayouts adapter: normalization, request building, HTTP behaviour (all mocked)."""

from datetime import date, datetime, timezone

import httpx
import pytest

from app.core.errors import (
    ProviderAuthError,
    ProviderBlockedError,
    ProviderNotConfiguredError,
    ProviderRateLimitedError,
    ProviderRequestError,
    ProviderTemporaryError,
)
from app.models.schemas import PriceType, SearchQuery, TripType
from app.providers.travelpayouts import TravelpayoutsProvider

NOW = datetime(2027, 2, 1, tzinfo=timezone.utc)


def query(**kw):
    base = dict(origin="OPO", destination="KRK", trip_type=TripType.RETURN, departure_month=date(2027, 3, 1))
    base.update(kw)
    return SearchQuery(**base)


def provider(handler=None, **kw):
    transport = httpx.MockTransport(handler) if handler else None
    return TravelpayoutsProvider("TEST-TOKEN", transport=transport, sleep=lambda s: None,
                                 min_interval_seconds=0, **kw)


def test_normalizes_return_offers(tp_fixture):
    result = provider().parse_response(tp_fixture, query(), collected_at=NOW)
    # LIS row filtered (wrong origin), invalid price row skipped
    assert len(result.offers) == 3
    assert result.skipped_items == 1
    o = result.offers[0]
    assert o.provider == "travelpayouts"
    assert o.price == 45 and o.currency == "EUR"
    assert o.airline == "FR" and o.flight_number == "1234"
    assert o.stops == 0 and o.return_stops == 0
    assert o.duration_minutes == 205 and o.return_duration_minutes == 205
    assert o.departure_at.utcoffset().total_seconds() == 0
    assert o.return_departure_at.utcoffset().total_seconds() == 3600
    assert o.booking_url == "https://www.aviasales.com/search/OPO0503KRK12031?t=TESTDATA"
    assert o.price_type == PriceType.INDICATIVE  # cached data is never "confirmed"
    assert o.baggage_included is None  # unknown, not assumed


def test_missing_fields_become_none(tp_fixture):
    offers = provider().parse_response(tp_fixture, query(), collected_at=NOW).offers
    no_airline = offers[2]
    assert no_airline.airline is None and no_airline.flight_number is None
    assert no_airline.duration_minutes is None
    # numeric flight number is converted to string
    assert offers[1].flight_number == "432"


def test_price_multiplied_by_passengers(tp_fixture):
    offers = provider().parse_response(tp_fixture, query(passengers=2), collected_at=NOW).offers
    assert offers[0].price == 90 and offers[0].passengers == 2


def test_one_way_query_ignores_return_fields(tp_fixture):
    offers = provider().parse_response(tp_fixture, query(trip_type=TripType.ONE_WAY), collected_at=NOW).offers
    assert all(o.return_departure_at is None and o.return_stops is None for o in offers)


def test_return_query_drops_rows_without_return():
    payload = {"success": True, "currency": "eur", "data": [
        {"origin": "OPO", "destination": "KRK", "price": 30, "departure_at": "2027-03-05T06:30:00+00:00"}]}
    assert provider().parse_response(payload, query(), collected_at=NOW).offers == []


def test_build_params_month_and_currency():
    p = provider(result_limit=500).build_params(query(currency="eur"))
    assert p["departure_at"] == "2027-03" and p["one_way"] == "false"
    assert p["currency"] == "eur" and p["limit"] == 500
    assert "token" not in p  # token must only travel in a header
    p2 = provider().build_params(query(trip_type=TripType.ONE_WAY, departure_month=None,
                                       departure_date=date(2027, 3, 4)))
    assert p2["departure_at"] == "2027-03-04" and p2["one_way"] == "true"


def test_search_sends_token_header_and_parses(tp_fixture):
    seen = {}

    def handler(request: httpx.Request):
        seen["token"] = request.headers.get("X-Access-Token")
        seen["url"] = str(request.url)
        return httpx.Response(200, json=tp_fixture)

    result = provider(handler).search_flights(query())
    assert seen["token"] == "TEST-TOKEN"
    assert "TEST-TOKEN" not in seen["url"]
    assert len(result.offers) == 3 and result.requests_made == 1 and not result.from_cache


def test_not_configured():
    with pytest.raises(ProviderNotConfiguredError):
        TravelpayoutsProvider(None).search_flights(query())
    assert TravelpayoutsProvider(None).health_check().status == "not_configured"


def test_api_error_payload():
    p = provider(lambda r: httpx.Response(200, json={"success": False, "error": "bad params"}))
    with pytest.raises(ProviderRequestError):
        p.search_flights(query())


def test_unauthorized():
    p = provider(lambda r: httpx.Response(401, json={"error": "Unauthorized"}))
    with pytest.raises(ProviderAuthError):
        p.search_flights(query())
    assert p.health_check().status == "auth_error"


def test_retries_429_then_succeeds(tp_fixture):
    calls = {"n": 0}
    sleeps = []

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={})
        return httpx.Response(200, json=tp_fixture)

    p = TravelpayoutsProvider("TEST-TOKEN", transport=httpx.MockTransport(handler),
                              sleep=sleeps.append, min_interval_seconds=0)
    result = p.search_flights(query())
    assert calls["n"] == 3 and len(result.offers) == 3
    assert sleeps == [7.0, 7.0]  # Retry-After honoured


def test_rate_limited_after_retries():
    p = provider(lambda r: httpx.Response(429, json={}), max_retries=2)
    with pytest.raises(ProviderRateLimitedError):
        p.search_flights(query())


def test_server_errors_backoff_exponential():
    sleeps = []
    p = TravelpayoutsProvider("TEST-TOKEN", transport=httpx.MockTransport(lambda r: httpx.Response(503, json={})),
                              sleep=sleeps.append, min_interval_seconds=0, max_retries=3)
    with pytest.raises(ProviderTemporaryError):
        p.search_flights(query())
    assert sleeps == [2.0, 4.0, 8.0]


def test_network_error_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("boom")

    p = provider(handler, max_retries=1)
    with pytest.raises(ProviderTemporaryError):
        p.search_flights(query())
    assert calls["n"] == 2


@pytest.mark.parametrize("status,body", [
    (403, "<html><title>Attention Required!</title>cf-chl captcha</html>"),
    (200, "<html><body>Please complete the CAPTCHA</body></html>"),
    (503, "<html>Checking your browser... challenge-platform</html>"),
])
def test_bot_challenge_stops_without_retry(status, body):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(status, text=body, headers={"content-type": "text/html"})

    with pytest.raises(ProviderBlockedError):
        provider(handler).search_flights(query())
    assert calls["n"] == 1  # never retried


def test_response_cache_hit_skips_request(tp_fixture):
    class MemCache:
        def __init__(self):
            self.d = {}

        def get(self, k):
            return self.d.get(k)

        def set(self, k, provider, v, ttl):
            self.d[k] = v

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=tp_fixture)

    p = provider(handler, cache=MemCache(), cache_ttl_seconds=60)
    first = p.search_flights(query())
    second = p.search_flights(query())
    assert calls["n"] == 1
    assert not first.from_cache and second.from_cache
    assert second.requests_made == 0
