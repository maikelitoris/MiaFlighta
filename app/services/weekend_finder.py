"""Weekend finder: for every weekend in a window, where can all travellers meet for the
lowest combined price? Also prices one traveller visiting the other.

One Ryanair "everywhere" request per departure airport per weekend returns the cheapest
return trip to every destination, so 12 weekends x 3 airports = 36 requests. Only
fares actually returned by Ryanair are used; destinations without a fare for a
traveller are simply not combined.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import AppConfig, WeekendFinderConfig
from app.core.errors import ProviderBlockedError, ProviderError
from app.core.timeutils import utcnow
from app.models.db_models import Airport, WeekendFare
from app.notifications.telegram import TELEGRAM_MAX_LEN, money
from app.services import provider_health, settings_service

log = logging.getLogger(__name__)

LAST_RUN_SETTING = "weekend_finder_last_run"
WEEKDAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------------------------------------------------------------------- weekends
def weekends(cfg: WeekendFinderConfig, today: date) -> list[tuple[date, date]]:
    """The window's weekends that are still in the future (outbound after today)."""
    start = cfg.start or today + timedelta(days=1)
    first = start + timedelta(days=(cfg.outbound_weekday - start.weekday()) % 7)
    days = [first + timedelta(weeks=i) for i in range(cfg.weeks)]
    return [(d, d + timedelta(days=cfg.nights)) for d in days if d > today]


# ---------------------------------------------------------------------- searching
@dataclass
class FinderRun:
    weekends: int = 0
    requests: int = 0
    fares: int = 0
    errors: list[str] = field(default_factory=list)
    skipped_reason: str | None = None


def is_due(session: Session, hours: float, now: datetime) -> bool:
    last = settings_service.get_setting(session, LAST_RUN_SETTING)
    return last is None or datetime.fromisoformat(last) + timedelta(hours=hours) <= now


def run_finder(session: Session, config: AppConfig, provider, *, today: date | None = None,
               now: datetime | None = None) -> FinderRun:
    """Searches every weekend x departure airport once and stores the fares."""
    cfg = config.weekend_finder
    today = today or date.today()
    now = now or utcnow()
    result = FinderRun()
    if not cfg.enabled or not cfg.travelers:
        result.skipped_reason = "weekend finder is off"
        return result
    if provider is None:
        result.skipped_reason = "the Ryanair provider is not enabled"
        return result
    if provider_health.is_paused(session, provider.name):
        result.skipped_reason = "Ryanair is paused after a block; see Settings"
        return result
    origins = list(dict.fromkeys(o.upper() for t in cfg.travelers for o in t.origins))
    excluded = {d.upper() for d in cfg.exclude_destinations}
    budget = cfg.max_requests_per_run
    for out_day, back_day in weekends(cfg, today):
        result.weekends += 1
        for origin in origins:
            if budget <= 0:
                result.errors.append("request budget for this run used up; later weekends skipped")
                break
            before = provider.http.requests_made
            try:
                fares = provider.round_trip_fares(origin, out_day, back_day, config.currency)
            except ProviderBlockedError as exc:
                provider_health.record_failure(session, provider.name, str(exc), status="blocked",
                                               pause_hours=config.scheduler.blocked_pause_hours)
                result.errors.append(str(exc))
                session.commit()
                return result
            except ProviderError as exc:
                result.errors.append(f"{origin} {out_day}: {exc}")
                continue
            finally:
                used = provider.http.requests_made - before
                budget -= used
                result.requests += used
            for f in fares:
                if f.destination in excluded:
                    continue
                session.add(WeekendFare(
                    observed_at=now, out_day=out_day, back_day=back_day, origin=f.origin,
                    destination=f.destination, city=f.destination_city, country=f.destination_country,
                    price=f.price, currency=f.currency, outbound_price=f.outbound_price,
                    inbound_price=f.inbound_price, outbound_departure=f.outbound_departure,
                    outbound_arrival=f.outbound_arrival, inbound_departure=f.inbound_departure,
                    inbound_arrival=f.inbound_arrival, outbound_flight=f.outbound_flight,
                    inbound_flight=f.inbound_flight))
                result.fares += 1
        session.commit()
    settings_service.set_setting(session, LAST_RUN_SETTING, now.isoformat())
    log.info("Weekend finder: %d weekends, %d requests, %d fares, %d errors",
             result.weekends, result.requests, result.fares, len(result.errors))
    return result


# ---------------------------------------------------------------------- combining
@dataclass
class Leg:
    traveler: str
    fare: WeekendFare


@dataclass
class Meetup:
    out_day: date
    back_day: date
    destination: str
    city: str
    country: str
    legs: list[Leg]
    total: float
    currency: str
    previous_low: float | None = None  # lowest total seen in earlier runs


@dataclass
class Visit:
    out_day: date
    back_day: date
    visitor: str
    host: str
    fare: WeekendFare


def latest_snapshot(session: Session) -> list[WeekendFare]:
    latest = session.scalar(select(func.max(WeekendFare.observed_at)))
    if latest is None:
        return []
    return list(session.scalars(select(WeekendFare).where(WeekendFare.observed_at == latest)))


def _best_per_traveler(rows: list[WeekendFare], cfg: WeekendFinderConfig):
    """{(out_day, traveler): {destination: cheapest fare from any of their airports}}"""
    best: dict[tuple[date, str], dict[str, WeekendFare]] = {}
    for t in cfg.travelers:
        mine = {o.upper() for o in t.origins}
        for r in rows:
            if r.origin not in mine:
                continue
            slot = best.setdefault((r.out_day, t.name), {})
            if r.destination not in slot or r.price < slot[r.destination].price:
                slot[r.destination] = r
    return best


def combine(session: Session, config: AppConfig, rows: list[WeekendFare] | None = None
            ) -> tuple[list[Meetup], list[Visit]]:
    cfg = config.weekend_finder
    rows = latest_snapshot(session) if rows is None else rows
    if not rows or not cfg.travelers:
        return [], []
    best = _best_per_traveler(rows, cfg)
    home = {o.upper() for t in cfg.travelers for o in t.origins}
    days = sorted({(r.out_day, r.back_day) for r in rows})
    meetups: list[Meetup] = []
    visits: list[Visit] = []
    for out_day, back_day in days:
        per = [(t.name, best.get((out_day, t.name), {})) for t in cfg.travelers]
        common = set.intersection(*(set(d) for _, d in per)) - home if per else set()
        for dest in common:
            legs = [Leg(name, d[dest]) for name, d in per]
            if len({leg.fare.currency for leg in legs}) != 1:
                continue
            f0 = legs[0].fare
            meetups.append(Meetup(out_day, back_day, dest, f0.city, f0.country, legs,
                                  round(sum(leg.fare.price for leg in legs), 2), f0.currency))
        if len(cfg.travelers) == 2:  # one visits the other
            for a, b in (cfg.travelers, cfg.travelers[::-1]):
                to_b = [f for dest, f in best.get((out_day, a.name), {}).items()
                        if dest in {o.upper() for o in b.origins}]
                if to_b:
                    visits.append(Visit(out_day, back_day, a.name, b.name, min(to_b, key=lambda f: f.price)))
    meetups.sort(key=lambda m: (m.total, m.out_day))
    _attach_previous_lows(session, meetups, rows, cfg)
    return meetups, visits


def _attach_previous_lows(session: Session, meetups: list[Meetup], rows: list[WeekendFare],
                          cfg: WeekendFinderConfig) -> None:
    """Lowest combined price for the same weekend and destination in earlier runs."""
    if not rows or not meetups:
        return
    latest = rows[0].observed_at
    older = list(session.scalars(select(WeekendFare).where(WeekendFare.observed_at < latest)))
    if not older:
        return
    by_run: dict[datetime, list[WeekendFare]] = {}
    for r in older:
        by_run.setdefault(r.observed_at, []).append(r)
    lows: dict[tuple[date, str], float] = {}
    for run_rows in by_run.values():
        best = _best_per_traveler(run_rows, cfg)
        days = {r.out_day for r in run_rows}
        for out_day in days:
            per = [best.get((out_day, t.name), {}) for t in cfg.travelers]
            for dest in set.intersection(*(set(d) for d in per)):
                total = sum(d[dest].price for d in per)
                key = (out_day, dest)
                lows[key] = min(lows.get(key, total), total)
    for m in meetups:
        m.previous_low = lows.get((m.out_day, m.destination))


# ---------------------------------------------------------------------- messages
def _e(text: str) -> str:
    return html.escape(text)


def _weekend(out_day: date, back_day: date) -> str:
    if out_day.month == back_day.month:
        return f"{WEEKDAY[out_day.weekday()]} {out_day.day}–{WEEKDAY[back_day.weekday()]} {back_day.day} {back_day:%b}"
    return (f"{WEEKDAY[out_day.weekday()]} {out_day.day} {out_day:%b}–"
            f"{WEEKDAY[back_day.weekday()]} {back_day.day} {back_day:%b}")


def _short(out_day: date, back_day: date) -> str:
    if out_day.month == back_day.month:
        return f"{out_day.day}–{back_day.day} {back_day:%b}"
    return f"{out_day.day} {out_day:%b}–{back_day.day} {back_day:%b}"


def _who(name: str) -> str:
    return "You" if name.lower() in ("you", "me") else name


def _amount(v: float, cur: str) -> str:
    return money(v, cur)


def _trend(m: Meetup) -> str:
    if m.previous_low is None:
        return ""
    if m.total < m.previous_low - 0.01:
        return f" · 🔻 new low (was {_amount(m.previous_low, m.currency)})"
    return ""


def finder_messages(session: Session, config: AppConfig, provider=None) -> list[str]:
    """Two messages: the best picks overall, then a weekend-by-weekend overview."""
    cfg = config.weekend_finder
    meetups, visits = combine(session, config)
    airports = {a.iata: a for a in session.scalars(select(Airport))}
    city = lambda code: airports[code].city if code in airports else code  # noqa: E731
    rows = latest_snapshot(session)
    if not rows:
        return []
    window = sorted({(r.out_day, r.back_day) for r in rows})
    names = " & ".join(_who(t.name) for t in cfg.travelers)

    # ---- message 1: top picks
    if meetups:
        b = meetups[0]
        preview = (f"🌍 Best weekend: {_short(b.out_day, b.back_day)} {b.city} · "
                   f"{_amount(b.total, b.currency)} together")
    else:
        preview = "🌍 Weekend finder: no shared destination found this time"
    lines = [f"<b>{_e(preview)}</b>",
             f"{names} · {len(window)} weekends {_short(*window[0])} → {_short(*window[-1])} · Ryanair only", ""]
    for i, m in enumerate(meetups[: cfg.top], 1):
        lines.append(f"<b>{i}. {_e(m.city)}</b> ({_e(m.country)}) · {_weekend(m.out_day, m.back_day)} · "
                     f"<b>{_amount(m.total, m.currency)}</b>{_trend(m)}")
        for leg in m.legs:
            f = leg.fare
            times = f"{f.outbound_departure:%a %H:%M} → back {f.inbound_departure:%a %H:%M}"
            link = ""
            if provider is not None and i <= 3:
                link = f' · <a href="{_e(provider.booking_url(f.origin, f.destination, m.out_day, m.back_day))}">book</a>'
            lines.append(f"   {_e(_who(leg.traveler))} {_amount(f.price, f.currency)} from {_e(city(f.origin))} "
                         f"({times}){link}")
        lines.append("")
    if visits:
        cheapest = {}
        for v in visits:
            key = (v.visitor, v.host)
            if key not in cheapest or v.fare.price < cheapest[key].fare.price:
                cheapest[key] = v
        lines.append("<b>Visiting each other</b> (cheapest weekend)")
        for v in cheapest.values():
            lines.append(f"   {_e(_who(v.visitor))} → {_e(city(v.fare.destination))}: "
                         f"{_amount(v.fare.price, v.fare.currency)} · {_weekend(v.out_day, v.back_day)}")
    msg1 = "\n".join(lines).strip()

    # ---- message 2: weekend by weekend
    lines = [f"<b>📅 Weekend by weekend · {_short(*window[0])} → {_short(*window[-1])}</b>", ""]
    for out_day, back_day in window:
        here = [m for m in meetups if m.out_day == out_day][:3]
        vis = [v for v in visits if v.out_day == out_day]
        parts = [f"{_e(m.city)} {_amount(m.total, m.currency)}" for m in here] or ["no shared destination"]
        line = f"<b>{_short(out_day, back_day)}</b>: " + " · ".join(parts)
        if vis:
            line += "\n   " + " · ".join(f"{_e(_who(v.visitor))}→{_e(city(v.fare.destination))} "
                                       f"{_amount(v.fare.price, v.fare.currency)}" for v in vis)
        lines.append(line)
    lines += ["", "<i>Prices per person, return, Ryanair basic fare (small bag only), from Ryanair's fare finder. "
                  "Together = sum for both. Confirm on ryanair.com.</i>"]
    msg2 = "\n".join(lines)
    return [m[:TELEGRAM_MAX_LEN] for m in (msg1, msg2)]
