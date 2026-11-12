"""Turns a SearchConfiguration into provider queries and filters results
(date window, weekdays, stay length). Month-capable providers get one query per
month, which keeps API usage tiny (~2-3 requests per route per run)."""

from __future__ import annotations

from datetime import date, timedelta

from app.core.timeutils import month_starts
from app.models.db_models import SearchConfiguration
from app.models.schemas import FlightOfferData, SearchQuery, TripType


def date_window(cfg: SearchConfiguration, today: date) -> tuple[date, date]:
    start = cfg.date_from or today + timedelta(days=cfg.days_ahead_min or 0)
    end = cfg.date_to or today + timedelta(days=cfg.days_ahead_max or 60)
    start = max(start, today)  # never search the past
    return start, end


def plan_queries(
    cfg: SearchConfiguration, today: date, *, currency: str, month_search: bool
) -> list[SearchQuery]:
    start, end = date_window(cfg, today)
    if end < start:
        return []
    trip = TripType(cfg.trip_type)
    hints = {}
    if trip == TripType.RETURN:
        hints = dict(
            min_stay_days=cfg.min_stay_days,
            max_stay_days=cfg.max_stay_days,
            return_weekdays=sorted(SearchConfiguration.parse_weekdays(cfg.return_weekdays)),
        )
    if month_search:
        return [
            SearchQuery(
                origin=cfg.origin, destination=cfg.destination, trip_type=trip,
                departure_month=m, passengers=cfg.passengers, currency=currency, **hints,
            )
            for m in month_starts(start, end)
        ]
    # Per-day providers: only the allowed weekdays to limit request volume.
    weekdays = SearchConfiguration.parse_weekdays(cfg.departure_weekdays)
    queries = []
    d = start
    while d <= end:
        if not weekdays or d.weekday() in weekdays:
            queries.append(
                SearchQuery(
                    origin=cfg.origin, destination=cfg.destination, trip_type=trip,
                    departure_date=d, passengers=cfg.passengers, currency=currency, **hints,
                )
            )
        d += timedelta(days=1)
    return queries


def offer_matches(cfg: SearchConfiguration, offer: FlightOfferData, today: date) -> bool:
    start, end = date_window(cfg, today)
    dep = offer.departure_at.date()
    if not (start <= dep <= end):
        return False
    dep_days = SearchConfiguration.parse_weekdays(cfg.departure_weekdays)
    if dep_days and dep.weekday() not in dep_days:
        return False
    if offer.trip_type == TripType.RETURN:
        if offer.return_departure_at is None:
            return False
        ret = offer.return_departure_at.date()
        stay = (ret - dep).days
        if stay < 0:
            return False
        if cfg.min_stay_days is not None and stay < cfg.min_stay_days:
            return False
        if cfg.max_stay_days is not None and stay > cfg.max_stay_days:
            return False
        ret_days = SearchConfiguration.parse_weekdays(cfg.return_weekdays)
        if ret_days and ret.weekday() not in ret_days:
            return False
    return True
