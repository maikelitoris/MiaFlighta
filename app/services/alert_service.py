"""Decide which offers deserve a notification and send them without repeats."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import AppConfig
from app.core.timeutils import utcnow
from app.models.db_models import Airport, Alert, FlightOffer, SearchConfiguration
from app.notifications.telegram import Notifier, SendResult, format_deal_message
from app.services import settings_service
from app.services.deal_engine import CATEGORY_RANK, DealAssessment, assess_offer

log = logging.getLogger(__name__)

# Alerts in these states count as "already handled" for duplicate prevention.
# Failed sends are retried on the next run.
HANDLED_STATUSES = ("sent", "not_configured", "disabled")


@dataclass
class Candidate:
    offer: FlightOffer
    assessment: DealAssessment


def last_handled_alert(session: Session, offer_id: int) -> Alert | None:
    return session.scalars(
        select(Alert)
        .where(Alert.offer_id == offer_id, Alert.notification_status.in_(HANDLED_STATUSES))
        .order_by(Alert.created_at.desc(), Alert.id.desc())
    ).first()


def is_repeat(previous: Alert | None, offer: FlightOffer, assessment: DealAssessment, drop_pct: float) -> bool:
    if previous is None:
        return False
    cheaper = offer.price <= previous.price * (1 - drop_pct / 100)
    upgraded = CATEGORY_RANK[assessment.category] > CATEGORY_RANK.get(previous.alert_type.removesuffix("_deal"), 0)
    return not (cheaper or upgraded)


def find_candidates(session: Session, config: AppConfig, offer_ids: list[int]) -> list[Candidate]:
    min_rank = CATEGORY_RANK[config.alerts.min_category]
    now = utcnow()
    candidates = []
    for oid in dict.fromkeys(offer_ids):
        offer = session.get(FlightOffer, oid)
        if offer is None or offer.departure_at <= now:
            continue
        cfg = session.get(SearchConfiguration, offer.search_configuration_id) if offer.search_configuration_id else None
        assessment = assess_offer(session, offer, config)
        budget = settings_service.effective_max_price(session, config, cfg)
        if assessment.effective_price_pp > budget:
            continue
        if CATEGORY_RANK[assessment.category] < min_rank:
            continue
        if is_repeat(last_handled_alert(session, oid), offer, assessment, config.alerts.realert_price_drop_pct):
            continue
        candidates.append(Candidate(offer, assessment))
    candidates.sort(key=lambda c: (-c.assessment.score, c.assessment.effective_price_pp))
    return candidates


def process_alerts(session: Session, config: AppConfig, offer_ids: list[int], notifier: Notifier) -> list[Alert]:
    candidates = find_candidates(session, config, offer_ids)[: config.alerts.max_alerts_per_run]
    enabled = settings_service.telegram_enabled(session)
    alerts = []
    for c in candidates:
        alert = Alert(
            offer_id=c.offer.id,
            alert_type=f"{c.assessment.category}_deal",
            score=c.assessment.score,
            price=c.offer.price,
            currency=c.offer.currency,
            created_at=utcnow(),
            notification_status="pending",
        )
        session.add(alert)
        session.flush()
        if not enabled:
            result = SendResult("disabled", "Telegram notifications disabled in settings")
        else:
            tc = config.transfer_costs.get(c.offer.destination)
            text = format_deal_message(
                c.offer, c.assessment,
                session.get(Airport, c.offer.origin), session.get(Airport, c.offer.destination),
                tc.note if tc else None,
            )
            result = notifier.send(text)
        alert.notification_status = result.status
        alert.error = result.error
        if result.status == "sent":
            alert.sent_at = utcnow()
        log.info("Alert %s for offer %s (%s, score %s): %s", alert.id, c.offer.id,
                 alert.alert_type, alert.score, result.status)
        alerts.append(alert)
        session.commit()
    return alerts
