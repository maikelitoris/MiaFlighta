"""Plain-text/HTML summaries used by Telegram commands and the CLI, plus shared queries."""

from __future__ import annotations

import html
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.airlines import airline_name
from app.core.config import AppConfig
from app.core.timeutils import utcnow
from app.models.db_models import (
    FlightOffer,
    PriceHistory,
    ProviderState,
    SearchConfiguration,
    SearchRun,
)
from app.notifications.telegram import money, stops_text
from app.services import settings_service
from app.services.deal_engine import DealAssessment, assess_offer


def current_offers(session: Session, config: AppConfig, *, now: datetime | None = None) -> list[FlightOffer]:
    """Offers observed recently with a future departure."""
    now = now or utcnow()
    since = now - timedelta(hours=config.dashboard.current_offer_hours)
    return list(
        session.scalars(
            select(FlightOffer)
            .where(FlightOffer.collected_at >= since, FlightOffer.departure_at > now)
            .order_by(FlightOffer.price)
        )
    )


def best_deals(session: Session, config: AppConfig, limit: int = 5) -> list[tuple[FlightOffer, DealAssessment]]:
    scored = [(o, assess_offer(session, o, config)) for o in current_offers(session, config)]
    scored.sort(key=lambda x: (-x[1].score, x[1].effective_price_pp))
    return scored[:limit]


def last_successful_run(session: Session) -> SearchRun | None:
    return session.scalars(
        select(SearchRun).where(SearchRun.status == "success").order_by(SearchRun.completed_at.desc())
    ).first()


def fmt_time(dt: datetime | None) -> str:
    return dt.strftime("%d %b %H:%M UTC") if dt else "never"


def status_text(session: Session, config: AppConfig) -> str:
    offers = session.scalar(select(func.count(FlightOffer.id))) or 0
    observations = session.scalar(select(func.count(PriceHistory.id))) or 0
    last = last_successful_run(session)
    lines = [
        "<b>Flight Radar status</b>",
        f"Tracked itineraries: {offers}",
        f"Price observations: {observations}",
        f"Current offers (last {config.dashboard.current_offer_hours:.0f}h): {len(current_offers(session, config))}",
        f"Last successful search: {fmt_time(last.completed_at if last else None)}",
        f"Budget: {money(settings_service.get_budget(session, config), config.currency)} per person",
        "",
        "<b>Providers</b>",
    ]
    states = {s.provider: s for s in session.scalars(select(ProviderState))}
    for name in config.providers:
        s = states.get(name)
        enabled = settings_service.provider_enabled(session, config, name)
        status = s.status if s else "never run"
        line = f"• {name}: {status}{'' if enabled else ' (disabled)'}"
        if s and s.paused_until and s.paused_until > utcnow():
            line += f", paused until {fmt_time(s.paused_until)}"
        if s and s.status != "ok" and s.last_error:
            line += f"\n  last error: {html.escape(s.last_error[:150])}"
        lines.append(line)
    lines.append("\n<i>Coverage is limited to the enabled providers; not all airlines are checked.</i>")
    return "\n".join(lines)


def deals_text(session: Session, config: AppConfig, limit: int = 5) -> str:
    deals = best_deals(session, config, limit)
    if not deals:
        return "No current offers yet. Run a search first (/search)."
    lines = ["<b>Best current offers</b> (indicative prices — verify before booking)", ""]
    for offer, a in deals:
        when = offer.departure_at.strftime("%a %d %b %H:%M")
        if offer.return_departure_at:
            when += " → " + offer.return_departure_at.strftime("%a %d %b")
        link = f' · <a href="{html.escape(offer.booking_url, quote=True)}">open</a>' if offer.booking_url else ""
        lines.append(
            f"<b>{offer.origin}→{offer.destination}</b> {money(offer.price, offer.currency)} "
            f"({'return' if offer.trip_type == 'return' else 'one-way'}) · {a.category} {a.score}/100\n"
            f"  {when} · {html.escape(airline_name(offer.airline))} · {stops_text(offer.stops)}{link}"
        )
    return "\n".join(lines)


def routes_text(session: Session, config: AppConfig) -> str:
    cfgs = list(session.scalars(select(SearchConfiguration).order_by(SearchConfiguration.id)))
    if not cfgs:
        return "No routes configured."
    budget = settings_service.get_budget(session, config)
    lines = ["<b>Routes</b>"]
    for c in cfgs:
        price = money(c.max_price, config.currency) if c.max_price else f"global {money(budget, config.currency)}"
        stay = f", stay {c.min_stay_days}-{c.max_stay_days}d" if c.trip_type == "return" else ""
        lines.append(
            f"#{c.id} {c.label}{' [off]' if not c.enabled else ''}: "
            f"{c.days_ahead_min}-{c.days_ahead_max} days ahead{stay}, max {price}, every {c.frequency_hours:g}h, "
            f"last run {fmt_time(c.last_run_at)}"
        )
    return "\n".join(lines)


def budget_command(session: Session, config: AppConfig, args: list[str]) -> str:
    """/budget -> show; /budget 120 -> set global; /budget 3 90 -> set route #3; /budget 3 off -> clear."""
    try:
        if not args:
            return f"Global budget: {money(settings_service.get_budget(session, config), config.currency)} per person.\n" \
                   "Change it with /budget 120 or per route with /budget &lt;route id&gt; 90 (see /routes)."
        if len(args) == 1:
            value = float(args[0].replace(",", "."))
            settings_service.set_budget(session, value)
            return f"✅ Global budget set to {money(value, config.currency)} per person."
        if len(args) == 2:
            cfg = session.get(SearchConfiguration, int(args[0].lstrip("#")))
            if cfg is None:
                return "Unknown route id. See /routes."
            if args[1].lower() in ("off", "none", "global"):
                cfg.max_price = None
                session.commit()
                return f"✅ Route #{cfg.id} now uses the global budget."
            value = float(args[1].replace(",", "."))
            if value <= 0:
                raise ValueError
            cfg.max_price = value
            session.commit()
            return f"✅ Budget for #{cfg.id} {cfg.label} set to {money(value, config.currency)} per person."
    except ValueError:
        pass
    return "Usage: /budget, /budget 120, /budget 3 90 or /budget 3 off"
