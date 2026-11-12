"""Travelpayouts / Aviasales Data API provider.

Free after registering at travelpayouts.com (token in your profile).
IMPORTANT: this API returns CACHED prices that Aviasales users found in roughly
the last 2-7 days. They are INDICATIVE: a fare may have changed or sold out.
Coverage is limited to what Aviasales users have searched recently and to the
airlines/agencies Aviasales sells; it is NOT a complete check of all airlines.

Endpoint used: GET https://api.travelpayouts.com/aviasales/v3/prices_for_dates
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

import httpx

from app.core.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderRequestError,
)
from app.core.http import PoliteHttpClient, ResponseCache
from app.models.schemas import FlightOfferData, PriceType, SearchQuery, TripType
from app.providers.base import FlightProvider, HealthResult, ProviderResult

log = logging.getLogger(__name__)

API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
BOOKING_BASE = "https://www.aviasales.com"


class TravelpayoutsProvider(FlightProvider):
    name = "travelpayouts"
    price_type = PriceType.INDICATIVE
    coverage_note = (
        "Cached prices found by Aviasales users in the last days. Indicative only; "
        "not all airlines or dates are covered."
    )
    supports_month_search = True

    def __init__(
        self,
        token: str | None,
        *,
        min_interval_seconds: float = 1.5,
        max_retries: int = 3,
        result_limit: int = 1000,
        cache: ResponseCache | None = None,
        cache_ttl_seconds: float = 3600,
        transport: httpx.BaseTransport | None = None,
        sleep=None,
    ) -> None:
        self.token = token
        self.result_limit = result_limit
        kwargs: dict[str, Any] = {}
        if sleep is not None:
            kwargs["sleep"] = sleep
        self.http = PoliteHttpClient(
            self.name,
            min_interval_seconds=min_interval_seconds,
            max_retries=max_retries,
            cache=cache,
            cache_ttl_seconds=cache_ttl_seconds,
            transport=transport,
            **kwargs,
        )

    def is_configured(self) -> bool:
        return bool(self.token)

    def close(self) -> None:
        self.http.close()

    # -- request building -------------------------------------------------
    def build_params(self, query: SearchQuery) -> dict[str, Any]:
        if query.departure_date:
            departure = query.departure_date.isoformat()
        else:
            departure = query.departure_month.strftime("%Y-%m")
        params: dict[str, Any] = {
            "origin": query.origin,
            "destination": query.destination,
            "departure_at": departure,
            "one_way": "true" if query.trip_type == TripType.ONE_WAY else "false",
            "direct": "false",
            "currency": query.currency.lower(),
            "sorting": "price",
            "unique": "false",
            "limit": self.result_limit,
            "page": 1,
        }
        if query.trip_type == TripType.RETURN and query.return_date:
            params["return_at"] = query.return_date.isoformat()
        return params

    def _headers(self) -> dict[str, str]:
        # Token goes in a header so it never appears in URLs, logs or cache keys.
        return {"X-Access-Token": self.token or ""}

    # -- interface ----------------------------------------------------------
    def search_flights(self, query: SearchQuery) -> ProviderResult:
        if not self.is_configured():
            raise ProviderNotConfiguredError("TRAVELPAYOUTS_TOKEN is not set")
        before = self.http.requests_made
        payload, from_cache = self.http.get_json(API_URL, self.build_params(query), self._headers())
        result = self.parse_response(payload, query, collected_at=datetime.now(timezone.utc))
        result.from_cache = from_cache
        result.requests_made = self.http.requests_made - before
        return result

    def health_check(self) -> HealthResult:
        if not self.is_configured():
            return HealthResult(False, "not_configured", "TRAVELPAYOUTS_TOKEN is not set")
        query = SearchQuery(
            origin="OPO", destination="KRK", trip_type=TripType.ONE_WAY,
            departure_month=date.today().replace(day=1),
        )
        try:
            params = self.build_params(query) | {"limit": 1}
            self.http.get_json(API_URL, params, self._headers(), use_cache=False)
        except ProviderAuthError as exc:
            return HealthResult(False, "auth_error", str(exc))
        except ProviderError as exc:
            return HealthResult(False, "failing", str(exc))
        return HealthResult(True, "ok", "API reachable and token accepted")

    # -- normalization ------------------------------------------------------
    def parse_response(self, payload: Any, query: SearchQuery, *, collected_at: datetime) -> ProviderResult:
        if not isinstance(payload, dict):
            raise ProviderRequestError("travelpayouts: unexpected response shape")
        if payload.get("success") is False:
            error = payload.get("error") or "unknown error"
            if "token" in str(error).lower() or "unauthorized" in str(error).lower():
                raise ProviderAuthError(f"travelpayouts: {error}")
            raise ProviderRequestError(f"travelpayouts: {error}")
        currency = (payload.get("currency") or query.currency).upper()
        items = payload.get("data") or []
        if not isinstance(items, list):
            raise ProviderRequestError("travelpayouts: 'data' is not a list")

        result = ProviderResult()
        for item in items:
            try:
                offer = self._normalize(item, query, currency, collected_at)
            except (ValueError, TypeError, KeyError) as exc:
                result.skipped_items += 1
                log.debug("travelpayouts: skipped item %s (%s)", item, exc)
                continue
            if offer is not None:
                result.offers.append(offer)
        if result.skipped_items:
            result.warnings.append(f"{result.skipped_items} items could not be normalized")
        return result

    def _normalize(
        self, item: dict[str, Any], query: SearchQuery, currency: str, collected_at: datetime
    ) -> FlightOfferData | None:
        origin = (item.get("origin_airport") or item.get("origin") or "").upper()
        destination = (item.get("destination_airport") or item.get("destination") or "").upper()
        # The API may expand city codes to other airports; keep only what we asked for.
        if origin != query.origin or destination != query.destination:
            return None

        departure = datetime.fromisoformat(item["departure_at"])
        return_at = item.get("return_at")
        is_return = query.trip_type == TripType.RETURN
        if is_return and not return_at:
            return None  # asked for return trips; ignore one-way rows
        return_departure = datetime.fromisoformat(return_at) if (is_return and return_at) else None

        price_pp = float(item["price"])  # price for one adult
        link = item.get("link")
        booking_url = f"{BOOKING_BASE}{link}" if link and link.startswith("/") else link

        duration_to = item.get("duration_to") or (item.get("duration") if not is_return else None)
        flight_number = item.get("flight_number")
        return FlightOfferData(
            provider=self.name,
            airline=item.get("airline") or None,
            flight_number=str(flight_number) if flight_number not in (None, "") else None,
            origin=origin,
            destination=destination,
            trip_type=query.trip_type,
            departure_at=departure,
            return_departure_at=return_departure,
            price=round(price_pp * query.passengers, 2),
            currency=currency,
            passengers=query.passengers,
            stops=_int_or_none(item.get("transfers")),
            return_stops=_int_or_none(item.get("return_transfers")) if is_return else None,
            duration_minutes=_int_or_none(duration_to),
            return_duration_minutes=_int_or_none(item.get("duration_back")) if is_return else None,
            baggage_included=None,  # not provided by this API
            baggage_info="Not provided by source — check before booking",
            booking_url=booking_url,
            price_type=self.price_type,
            collected_at=collected_at,
        )


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)
