"""Periodic Telegram digest: every `digest.every_hours` (default 12h) send the
cheapest current offers per route, whether or not they scored as a "deal".

Deal alerts (app/services/alert_service.py) still go out immediately; the digest
is the regular "here is where prices stand" message. The last send time is kept
in app_settings, so restarting the app never causes a duplicate digest."""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.airlines import airline_name
from app.core.config import AppConfig
from app.core.timeutils import utcnow
from app.models.db_models import FlightOffer, SearchConfiguration
from app.notifications.telegram import TELEGRAM_MAX_LEN, Notifier, money, stops_text
from app.services import reports, settings_service
from app.services.deal_engine import assess_offer

log = logging.getLogger(__name__)

LAST_DIGEST = "last_digest_at"
CATEGORY_ICON = {"excellent": "🔥", "good": "👍", "normal": "•"}


def _when(o: FlightOffer) -> str:
    text = o.departure_at.strftime("%a %d %b")
    if o.return_departure_at:
        text += " → " + o.return_departure_at.strftime("%a %d %b")
    return text


def build_digest(session: Session, config: AppConfig, *, now: datetime | None = None) -> str:
    """Cheapest current offer per route (within budget), cheapest first."""
    now = now or utcnow()
    cfg_by_id = {c.id: c for c in session.query(SearchConfiguration)}
    best: dict[tuple, tuple] = {}
    over_budget = 0
    for o in reports.current_offers(session, config, now=now):
        a = assess_offer(session, o, config)
        budget = settings_service.effective_max_price(session, config, cfg_by_id.get(o.search_configuration_id))
        if a.effective_price_pp > budget:
            over_budget += 1
            continue
        key = (o.origin, o.destination, o.trip_type)
        if key not in best or a.effective_price_pp < best[key][1].effective_price_pp:
            best[key] = (o, a)

    hours = config.digest.every_hours
    lines = [f"🗓️ <b>Flight Radar — {hours:g}h summary</b>", now.strftime("%a %d %b %H:%M UTC"), ""]
    if not best:
        lines.append("No offers within your budget right now.")
        if over_budget:
            lines.append(f"({over_budget} offers found above budget — raise it with /budget.)")
    else:
        rows = sorted(best.values(), key=lambda x: x[1].effective_price_pp)[: config.digest.max_routes]
        for o, a in rows:
            link = f' · <a href="{html.escape(o.booking_url, quote=True)}">open</a>' if o.booking_url else ""
            kind = "return" if o.trip_type == "return" else "one-way"
            lines.append(
                f"{CATEGORY_ICON.get(a.category, '•')} <b>{o.origin}→{o.destination}</b> "
                f"{money(o.price, o.currency)} {kind} · {_when(o)}\n"
                f"   {html.escape(airline_name(o.airline))} · {stops_text(o.stops)} · score {a.score}{link}"
            )
    last = reports.last_successful_run(session)
    lines += ["", f"Last successful search: {reports.fmt_time(last.completed_at if last else None)}",
              "<i>Indicative prices from the enabled providers only — verify before booking. /deals for more.</i>"]
    return "\n".join(lines)[:TELEGRAM_MAX_LEN]


def digest_due(session: Session, config: AppConfig, now: datetime) -> bool:
    if config.digest.every_hours <= 0:
        return False
    last = settings_service.get_setting(session, LAST_DIGEST)
    if not last:
        return True
    return now - datetime.fromisoformat(last) >= timedelta(hours=config.digest.every_hours) - timedelta(minutes=1)


def maybe_send_digest(session: Session, config: AppConfig, notifier: Notifier, *,
                      now: datetime | None = None, force: bool = False) -> str:
    """Send the digest if it is due. Returns "sent", "not_due", "disabled" or the failure status."""
    now = now or utcnow()
    if not settings_service.telegram_enabled(session) or not getattr(notifier, "configured", True):
        return "disabled"
    if not force and not digest_due(session, config, now):
        return "not_due"
    result = notifier.send(build_digest(session, config, now=now))
    if result.status == "sent":
        settings_service.set_setting(session, LAST_DIGEST, now.isoformat())
        log.info("Telegram digest sent")
    else:
        log.warning("Telegram digest not sent: %s %s", result.status, result.error or "")
    return result.status
