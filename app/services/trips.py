"""Trips: specific out/back date pairs to watch (e.g. "Fri 29 Jan -> Mon 1 Feb").

A trip is stored as ordinary SearchConfiguration rows, one per origin x destination,
with date_from == date_to == the outbound date and min_stay == max_stay == the
number of nights. That keeps the search, storage and alert code unchanged and
needs no database migration. A configuration with exactly that shape is shown
as a trip; anything else (rolling windows, flexible stays) stays a "route".

Also estimates how many provider requests one search run will make, so the
dashboard can warn before a request cap is reached.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import AppConfig
from app.core.timeutils import month_starts
from app.models.db_models import FlightOffer, SearchConfiguration
from app.services.search_planner import date_window, plan_queries

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class Trip:
    outbound: date
    back: date
    config_ids: list[int] = field(default_factory=list)
    origins: list[str] = field(default_factory=list)
    destinations: list[str] = field(default_factory=list)
    enabled: bool = True
    max_price: float | None = None
    last_run_at: datetime | None = None

    @property
    def nights(self) -> int:
        return (self.back - self.outbound).days

    @property
    def key(self) -> str:
        return f"{self.outbound.isoformat()}_{self.back.isoformat()}"

    def is_past(self, today: date) -> bool:
        return self.outbound < today

    @property
    def label(self) -> str:
        return (f"{WEEKDAY_NAMES[self.outbound.weekday()]} {self.outbound:%d %b %Y} → "
                f"{WEEKDAY_NAMES[self.back.weekday()]} {self.back:%d %b %Y} ({self.nights} nights)")


def is_trip_config(c: SearchConfiguration) -> bool:
    return (
        c.trip_type == "return"
        and c.date_from is not None
        and c.date_from == c.date_to
        and c.min_stay_days is not None
        and c.min_stay_days == c.max_stay_days
        and c.min_stay_days >= 0
    )


def trip_dates(c: SearchConfiguration) -> tuple[date, date]:
    return c.date_from, c.date_from + timedelta(days=c.min_stay_days)


def repeat_weekly(outbound: date, back: date, weeks: int) -> list[tuple[date, date]]:
    """The same out/back weekdays for `weeks` consecutive weeks (weeks >= 1)."""
    return [(outbound + timedelta(weeks=i), back + timedelta(weeks=i)) for i in range(max(1, weeks))]


def validate_dates(outbound: date, back: date, today: date) -> None:
    if back < outbound:
        raise ValueError("the return date is before the outbound date")
    if outbound < today:
        raise ValueError(f"{outbound:%d %b %Y} is in the past")
    if (back - outbound).days > 60:
        raise ValueError("trips longer than 60 nights are not supported")


def new_trip_config(
    config: AppConfig,
    origin: str,
    destination: str,
    outbound: date,
    back: date,
    *,
    max_price: float | None = None,
    passengers: int | None = None,
    frequency_hours: float | None = None,
) -> SearchConfiguration:
    """An unsaved configuration for one airport pair of a trip (all fields set, so it
    can also be used for request estimates before saving)."""
    nights = (back - outbound).days
    return SearchConfiguration(
        origin=origin.upper(), destination=destination.upper(), trip_type="return",
        date_from=outbound, date_to=outbound, min_stay_days=nights, max_stay_days=nights,
        days_ahead_min=0, days_ahead_max=config.routes.days_ahead_max,
        departure_weekdays="", return_weekdays="",
        passengers=passengers or config.routes.passengers, max_price=max_price,
        frequency_hours=frequency_hours or config.routes.frequency_hours, enabled=True,
    )


def add_trip(
    session: Session,
    config: AppConfig,
    outbound: date,
    back: date,
    origins: list[str],
    destinations: list[str],
    *,
    max_price: float | None = None,
    passengers: int | None = None,
    frequency_hours: float | None = None,
    today: date | None = None,
) -> int:
    """Create the configurations for one trip. Returns how many were added
    (existing identical ones are re-enabled, not duplicated)."""
    validate_dates(outbound, back, today or date.today())
    if not origins or not destinations:
        raise ValueError("pick at least one departure and one destination airport")
    nights = (back - outbound).days
    existing = {
        (c.origin, c.destination): c
        for c in session.scalars(select(SearchConfiguration).where(
            SearchConfiguration.trip_type == "return",
            SearchConfiguration.date_from == outbound,
            SearchConfiguration.date_to == outbound,
            SearchConfiguration.min_stay_days == nights,
            SearchConfiguration.max_stay_days == nights,
        ))
    }
    added = 0
    for o in origins:
        for d in destinations:
            o, d = o.upper(), d.upper()
            if o == d:
                continue
            if (o, d) in existing:
                c = existing[(o, d)]
                c.enabled = True
                if max_price is not None:
                    c.max_price = max_price
                continue
            session.add(new_trip_config(config, o, d, outbound, back, max_price=max_price,
                                        passengers=passengers, frequency_hours=frequency_hours))
            added += 1
    session.commit()
    return added


def list_trips(session: Session) -> list[Trip]:
    groups: dict[tuple[date, date], Trip] = {}
    configs = session.scalars(select(SearchConfiguration).order_by(SearchConfiguration.date_from,
                                                                   SearchConfiguration.id))
    for c in configs:
        if not is_trip_config(c):
            continue
        out, back = trip_dates(c)
        t = groups.setdefault((out, back), Trip(outbound=out, back=back, enabled=False))
        t.config_ids.append(c.id)
        if c.origin not in t.origins:
            t.origins.append(c.origin)
        if c.destination not in t.destinations:
            t.destinations.append(c.destination)
        t.enabled = t.enabled or c.enabled
        if c.max_price is not None:
            t.max_price = c.max_price if t.max_price is None else min(t.max_price, c.max_price)
        if c.last_run_at and (t.last_run_at is None or c.last_run_at > t.last_run_at):
            t.last_run_at = c.last_run_at
    return sorted(groups.values(), key=lambda t: (t.outbound, t.back))


def _trip_configs(session: Session, trip: Trip) -> list[SearchConfiguration]:
    return [session.get(SearchConfiguration, i) for i in trip.config_ids]


def set_trip_enabled(session: Session, trip: Trip, enabled: bool) -> None:
    for c in _trip_configs(session, trip):
        c.enabled = enabled
    session.commit()


def set_trip_max_price(session: Session, trip: Trip, max_price: float | None) -> None:
    for c in _trip_configs(session, trip):
        c.max_price = max_price
    session.commit()


def delete_trip(session: Session, trip: Trip) -> None:
    """Removes the trip's search configurations. Collected offers and price
    history are kept (their link to the configuration is cleared)."""
    ids = set(trip.config_ids)
    for o in session.scalars(select(FlightOffer).where(FlightOffer.search_configuration_id.in_(ids))):
        o.search_configuration_id = None
    for c in _trip_configs(session, trip):
        session.delete(c)
    session.commit()


def cheapest_offers(session: Session, trip: Trip) -> list[FlightOffer]:
    """Stored return offers for exactly this trip's dates and airports, cheapest first."""
    start = datetime.combine(trip.outbound, datetime.min.time())
    q = select(FlightOffer).where(
        FlightOffer.trip_type == "return",
        FlightOffer.origin.in_(trip.origins),
        FlightOffer.destination.in_(trip.destinations),
        FlightOffer.departure_at >= start,
        FlightOffer.departure_at < start + timedelta(days=1),
    ).order_by(FlightOffer.price)
    return [o for o in session.scalars(q)
            if o.return_departure_at is not None and o.return_departure_at.date() == trip.back]


def disable_rolling_routes(session: Session) -> int:
    """Turns off every non-trip configuration (rolling 30-60 day windows)."""
    n = 0
    for c in session.scalars(select(SearchConfiguration).where(SearchConfiguration.enabled.is_(True))):
        if not is_trip_config(c):
            c.enabled = False
            n += 1
    session.commit()
    return n


# ---------------------------------------------------------------------- request estimate
@dataclass
class ProviderUsage:
    provider: str
    requests_per_run: int
    cap_per_run: int
    runs_per_day: float
    note: str = ""

    @property
    def requests_per_day(self) -> float:
        return self.requests_per_run * self.runs_per_day

    @property
    def over_cap(self) -> bool:
        return self.requests_per_run > self.cap_per_run

    @property
    def cap_share(self) -> float:
        return self.requests_per_run / self.cap_per_run if self.cap_per_run else 0.0


def _ryanair_requests(configs: list[SearchConfiguration], today: date) -> int:
    """Ryanair fetches one month of daily fares per direction and remembers it for the
    rest of the run, so cost = unique (from, to, month) combinations + 1 warm-up."""
    keys: set[tuple[str, str, date]] = set()
    for c in configs:
        start, end = date_window(c, today)
        if end < start:
            continue
        for m in month_starts(start, end):
            keys.add((c.origin, c.destination, m))
        if c.trip_type == "return":
            min_stay = c.min_stay_days if c.min_stay_days is not None else 1
            max_stay = c.max_stay_days if c.max_stay_days is not None else 30
            for m in month_starts(start + timedelta(days=min_stay), end + timedelta(days=max_stay)):
                keys.add((c.destination, c.origin, m))
    return len(keys) + (1 if keys else 0)


def estimate_usage(
    configs: list[SearchConfiguration],
    config: AppConfig,
    providers: dict[str, object],
    *,
    today: date | None = None,
) -> list[ProviderUsage]:
    """Worst-case requests for one run in which every given configuration is due
    (no cache hits). Rough but conservative."""
    today = today or date.today()
    configs = [c for c in configs if c.enabled]
    hours = [c.frequency_hours for c in configs if c.frequency_hours] or [config.routes.frequency_hours]
    runs_per_day = 24 / min(hours)
    usage = []
    for name, provider in providers.items():
        cap = config.provider(name).max_requests_per_run
        if name == "ryanair":
            n = _ryanair_requests(configs, today)
            note = "1 request = one month of fares for one direction, shared by every trip in that month"
        else:
            month = bool(getattr(provider, "supports_month_search", False))
            n = sum(len(plan_queries(c, today, currency=config.currency, month_search=month)) for c in configs)
            note = "1 request per route and month" if month else "1 request per route and day"
        usage.append(ProviderUsage(name, n, cap, runs_per_day, note))
    return usage

