"""Build the provider instances from configuration. Add new providers here."""

from __future__ import annotations

from app.core.config import AppConfig
from app.core.http import ResponseCache
from app.providers.base import FlightProvider
from app.providers.ryanair import RyanairProvider
from app.providers.travelpayouts import TravelpayoutsProvider

AVAILABLE_PROVIDERS = ["travelpayouts", "ryanair"]


def build_providers(config: AppConfig, cache: ResponseCache | None = None) -> dict[str, FlightProvider]:
    providers: dict[str, FlightProvider] = {}
    tp = config.provider("travelpayouts")
    providers["travelpayouts"] = TravelpayoutsProvider(
        config.secrets.travelpayouts_token,
        min_interval_seconds=tp.min_interval_seconds,
        max_retries=tp.max_retries,
        result_limit=tp.result_limit,
        cache=cache,
        cache_ttl_seconds=tp.cache_ttl_minutes * 60,
    )
    if "ryanair" in config.providers:
        ra = config.provider("ryanair")
        providers["ryanair"] = RyanairProvider(
            min_interval_seconds=ra.min_interval_seconds,
            max_retries=ra.max_retries,
            cache=cache,
            cache_ttl_seconds=ra.cache_ttl_minutes * 60,
        )
    return providers
