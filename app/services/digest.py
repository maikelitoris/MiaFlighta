"""Regular Telegram messages that are NOT deal alerts:

* a price digest after every scheduled search (default every 12 h): the current best
  price for every watched trip, even when it is not a good deal;
* a weekly report: the lowest price seen per trip over the last N days, the current
  price, and the single best pick.

Both only use prices that were actually collected. When nothing was found they say so.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.airlines import airline_name
from app.core.config import AppConfig
from app.core.timeutils import utcnow
from app.models.db_models import Airport, FlightOffer, PriceHistory, ProviderState, SearchRun
from app.notifications.telegram import TELEGRAM_MAX_LEN, Notifier, SendResult, money
from app.services import settings_service
from app.services.fare_compare import compare_offer
from app.services.fare_compare import history_text as fare_history_text
from app.services.trips import Trip, cheapest_offers, list_trips

log = logging.getLogger(__name__)

DIGEST_ENABLED = "digest_every_run"
WEEKLY_ENABLED = "weekly_report"
WEEKLY_LAST_SENT = "weekly_report_last_slot"
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ---------------------------------------------------------------------- settings
def digest_enabled(session: Session, config: AppConfig) -> bool:
    return bool(settings_service.get_setting(session, DIGEST_ENABLED, config.reports.digest_every_run))


def weekly_enabled(session: Session, config: AppConfig) -> bool:
    return bool(settings_service.get_setting(session, WEEKLY_ENABLED, config.reports.weekly_report))


# ---------------------------------------------------------------------- helpers
def _e(text: str) -> str:
    return html.escape(text)


def _city(airports: dict[str, Airport], code: str) -> str:
    a = airports.get(code)
    return a.city if a else code


def _route(airports: dict[str, Airport], o: FlightOffer) -> str:
    return f"{_city(airports, o.origin)} → {_city(airports, o.destination)}"


def _day(d: date) -> str:
    return d.strftime("%a %d %b")


def _trip_title(t: Trip) -> str:
    return f"<b>{_day(t.outbound)} → {_day(t.back)}</b> ({t.nights} night{'s' if t.nights != 1 else ''})"


def _previous_price(o: FlightOffer) -> float | None:
    hist = sorted(o.history, key=lambda h: h.observed_at)
    return hist[-2].observed_price if len(hist) >= 2 else None


def _change(now: float, before: float | None, currency: str) -> str:
    if before is None:
        return "first price"
    diff = now - before
    if abs(diff) < 0.01:
        return "unchanged"
    arrow = "🔻" if diff < 0 else "🔺"
    return f"{arrow} {money(abs(diff), currency)} vs last check"


def _observations(session: Session, offers: list[FlightOffer], since: datetime | None = None) -> list[PriceHistory]:
    if not offers:
        return []
    q = select(PriceHistory).where(PriceHistory.offer_id.in_([o.id for o in offers]))
    if since is not None:
        q = q.where(PriceHistory.observed_at >= since)
    return list(session.scalars(q))


def _current(offers: list[FlightOffer], config: AppConfig, now: datetime) -> list[FlightOffer]:
    since = now - timedelta(hours=config.dashboard.current_offer_hours)
    return [o for o in offers if o.collected_at >= since]


def _provider_warnings(session: Session) -> list[str]:
    out = []
    for p in session.scalars(select(ProviderState)):
        if p.paused_until and p.paused_until > utcnow():
            out.append(f"⚠️ {p.provider} paused until {p.paused_until:%d %b %H:%M} UTC: {_e(p.last_error or '')}")
        elif p.status in ("failing", "blocked", "auth_error"):
            out.append(f"⚠️ {p.provider}: {p.status} ({_e((p.last_error or '')[:120])})")
    return out


def search_every_hours(session: Session, config: AppConfig) -> float:
    """Shortest search interval among enabled configurations."""
    hours = [c.frequency_hours for c in settings_service.enabled_configs(session) if c.frequency_hours]
    return min(hours) if hours else config.routes.frequency_hours


def _upcoming(session: Session, today: date) -> list[Trip]:
    return [t for t in list_trips(session) if t.enabled and not t.is_past(today)]


def _leg_line(label: str, leg, airports) -> str:
    times = f"{leg.departure_at:%H:%M}" if leg.departure_at else "?"
    if leg.arrival_at:
        times += f"→{leg.arrival_at:%H:%M}"
    return (f"   {label} {_day(leg.day)} {times} {_e(_city(airports, leg.origin))}→"
            f"{_e(_city(airports, leg.destination))} · {money(leg.price, leg.currency)}")


def leg_lines(session: Session, o: FlightOffer, airports: dict[str, Airport]) -> list[str]:
    """Going + coming-back flights of a return offer, plus how it compares."""
    cmp = compare_offer(session, o)
    lines = []
    if cmp and cmp.going and cmp.coming_back:
        lines.append(_leg_line("🛫 Going", cmp.going, airports))
        lines.append(_leg_line("🛬 Back ", cmp.coming_back, airports))
    else:
        times = f"   out {o.departure_at:%a %d %b %H:%M}"
        if o.return_departure_at:
            times += f" · back {o.return_departure_at:%a %d %b %H:%M}"
        lines.append(times)
    if o.booking_url:
        lines.append(f'   <a href="{_e(o.booking_url)}">Open booking page (both flights)</a>')
    if cmp:
        if cmp.rank_text:
            lines.append(f"📊 <b>{_e(cmp.verdict.capitalize())}</b>: {_e(cmp.rank_text)}")
            if cmp.cheapest:
                c = cmp.cheapest[0]
                lines.append(f"   Cheaper dates: {_day(c.outbound)} → {_day(c.back)} {money(c.price, o.currency)}")
        else:
            lines.append("📊 Not enough other dates in the fare calendar to compare yet")
        lines.append(f"<i>{_e(fare_history_text(cmp))}</i>")
    return lines


# ---------------------------------------------------------------------- 12-hour digest
def digest_blocks(session: Session, config: AppConfig, *, now: datetime | None = None) -> list[str]:
    """Message parts: a header, one block per watched trip, a footer."""
    now = now or utcnow()
    airports = {a.iata: a for a in session.scalars(select(Airport))}
    trips = _upcoming(session, now.date())
    hours = search_every_hours(session, config)
    header = (f"✈️ <b>Flight Radar update</b> · {now:%a %d %b %H:%M} UTC\n"
              "<i>Latest prices for your trips — an update, not a deal alert.</i>")
    blocks = [header]
    if not trips:
        blocks.append("No upcoming trips are being watched. Add dates on the dashboard's Trips page.")
    for t in trips:
        offers = cheapest_offers(session, t)
        current = _current(offers, config, now)
        lines = [_trip_title(t)]
        if not current:
            lines.append("No fare found in the latest search for these exact dates.")
        for i, o in enumerate(current[:4]):
            medal = "🥇 " if i == 0 else "   "
            line = (f"{medal}<b>{money(o.price, o.currency)}</b> {_e(_route(airports, o))} "
                    f"({_e(airline_name(o.airline))}) · {_change(o.price, _previous_price(o), o.currency)}")
            if i == 0:
                line += "\n" + "\n".join(leg_lines(session, o, airports))
            lines.append(line)
        missing = [f"{_city(airports, a)}→{_city(airports, b)}" for a in t.origins for b in t.destinations
                   if not any(o.origin == a and o.destination == b for o in current)]
        if current and missing:
            lines.append(f"<i>No fare: {_e(', '.join(missing))}</i>")
        hist = _observations(session, offers)
        if hist:
            low = min(hist, key=lambda h: h.observed_price)
            lines.append(f"Lowest seen so far: {money(low.observed_price, low.currency)} ({low.observed_at:%d %b})")
        blocks.append("\n".join(lines))
    footer = _provider_warnings(session)
    footer.append(f"<i>Total price for the searched passengers, basic fare (small bag only). Indicative — confirm on the airline's site. "
                  f"Next update in about {hours:g} h.</i>")
    blocks.append("\n".join(footer))
    return blocks


# ---------------------------------------------------------------------- weekly report
@dataclass
class RouteWeek:
    trip: Trip
    offer: FlightOffer
    low: PriceHistory
    high: float
    first: float
    observations: int


def weekly_blocks(session: Session, config: AppConfig, *, now: datetime | None = None,
                  days: int | None = None) -> list[str]:
    now = now or utcnow()
    days = days or config.reports.report_days
    since = now - timedelta(days=days)
    airports = {a.iata: a for a in session.scalars(select(Airport))}
    trips = _upcoming(session, now.date())
    blocks = [f"📊 <b>Flight Radar weekly report</b> · {since:%d %b} – {now:%d %b}\n"
              f"<i>Best prices observed over the last {days} days.</i>"]
    weeks: list[RouteWeek] = []
    for t in trips:
        lines = [_trip_title(t)]
        rows = []
        for o in cheapest_offers(session, t):
            hist = sorted(_observations(session, [o], since), key=lambda h: h.observed_at)
            if not hist:
                continue
            low = min(hist, key=lambda h: h.observed_price)
            rows.append(RouteWeek(t, o, low, max(h.observed_price for h in hist), hist[0].observed_price, len(hist)))
        rows.sort(key=lambda r: r.low.observed_price)
        weeks += rows
        if not rows:
            lines.append("No prices observed this week.")
        for r in rows[:4]:
            trend = r.offer.price - r.first
            trend_txt = "flat" if abs(trend) < 0.01 else (f"🔻 {money(-trend, r.offer.currency)}" if trend < 0
                                                         else f"🔺 {money(trend, r.offer.currency)}")
            lines.append(
                f"• {_e(_route(airports, r.offer))}: low <b>{money(r.low.observed_price, r.low.currency)}</b> "
                f"({r.low.observed_at:%a %d %b}) · now {money(r.offer.price, r.offer.currency)} · "
                f"high {money(r.high, r.offer.currency)} · week {trend_txt}"
            )
        blocks.append("\n".join(lines))

    current = [r for r in weeks if r.offer.collected_at >= now - timedelta(hours=config.dashboard.current_offer_hours)]
    if current:
        best = min(current, key=lambda r: r.offer.price)
        o = best.offer
        seen_days = len({h.observed_at.date() for h in _observations(session, [o], since)})
        if seen_days < 3:
            verdict = (f"only {seen_days} day{'s' if seen_days != 1 else ''} of prices so far, "
                       "too little to say whether this is low")
        elif o.price <= best.low.observed_price + 0.01:
            verdict = f"the lowest price seen in {seen_days} days of checks"
        else:
            verdict = f"{money(o.price - best.low.observed_price, o.currency)} above this week's low"
        pick = (f"🏆 <b>Best pick right now</b>\n{_trip_title(best.trip)}\n"
                f"<b>{money(o.price, o.currency)}</b> {_e(_route(airports, o))} ({_e(airline_name(o.airline))}): "
                f"{verdict}.\n" + "\n".join(leg_lines(session, o, airports)))
        blocks.insert(1, pick)
    elif trips:
        blocks.insert(1, "🏆 No current fare for any watched trip, so there is no pick this week.")

    runs = session.scalars(select(SearchRun.started_at).where(SearchRun.started_at >= since,
                                                              SearchRun.status == "success")).all()
    slots = {r.replace(minute=0, second=0, microsecond=0) for r in runs}
    first_run = session.scalar(select(SearchRun.started_at).order_by(SearchRun.started_at).limit(1))
    covered_hours = (now - max(since, first_run)).total_seconds() / 3600 if first_run else 0
    expected = max(1, int(covered_hours / search_every_hours(session, config)) + 1)
    health = f"Searches completed: {len(slots)} of about {expected} expected"
    if first_run and first_run > since:
        health += f" (watching since {first_run:%d %b})"
    if len(slots) < expected * 0.75:
        health += " — the computer was probably off or asleep for part of the week"
    footer = [health] + _provider_warnings(session)
    footer.append("<i>Total price for the searched passengers, basic fare. Indicative — confirm on the airline's site.</i>")
    blocks.append("\n".join(footer))
    return blocks


# ---------------------------------------------------------------------- sending
def send_blocks(notifier: Notifier, blocks: list[str]) -> SendResult:
    """Joins blocks into as few Telegram messages as fit (4096 characters each)."""
    messages, current = [], ""
    for b in blocks:
        b = b[:TELEGRAM_MAX_LEN]
        if current and len(current) + 2 + len(b) > TELEGRAM_MAX_LEN:
            messages.append(current)
            current = b
        else:
            current = f"{current}\n\n{b}" if current else b
    if current:
        messages.append(current)
    result = SendResult("sent")
    for m in messages:
        result = notifier.send(m)
        if result.status != "sent":
            break
    return result


def send_digest(session: Session, config: AppConfig, notifier: Notifier) -> SendResult:
    result = send_blocks(notifier, digest_blocks(session, config))
    log.info("Telegram price digest: %s %s", result.status, result.error or "")
    return result


def last_weekly_slot(now_local: datetime, weekday: int, hour: int) -> datetime:
    """Most recent scheduled report time (local) at or before now_local."""
    slot = now_local.replace(hour=hour, minute=0, second=0, microsecond=0)
    slot -= timedelta(days=(now_local.weekday() - weekday) % 7)
    if slot > now_local:
        slot -= timedelta(days=7)
    return slot


def maybe_send_weekly(session: Session, config: AppConfig, notifier: Notifier,
                      *, now_local: datetime | None = None) -> SendResult | None:
    """Sends the weekly report once per scheduled slot. If the computer was off at the
    scheduled time, it is sent at the next check instead. The very first check only
    records the slot, so starting the app does not immediately send a report."""
    if not weekly_enabled(session, config):
        return None
    now_local = now_local or datetime.now()
    slot = last_weekly_slot(now_local, config.reports.weekly_day, config.reports.weekly_hour)
    last = settings_service.get_setting(session, WEEKLY_LAST_SENT)
    if last is None:
        settings_service.set_setting(session, WEEKLY_LAST_SENT, slot.isoformat())
        return None
    if datetime.fromisoformat(last) >= slot:
        return None
    result = send_blocks(notifier, weekly_blocks(session, config))
    log.info("Telegram weekly report: %s %s", result.status, result.error or "")
    if result.status in ("sent", "not_configured"):
        settings_service.set_setting(session, WEEKLY_LAST_SENT, slot.isoformat())
    return result
