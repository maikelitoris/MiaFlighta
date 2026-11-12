"""Ryanair fare-finder provider (airline's own public website data).

How it works: ryanair.com's own pages load fares from JSON endpoints under
services-api.ryanair.com/farfnd/v4/. We call the same endpoints directly, because
that is far lighter for Ryanair than driving a full browser:

* cheapestPerDay: the lowest fare for every day of a month on one route and
  direction, so ONE request covers a month;
* oneWayFares: fallback that returns the single cheapest fare in a date range.

Terms: Ryanair's website terms allow use of the site, its data and its APIs for
private, non-commercial purposes, and forbid screen-scraping for commercial
purposes. This provider is meant ONLY for that personal use:
* it sends an honest User-Agent and does not pretend to be a browser;
* it keeps volume tiny and polite (a few dozen requests per run);
* it stops at the first sign of a block or challenge (see app.core.http).
Do not use it commercially or republish the data.

Limits:
* these endpoints are undocumented and Ryanair may change them at any time;
* prices are Ryanair's current lowest fare for one adult on the basic fare
  (small under-seat bag only), which is indicative until you open the booking page;
* cheapestPerDay gives no flight numbers;
* return trips are built from two one-way fares — Ryanair prices each direction
  separately, so the sum is the real return price.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.errors import ProviderError, ProviderRequestError
from app.core.http import PoliteHttpClient, ResponseCache
from app.core.timeutils import month_starts
from app.models.schemas import FlightOfferData, PriceType, SearchQuery, TripType
from app.providers.base import FlightProvider, HealthResult, ProviderResult

log = logging.getLogger(__name__)

API_BASE = "https://services-api.ryanair.com/farfnd/v4/"
HOME_URL = "https://www.ryanair.com/ie/en"
BOOKING_URL = "https://www.ryanair.com/ie/en/trip/flights/select"
BAGGAGE_INFO = "Ryanair basic fare: small under-seat bag only; cabin/checked bags cost extra"
DEFAULT_MAX_STAY = 30
MEMO_TTL_SECONDS = 15 * 60


@dataclass(frozen=True)
class Fare:
    day: date
    departure: datetime  # local wall-clock time at the departure airport
    arrival: datetime | None
    price: float  # one adult
    currency: str
    flight_number: str | None = None


class RyanairProvider(FlightProvider):
    name = "ryanair"
    price_type = PriceType.INDICATIVE
    coverage_note = (
        "Ryanair only, from the airline's own fare finder (personal, non-commercial use). "
        "Lowest fare per day, basic fare without bags."
    )
    supports_month_search = True

    def __init__(
        self,
        *,
        min_interval_seconds: float = 3.0,
        max_retries: int = 2,
        cache: ResponseCache | None = None,
        cache_ttl_seconds: float = 3600,
        warm_up: bool = True,
        transport: httpx.BaseTransport | None = None,
        sleep=None,
    ) -> None:
        kwargs: dict[str, Any] = {"sleep": sleep} if sleep is not None else {}
        self.http = PoliteHttpClient(
            self.name,
            min_interval_seconds=min_interval_seconds,
            max_retries=max_retries,
            cache=cache,
            cache_ttl_seconds=cache_ttl_seconds,
            transport=transport,
            **kwargs,
        )
        self.warm_up = warm_up
        self._warmed = False
        # Same-run memo so the inbound month used by several queries is fetched once
        # (and still counts as a fresh observation).
        self._memo: dict[tuple, tuple[float, dict[date, Fare], bool]] = {}

    def is_configured(self) -> bool:
        return True  # no key needed

    def close(self) -> None:
        self.http.close()

    # ------------------------------------------------------------------ fetching
    def _ensure_session(self) -> None:
        if self.warm_up and not self._warmed:
            status = self.http.visit(HOME_URL)  # raises ProviderBlockedError on a challenge
            log.debug("ryanair: home page HTTP %s", status)
            self._warmed = True

    def _fares_for_month(self, origin: str, dest: str, month: date, currency: str) -> tuple[dict[date, Fare], bool]:
        """Cheapest fare per day for one direction and month -> ({day: Fare}, from_cache)."""
        key = (origin, dest, month, currency)
        memo = self._memo.get(key)
        if memo and time.monotonic() - memo[0] < MEMO_TTL_SECONDS:
            return memo[1], memo[2]
        self._ensure_session()
        url = f"{API_BASE}oneWayFares/{origin}/{dest}/cheapestPerDay"
        params = {"outboundMonthOfDate": month.replace(day=1).isoformat(), "currency": currency}
        try:
            payload, from_cache = self.http.get_json(url, params)
            fares = parse_cheapest_per_day(payload, currency)
        except ProviderRequestError as exc:
            if exc.status_code not in (None, 400, 404):
                raise
            log.info("ryanair: cheapestPerDay unavailable for %s->%s %s (%s); trying oneWayFares",
                     origin, dest, month, exc)
            fares, from_cache = self._fallback_month(origin, dest, month, currency)
        self._memo[key] = (time.monotonic(), fares, from_cache)
        return fares, from_cache

    def _fallback_month(self, origin: str, dest: str, month: date, currency: str) -> tuple[dict[date, Fare], bool]:
        next_month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        params = {
            "departureAirportIataCode": origin,
            "arrivalAirportIataCode": dest,
            "outboundDepartureDateFrom": month.replace(day=1).isoformat(),
            "outboundDepartureDateTo": (next_month - timedelta(days=1)).isoformat(),
            "currency": currency,
        }
        try:
            payload, from_cache = self.http.get_json(f"{API_BASE}oneWayFares", params)
        except ProviderRequestError as exc:
            if exc.status_code == 404:
                return {}, False  # route not served
            raise
        return parse_one_way_fares(payload, origin, dest), from_cache

    # ------------------------------------------------------------------ interface
    def search_flights(self, query: SearchQuery) -> ProviderResult:
        before = self.http.requests_made
        month = query.departure_month or query.departure_date.replace(day=1)
        out_fares, out_cached = self._fares_for_month(query.origin, query.destination, month, query.currency)
        if query.departure_date:
            out_fares = {d: f for d, f in out_fares.items() if d == query.departure_date}
        collected = datetime.now(timezone.utc)
        result = ProviderResult(from_cache=out_cached)

        if query.trip_type == TripType.ONE_WAY:
            for fare in sorted(out_fares.values(), key=lambda f: f.day):
                result.offers.append(self._offer(query, fare, None, collected))
        else:
            min_stay = query.min_stay_days if query.min_stay_days is not None else 1
            max_stay = query.max_stay_days if query.max_stay_days is not None else DEFAULT_MAX_STAY
            if out_fares:
                first = min(out_fares) + timedelta(days=min_stay)
                last = max(out_fares) + timedelta(days=max_stay)
                in_fares: dict[date, Fare] = {}
                all_cached = out_cached
                for m in month_starts(first, last):
                    fares, cached = self._fares_for_month(query.destination, query.origin, m, query.currency)
                    in_fares.update(fares)
                    all_cached = all_cached and cached
                result.from_cache = all_cached
                weekdays = set(query.return_weekdays)
                for out in sorted(out_fares.values(), key=lambda f: f.day):
                    options = [
                        f for d, f in in_fares.items()
                        if min_stay <= (d - out.day).days <= max_stay
                        and (not weekdays or d.weekday() in weekdays)
                        and (query.return_date_from is None or d >= query.return_date_from)
                        and (query.return_date_to is None or d <= query.return_date_to)
                        and f.departure > (out.arrival or out.departure)
                        and f.currency == out.currency
                    ]
                    if options:  # keep the cheapest return for each departure day
                        best = min(options, key=lambda f: (f.price, f.day))
                        result.offers.append(self._offer(query, out, best, collected))
        result.requests_made = self.http.requests_made - before
        return result

    def health_check(self) -> HealthResult:
        try:
            self._fares_for_month("OPO", "KRK", date.today().replace(day=1), "EUR")
        except ProviderError as exc:
            status = "blocked" if "challenge" in str(exc) or "denied" in str(exc) else "failing"
            return HealthResult(False, status, str(exc))
        finally:
            self._memo.clear()
        return HealthResult(True, "ok", "fare finder reachable")

    # ------------------------------------------------------------------ building offers
    def _offer(self, q: SearchQuery, out: Fare, back: Fare | None, collected: datetime) -> FlightOfferData:
        total_pp = out.price + (back.price if back else 0)
        params = {
            "adults": q.passengers, "teens": 0, "children": 0, "infants": 0,
            "dateOut": out.day.isoformat(), "isConnectedFlight": "false",
            "isReturn": "true" if back else "false", "discount": 0,
            "originIata": q.origin, "destinationIata": q.destination,
        }
        if back:
            params["dateIn"] = back.day.isoformat()
        booking = f"{BOOKING_URL}?{httpx.QueryParams(params)}"
        return FlightOfferData(
            provider=self.name,
            airline="FR",
            flight_number=out.flight_number,
            origin=q.origin,
            destination=q.destination,
            trip_type=q.trip_type,
            departure_at=out.departure,
            arrival_at=out.arrival,
            return_departure_at=back.departure if back else None,
            return_arrival_at=back.arrival if back else None,
            price=round(total_pp * q.passengers, 2),
            currency=out.currency,
            passengers=q.passengers,
            stops=0,  # Ryanair fare finder lists point-to-point flights
            return_stops=0 if back else None,
            baggage_included=False,
            baggage_info=BAGGAGE_INFO,
            booking_url=booking,
            price_type=self.price_type,
            collected_at=collected,
        )


# ---------------------------------------------------------------------- parsing
def _price(obj: dict | None) -> tuple[float, str] | None:
    if not obj or obj.get("value") in (None, ""):
        return None
    return float(obj["value"]), str(obj.get("currencyCode") or "").upper()


def parse_cheapest_per_day(payload: Any, currency: str) -> dict[date, Fare]:
    try:
        fares = payload["outbound"]["fares"]
    except (KeyError, TypeError) as exc:
        raise ProviderRequestError("ryanair: unexpected cheapestPerDay response shape") from exc
    result: dict[date, Fare] = {}
    for item in fares or []:
        if item.get("unavailable") or item.get("soldOut"):
            continue
        price = _price(item.get("price"))
        if price is None or not item.get("departureDate"):
            continue
        dep = datetime.fromisoformat(item["departureDate"]).replace(tzinfo=None)
        arr = datetime.fromisoformat(item["arrivalDate"]).replace(tzinfo=None) if item.get("arrivalDate") else None
        result[dep.date()] = Fare(day=dep.date(), departure=dep, arrival=arr, price=price[0],
                                  currency=price[1] or currency.upper())
    return result


def parse_one_way_fares(payload: Any, origin: str, dest: str) -> dict[date, Fare]:
    try:
        items = payload["fares"] or []
    except (KeyError, TypeError) as exc:
        raise ProviderRequestError("ryanair: unexpected oneWayFares response shape") from exc
    result: dict[date, Fare] = {}
    for item in items:
        out = item.get("outbound") or {}
        if (out.get("departureAirport") or {}).get("iataCode") != origin:
            continue
        if (out.get("arrivalAirport") or {}).get("iataCode") != dest:
            continue
        price = _price(out.get("price"))
        if price is None:
            continue
        dep = datetime.fromisoformat(out["departureDate"]).replace(tzinfo=None)
        arr = datetime.fromisoformat(out["arrivalDate"]).replace(tzinfo=None) if out.get("arrivalDate") else None
        number = (out.get("flightNumber") or "").strip()
        number = number[2:].strip() if number[:2].upper() == "FR" else number
        fare = Fare(day=dep.date(), departure=dep, arrival=arr, price=price[0], currency=price[1],
                    flight_number=number or None)
        if dep.date() not in result or fare.price < result[dep.date()].price:
            result[dep.date()] = fare
    return result
