"""Rule-based deal scoring with simple statistics (no ML).

Rules (all configurable in config.yaml -> scoring):
* points for low effective price per passenger (price + Katowice-style transfer costs);
* points when >= N% below the median of COMPARABLE journeys (same route, trip type,
  currency, passenger count, baggage status and — for returns — similar stay length);
  only when enough comparable history exists, otherwise no history points are given
  and the message says so;
* points for direct flights, convenient departure times, included checked baggage.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import AppConfig
from app.core.timeutils import utcnow
from app.models.db_models import FlightOffer, PriceHistory

CATEGORY_RANK = {"normal": 0, "good": 1, "excellent": 2}


@dataclass
class Baseline:
    median: float
    average: float
    lowest: float
    samples: int  # number of comparable offers
    lookback_days: int
    observation_days: int = 0


@dataclass
class DealAssessment:
    score: int
    category: str  # excellent | good | normal
    effective_price_pp: float  # per passenger incl. ground transfer
    transfer_cost_pp: float
    baseline: Baseline | None
    comparable_samples: int
    discount_pct: float | None  # vs baseline median, positive = cheaper
    reasons: list[str] = field(default_factory=list)


def _stay(dep: datetime, ret: datetime | None) -> int | None:
    return (ret.date() - dep.date()).days if ret else None


def comparable_baseline(
    session: Session, offer: FlightOffer, config: AppConfig, now: datetime | None = None
) -> tuple[Baseline | None, int]:
    sc = config.scoring
    now = now or utcnow()
    since = now - timedelta(days=sc.history_lookback_days)
    rows = session.execute(
        select(
            FlightOffer.id,
            FlightOffer.departure_at,
            FlightOffer.return_departure_at,
            FlightOffer.baggage_included,
            PriceHistory.observed_price,
            PriceHistory.observed_at,
        )
        .join(PriceHistory, PriceHistory.offer_id == FlightOffer.id)
        .where(
            FlightOffer.origin == offer.origin,
            FlightOffer.destination == offer.destination,
            FlightOffer.trip_type == offer.trip_type,
            FlightOffer.passengers == offer.passengers,
            FlightOffer.id != offer.id,
            PriceHistory.currency == offer.currency,
            PriceHistory.observed_at >= since,
        )
    ).all()
    my_stay = _stay(offer.departure_at, offer.return_departure_at)
    per_offer: dict[int, list[float]] = defaultdict(list)
    days: set = set()
    for oid, dep, ret, bag, price, observed_at in rows:
        if bag != offer.baggage_included:  # compare like with like (None = unknown)
            continue
        if my_stay is not None:
            stay = _stay(dep, ret)
            if stay is None or abs(stay - my_stay) > sc.stay_tolerance_days:
                continue
        per_offer[oid].append(price)
        days.add(observed_at.date())
    samples = len(per_offer)
    # A real history needs observations over time, not just many fares from one search.
    if samples < sc.history_min_samples or len(days) < sc.history_min_days:
        return None, samples
    typical = [statistics.median(prices) for prices in per_offer.values()]
    return (
        Baseline(
            median=round(statistics.median(typical), 2),
            average=round(statistics.fmean(typical), 2),
            lowest=round(min(min(p) for p in per_offer.values()), 2),
            samples=samples,
            lookback_days=sc.history_lookback_days,
            observation_days=len(days),
        ),
        samples,
    )


def assess(
    offer: FlightOffer, config: AppConfig, baseline: Baseline | None, comparable_samples: int = 0
) -> DealAssessment:
    sc = config.scoring
    reasons: list[str] = []
    score = 0
    legs = 2 if offer.trip_type == "return" else 1
    transfer_pp = config.transfer_cost(offer.destination) * legs
    effective_pp = offer.price_per_passenger + transfer_pp

    for t in sorted(sc.price_points.get(offer.trip_type, []), key=lambda t: t.below):
        if effective_pp < t.below:
            score += t.points
            reasons.append(f"price below {t.below:.0f} {offer.currency} (+{t.points})")
            break

    discount = None
    if baseline is not None and baseline.median > 0:
        discount = round((baseline.median - offer.price) / baseline.median * 100, 1)
        if discount >= sc.history_discount_pct:
            score += sc.history_points
            reasons.append(f"{discount:.0f}% below comparable median (+{sc.history_points})")

    if offer.is_direct:
        score += sc.direct_points
        reasons.append(f"direct (+{sc.direct_points})")

    cd = sc.convenient_departure
    times = [offer.departure_at.time()]
    if offer.return_departure_at:
        times.append(offer.return_departure_at.time())
    if all(cd.earliest <= t <= cd.latest for t in times):
        score += cd.points
        reasons.append(f"convenient departure time (+{cd.points})")

    if offer.baggage_included:
        score += sc.baggage_points
        reasons.append(f"checked baggage included (+{sc.baggage_points})")

    score = min(score, 100)
    if score >= sc.excellent_min:
        category = "excellent"
    elif score >= sc.good_min:
        category = "good"
    else:
        category = "normal"
    return DealAssessment(
        score=score,
        category=category,
        effective_price_pp=round(effective_pp, 2),
        transfer_cost_pp=transfer_pp,
        baseline=baseline,
        comparable_samples=comparable_samples,
        discount_pct=discount,
        reasons=reasons,
    )


def assess_offer(session: Session, offer: FlightOffer, config: AppConfig) -> DealAssessment:
    baseline, samples = comparable_baseline(session, offer, config)
    return assess(offer, config, baseline, samples)
