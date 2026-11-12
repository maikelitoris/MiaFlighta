"""Runtime settings stored in the app_settings table, with config.yaml defaults."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import AppConfig
from app.core.timeutils import utcnow
from app.models.db_models import AppSetting, SearchConfiguration

BUDGET = "max_price"
TELEGRAM_ENABLED = "telegram_enabled"
DISABLED_PROVIDERS = "disabled_providers"


def get_setting(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(AppSetting, key)
    return json.loads(row.value) if row else default


def set_setting(session: Session, key: str, value: Any) -> None:
    session.merge(AppSetting(key=key, value=json.dumps(value), updated_at=utcnow()))
    session.commit()


def get_budget(session: Session, config: AppConfig) -> float:
    return float(get_setting(session, BUDGET, config.default_max_price))


def set_budget(session: Session, value: float) -> None:
    if value <= 0:
        raise ValueError("budget must be positive")
    set_setting(session, BUDGET, float(value))


def effective_max_price(session: Session, config: AppConfig, search_config: SearchConfiguration | None) -> float:
    """Per-passenger budget: route override if set, else the global budget."""
    if search_config is not None and search_config.max_price:
        return float(search_config.max_price)
    return get_budget(session, config)


def telegram_enabled(session: Session) -> bool:
    return bool(get_setting(session, TELEGRAM_ENABLED, True))


def disabled_providers(session: Session) -> set[str]:
    return set(get_setting(session, DISABLED_PROVIDERS, []))


def provider_enabled(session: Session, config: AppConfig, name: str) -> bool:
    return config.provider(name).enabled and name not in disabled_providers(session)


def set_provider_enabled(session: Session, name: str, enabled: bool) -> None:
    disabled = disabled_providers(session)
    (disabled.discard if enabled else disabled.add)(name)
    set_setting(session, DISABLED_PROVIDERS, sorted(disabled))


def set_all_frequencies(session: Session, hours: float) -> None:
    if hours <= 0:
        raise ValueError("frequency must be positive")
    session.execute(update(SearchConfiguration).values(frequency_hours=hours))
    session.commit()


def enabled_configs(session: Session) -> list[SearchConfiguration]:
    return list(
        session.scalars(
            select(SearchConfiguration).where(SearchConfiguration.enabled.is_(True)).order_by(SearchConfiguration.id)
        )
    )
