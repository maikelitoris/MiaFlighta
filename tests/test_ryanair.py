"""Ryanair fare-finder provider. All responses are mocked TEST DATA (not real fares).
Response shapes follow the community ryanair-py library's fixtures and public docs."""

from datetime import date, datetime, timedelta

import httpx
import pytest

from app.core.errors import ProviderBlockedError
from app.models.schemas import PriceType, SearchQuery, TripType
from app.providers.ryanair import RyanairProvider, parse_cheapest_per_day, parse_one_way_fares


def day_fare(d: date, price, dep="06:30", arr="11:20", **flags):
    """One TEST DATA cheapestPerDay entry."""
    entry = {
        "day": d.isoformat(),
        "departureDate": f"{d.isoformat()}T{dep}:00",
        "arrivalDate": f"{d.isoformat()}T{arr}:00",
        "price": None if price is None else {"value": price, "currencyCode": "EUR", "currencySymbol": "€"},
        "soldOut": False,
        "unavailable": price is None,
    }
    entry.update(flags)
    return entry


def month_payload(month: date, prices: dict[int, float | None]) -> dict:
    return {"outbound": {"fares": [day_fare(month.replace(day=d), p) for d, p in prices.items()]}}


def one_way_payload(origin, dest, dep: datetime, price, flight="FR8123"):
    return {"fares": [{"outbound": {
        "departureAirport": {"iataCode": origin, "name": "TEST"},
        "arrivalAirport": {"iataCode": dest, "name": "TEST"},
        "departureDate": dep.isoformat(), "arrivalDate": (dep + timedelta(hours=4)).isoformat(),
        "price": {"value": price, "currencyCode": "EUR"}, "flightNumber": flight,
    }, "summary": {"price": {"value": price, "currencyCode": "EUR"}}}]}


class FakeRyanair:
    """Routes mocked requests: {(origin, dest, 'YYYY-MM'): payload}."""

    def __init__(self, months: dict, home_status=200, home_body="<html>ok</html>"):
        self.months = months
        self.home_status = home_status
        self.home_body = home_body
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if request.url.host == "www.ryanair.com":
            return httpx.Response(self.home_status, text=self.home_body, headers={"content-type": "text/html"})
        if path.endswith("/cheapestPerDay"):
            parts = path.split("/")
            origin, dest = parts[-3], parts[-2]
            month = request.url.params["outboundMonthOfDate"][:7]
            payload = self.months.get((origin, dest, month))
            if payload is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=payload)
        if path.endswith("/oneWayFares"):
            p = request.url.params
            payload = self.months.get(("fallback", p["departureAirportIataCode"], p["arrivalAirportIataCode"]))
            return httpx.Response(200, json=payload or {"fares": []})
        return httpx.Response(500)


def provider(fake, **kw):
    return RyanairProvider(transport=httpx.MockTransport(fake), sleep=lambda s: None, min_interval_seconds=0, **kw)


DEC = date(2027, 12, 1)
JAN = date(2028, 1, 1)


def q(trip=TripType.ONE_WAY, **kw):
    return SearchQuery(origin="OPO", destination="KRK", trip_type=trip, departure_month=DEC, **kw)


def test_parse_cheapest_per_day_skips_unavailable():
    payload = month_payload(DEC, {1: 19.99, 2: None, 3: 25})
    payload["outbound"]["fares"].append(day_fare(DEC.replace(day=4), 10, soldOut=True))
    fares = parse_cheapest_per_day(payload, "EUR")
    assert sorted(fares) == [date(2027, 12, 1), date(2027, 12, 3)]
    f = fares[date(2027, 12, 1)]
    assert f.price == 19.99 and f.currency == "EUR"
    assert f.departure == datetime(2027, 12, 1, 6, 30) and f.arrival == datetime(2027, 12, 1, 11, 20)


def test_parse_one_way_fares_strips_flight_prefix():
    fares = parse_one_way_fares(one_way_payload("OPO", "KRK", datetime(2027, 12, 5, 7), 33.5), "OPO", "KRK")
    assert fares[date(2027, 12, 5)].flight_number == "8123"
    assert parse_one_way_fares(one_way_payload("LIS", "KRK", datetime(2027, 12, 5, 7), 1), "OPO", "KRK") == {}


def test_one_way_offers(tmp_path):
    fake = FakeRyanair({("OPO", "KRK", "2027-12"): month_payload(DEC, {5: 29.99, 6: None, 12: 41})})
    res = provider(fake).search_flights(q(passengers=2))
    assert [o.departure_at.day for o in res.offers] == [5, 12]
    o = res.offers[0]
    assert o.provider == "ryanair" and o.airline == "FR" and o.stops == 0
    assert o.price == 59.98 and o.passengers == 2  # price per adult x 2
    assert o.baggage_included is False and "under-seat" in o.baggage_info
    assert o.price_type == PriceType.INDICATIVE
    assert o.arrival_at == datetime(2027, 12, 5, 11, 20)
    assert o.booking_url.startswith("https://www.ryanair.com/") and "dateOut=2027-12-05" in o.booking_url
    assert "adults=2" in o.booking_url
    assert res.requests_made == 2  # home page + one month


def test_return_trips_pick_cheapest_valid_return_across_months():
    fake = FakeRyanair({
        ("OPO", "KRK", "2027-12"): month_payload(DEC, {28: 30}),
        ("KRK", "OPO", "2027-12"): month_payload(DEC, {29: 5, 31: 22}),  # 29 = 1-day stay: too short
        ("KRK", "OPO", "2028-01"): month_payload(JAN, {2: 18, 9: 12}),  # 9 Jan = 12-day stay: too long
    })
    res = provider(fake).search_flights(q(TripType.RETURN, min_stay_days=3, max_stay_days=10))
    assert len(res.offers) == 1
    o = res.offers[0]
    assert o.return_departure_at.date() == date(2028, 1, 2)  # cheapest within 3-10 days
    assert o.price == 48 and o.return_stops == 0
    assert "dateIn=2028-01-02" in o.booking_url and "isReturn=true" in o.booking_url


def test_return_weekday_filter():
    fake = FakeRyanair({
        ("OPO", "KRK", "2027-12"): month_payload(DEC, {3: 30}),  # Fri 3 Dec 2027
        ("KRK", "OPO", "2027-12"): month_payload(DEC, {6: 10, 5: 40}),  # Mon 6 cheaper, Sun 5
    })
    res = provider(fake).search_flights(q(TripType.RETURN, min_stay_days=1, max_stay_days=5, return_weekdays=[6]))
    assert res.offers[0].return_departure_at.date() == date(2027, 12, 5)  # Sunday only


def test_inbound_month_fetched_once_per_run():
    fake = FakeRyanair({
        ("OPO", "KRK", "2027-12"): month_payload(DEC, {10: 30}),
        ("KRK", "OPO", "2027-12"): month_payload(DEC, {15: 20}),
        ("KRK", "OPO", "2028-01"): month_payload(JAN, {}),
    })
    p = provider(fake)
    p.search_flights(q(TripType.RETURN, min_stay_days=3, max_stay_days=10))
    p.search_flights(q(TripType.ONE_WAY))
    # outbound Dec + inbound Dec (stay 13-20 Dec stays in December); the one-way query reuses the memo
    assert sum("cheapestPerDay" in c for c in fake.calls) == 2


def test_fallback_to_one_way_fares_when_calendar_missing():
    fake = FakeRyanair({("fallback", "OPO", "KRK"): one_way_payload("OPO", "KRK", datetime(2027, 12, 9, 7), 27)})
    res = provider(fake).search_flights(q())
    assert len(res.offers) == 1 and res.offers[0].flight_number == "8123" and res.offers[0].price == 27


def test_route_not_served_returns_empty():
    res = provider(FakeRyanair({})).search_flights(q())
    assert res.offers == []


def test_blocked_home_page_stops():
    fake = FakeRyanair({}, home_status=403, home_body="<html>Access Denied - captcha</html>")
    with pytest.raises(ProviderBlockedError):
        provider(fake).search_flights(q())
    assert len(fake.calls) == 1


def test_challenge_on_api_stops_without_retry():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if request.url.host == "www.ryanair.com":
            return httpx.Response(200, text="<html>home</html>", headers={"content-type": "text/html"})
        return httpx.Response(403, text="<html>Please verify you are human: captcha</html>",
                              headers={"content-type": "text/html"})

    with pytest.raises(ProviderBlockedError):
        provider(handler).search_flights(q())
    assert calls["n"] == 2


def test_honest_user_agent():
    seen = []

    def handler(request):
        seen.append(request.headers["user-agent"])
        return httpx.Response(200, json=month_payload(DEC, {}))

    provider(handler, warm_up=False).search_flights(q())
    assert seen and all(ua.startswith("FlightRadar/") for ua in seen)
