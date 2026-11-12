"""Provider health tracking. Blocked providers are paused, not bypassed."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.timeutils import utcnow
from app.models.db_models import ProviderState

log = logging.getLogger(__name__)


def get_state(session: Session, provider: str) -> ProviderState:
    state = session.get(ProviderState, provider)
    if state is None:
        state = ProviderState(provider=provider, status="unknown", consecutive_failures=0)
        session.add(state)
        session.flush()
    return state


def is_paused(session: Session, provider: str, now: datetime | None = None) -> bool:
    state = session.get(ProviderState, provider)
    now = now or utcnow()
    return bool(state and state.paused_until and state.paused_until > now)


def record_success(session: Session, provider: str) -> None:
    state = get_state(session, provider)
    state.status = "ok"
    state.last_success_at = utcnow()
    state.consecutive_failures = 0
    state.paused_until = None
    session.commit()


def record_failure(session: Session, provider: str, error: str, *, status: str = "failing",
                   pause_hours: float | None = None) -> None:
    state = get_state(session, provider)
    state.status = status
    state.last_failure_at = utcnow()
    state.last_error = error[:2000]
    state.consecutive_failures = (state.consecutive_failures or 0) + 1
    if pause_hours:
        state.paused_until = utcnow() + timedelta(hours=pause_hours)
        log.error("Provider %s paused until %s UTC: %s", provider, state.paused_until, error)
    session.commit()


def resume(session: Session, provider: str) -> None:
    """Manual resume (dashboard). Use only after checking the cause."""
    state = get_state(session, provider)
    state.paused_until = None
    state.status = "unknown"
    session.commit()
