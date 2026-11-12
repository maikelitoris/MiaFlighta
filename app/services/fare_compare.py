"""Is this trip price good? Answered only with fares that were actually collected.

Ryanair publishes no historical prices, so two honest comparisons are used:

1. **Other dates, right now.** One fare-calendar request returns the cheapest fare for
   every day of a month. A return trip of N nights is compared with every other trip
   of N nights that can be built from the same calendars (same airports, same search).
2. **The same trip over time.** Every search appends an observation, so after a few
   days the app can say whether the price is rising or falling.

It also splits a return trip into its going and coming-back flights.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.db_models import DailyFare, FlightOffer, PriceHistory

SNAPSHOT_HOURS = 6  # fares observed this close to the latest observation count as one snapshot


@dataclass
class Leg:
    origin: str
    destination: str
    day: date
    price: float
    currency: str
    departure_at: datetime | None
    arrival_at: datetime | None
    flight_number: str | None


@dataclass
class Alternative:
    outbound: date
    back: date
    price: float


@dataclass
class TripComparison:
    going: Leg | None
    coming_back: Leg | None
    total: float
    currency: str
    nights: int
    # other dates (same snapshot)
    same_length_count: int = 0  # comparable trips (including this one)
    cheaper_count: int = 0
    median_price: float | None = None
    cheapest: list[Alternative] = field(default_factory=list)
    same_weekdays: list[Alternative] = field(default_factory=list)
    window: tuple[date, date] | None = None
    # over time
    observations: int = 0
    observed_days: int = 0
    lowest_seen: float | None = None
    highest_seen: float | None = None
    first_seen_price: float | None = None

    @property
    def rank_text(self) -> str | None:
        if self.same_length_count < 5 or self.median_price is None:
            return None
        n = self.same_length_count - 1
        if self.cheaper_count == 0:
            head = f"Cheapest of {self.same_length_count} {self.nights}-night trips"
        else:
            head = f"{self.cheaper_count} of {n} other {self.nights}-night trips are cheaper"
        return f"{head} (median {self.median_price:.2f} {self.currency})"

    @property
    def verdict(self) -> str:
        """Short label: good / average / expensive, or not enough data."""
        if self.same_length_count < 5 or self.median_price is None:
            return "not enough dates to compare"
        share = self.cheaper_count / max(1, self.same_length_count - 1)
        if share <= 0.25:
            return "good price for these routes"
        if share <= 0.6:
            return "average price"
        return "expensive compared with other dates"


def _snapshot(session: Session, origin: str, dest: str, provider: str | None = None) -> dict[date, DailyFare]:
    """Latest fare per day from the most recent search of this direction."""
    q = select(func.max(DailyFare.observed_at)).where(DailyFare.origin == origin, DailyFare.destination == dest)
    if provider:
        q = q.where(DailyFare.provider == provider)
    latest = session.scalar(q)
    if latest is None:
        return {}
    rows = select(DailyFare).where(
        DailyFare.origin == origin, DailyFare.destination == dest,
        DailyFare.observed_at >= latest - timedelta(hours=SNAPSHOT_HOURS),
    ).order_by(DailyFare.observed_at)
    if provider:
        rows = rows.where(DailyFare.provider == provider)
    out: dict[date, DailyFare] = {}
    for r in session.scalars(rows):
        out[r.day] = r  # later observation wins
    return out


def _leg(f: DailyFare | None) -> Leg | None:
    if f is None:
        return None
    return Leg(f.origin, f.destination, f.day, f.price, f.currency, f.departure_at, f.arrival_at, f.flight_number)


def calendar(session: Session, origin: str, dest: str) -> dict[date, DailyFare]:
    """Latest known fare per day for one direction (for charts)."""
    return _snapshot(session, origin, dest)


def compare_offer(session: Session, offer: FlightOffer) -> TripComparison | None:
    if offer.trip_type != "return" or offer.return_departure_at is None:
        return None
    out_day, back_day = offer.departure_at.date(), offer.return_departure_at.date()
    nights = (back_day - out_day).days
    outs = _snapshot(session, offer.origin, offer.destination, offer.provider)
    backs = _snapshot(session, offer.destination, offer.origin, offer.provider)
    pax = max(1, offer.passengers or 1)
    cmp = TripComparison(going=_leg(outs.get(out_day)), coming_back=_leg(backs.get(back_day)),
                         total=offer.price, currency=offer.currency, nights=nights)

    # 1. other dates with the same trip length, from the same calendars
    combos = []
    for d, f in outs.items():
        b = backs.get(d + timedelta(days=nights))
        if b is not None and b.currency == f.currency == offer.currency:
            combos.append(Alternative(d, b.day, round((f.price + b.price) * pax, 2)))
    if combos:
        mine = next((c.price for c in combos if c.outbound == out_day), offer.price)
        cmp.same_length_count = len(combos)
        cmp.cheaper_count = sum(c.price < mine - 0.005 for c in combos)
        cmp.median_price = round(median(c.price for c in combos), 2)
        others = sorted((c for c in combos if c.outbound != out_day), key=lambda c: (c.price, c.outbound))
        cmp.cheapest = [c for c in others if c.price < mine - 0.005][:3]
        cmp.same_weekdays = [c for c in others if c.outbound.weekday() == out_day.weekday()][:3]
        cmp.window = (min(c.outbound for c in combos), max(c.outbound for c in combos))

    # 2. the same itinerary over time
    hist = list(session.scalars(select(PriceHistory).where(PriceHistory.offer_id == offer.id)
                                .order_by(PriceHistory.observed_at)))
    if hist:
        cmp.observations = len(hist)
        cmp.observed_days = len({h.observed_at.date() for h in hist})
        cmp.lowest_seen = min(h.observed_price for h in hist)
        cmp.highest_seen = max(h.observed_price for h in hist)
        cmp.first_seen_price = hist[0].observed_price
    return cmp


def history_text(cmp: TripComparison) -> str:
    if cmp.observed_days < 2:
        return "Price history: only collected since today, so no trend yet"
    change = cmp.total - (cmp.first_seen_price or cmp.total)
    trend = "unchanged" if abs(change) < 0.01 else (f"down {abs(change):.2f}" if change < 0 else f"up {change:.2f}")
    return (f"Price history ({cmp.observed_days} days): low {cmp.lowest_seen:.2f}, high {cmp.highest_seen:.2f}, "
            f"{trend} {cmp.currency} since first seen")
