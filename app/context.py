"""Builds the application object graph once (config, DB, providers, services)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import AppConfig, load_config
from app.database.db import DbResponseCache, init_database
from app.notifications.telegram import TelegramNotifier
from app.providers.base import FlightProvider
from app.providers.registry import build_providers
from app.services.search_service import SearchService


@dataclass
class AppContext:
    config: AppConfig
    session_factory: sessionmaker[Session]
    providers: dict[str, FlightProvider]
    notifier: TelegramNotifier
    search_service: SearchService


def build_context(config: AppConfig | None = None, *, sync_routes: bool = False) -> AppContext:
    config = config or load_config()
    factory = init_database(config, sync_routes=sync_routes)
    providers = build_providers(config, cache=DbResponseCache(factory))
    notifier = TelegramNotifier(config.secrets.telegram_bot_token, config.secrets.telegram_chat_id)
    service = SearchService(config, factory, providers, notifier)
    return AppContext(config, factory, providers, notifier, service)
