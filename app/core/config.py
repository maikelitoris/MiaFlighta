"""Application configuration: YAML defaults + environment variables (secrets)."""

from __future__ import annotations

import os
from datetime import time
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class AirportConfig(BaseModel):
    iata: str
    name: str
    city: str
    country: str
    timezone: str

    @field_validator("iata")
    @classmethod
    def _upper(cls, v: str) -> str:
        v = v.strip().upper()
        if len(v) != 3 or not v.isalpha():
            raise ValueError(f"invalid IATA code: {v!r}")
        return v


class RoutesConfig(BaseModel):
    origins: list[str] = ["OPO", "LIS", "FAO"]
    destinations: list[str] = ["KRK", "KTW"]
    trip_types: list[Literal["one_way", "return"]] = ["return", "one_way"]
    days_ahead_min: int = Field(1, ge=0)
    days_ahead_max: int = Field(60, ge=1, le=365)
    min_stay_days: int | None = 3
    max_stay_days: int | None = 10
    departure_weekdays: list[int] = []
    return_weekdays: list[int] = []
    passengers: int = Field(1, ge=1, le=9)
    max_price: float | None = None
    frequency_hours: float = Field(6, gt=0)


class TransferCost(BaseModel):
    amount: float = Field(ge=0)
    note: str = ""


class ProviderConfig(BaseModel):
    enabled: bool = True
    min_interval_seconds: float = 1.5
    cache_ttl_minutes: float = 60
    max_retries: int = 3
    max_requests_per_run: int = 100
    result_limit: int = 1000


class PriceThreshold(BaseModel):
    below: float
    points: int


class ConvenientDeparture(BaseModel):
    earliest: time = time(7, 0)
    latest: time = time(21, 0)
    points: int = 10


class ScoringConfig(BaseModel):
    price_points: dict[Literal["one_way", "return"], list[PriceThreshold]] = {
        "one_way": [PriceThreshold(below=50, points=40), PriceThreshold(below=70, points=25)],
        "return": [PriceThreshold(below=100, points=40), PriceThreshold(below=140, points=25)],
    }
    history_discount_pct: float = 30
    history_points: int = 30
    history_min_samples: int = 8
    history_min_days: int = 3
    history_lookback_days: int = 60
    stay_tolerance_days: int = 2
    direct_points: int = 10
    convenient_departure: ConvenientDeparture = ConvenientDeparture()
    baggage_points: int = 10
    excellent_min: int = 70
    good_min: int = 50


class AlertsConfig(BaseModel):
    min_category: Literal["excellent", "good"] = "good"
    realert_price_drop_pct: float = 5
    max_alerts_per_run: int = 10


class SchedulerConfig(BaseModel):
    tick_minutes: float = 30
    blocked_pause_hours: float = 24


class DigestConfig(BaseModel):
    every_hours: float = Field(12, ge=0)  # 0 = no periodic summary
    max_routes: int = Field(8, ge=1, le=30)


class DashboardConfig(BaseModel):
    current_offer_hours: float = 24


class Secrets(BaseModel):
    travelpayouts_token: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None


class AppConfig(BaseModel):
    currency: str = "EUR"
    default_max_price: float = 150
    airports: list[AirportConfig] = []
    routes: RoutesConfig = RoutesConfig()
    transfer_costs: dict[str, TransferCost] = {}
    providers: dict[str, ProviderConfig] = {"travelpayouts": ProviderConfig()}
    scoring: ScoringConfig = ScoringConfig()
    alerts: AlertsConfig = AlertsConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    dashboard: DashboardConfig = DashboardConfig()
    digest: DigestConfig = DigestConfig()
    database_path: str = "data/flight_radar.db"
    secrets: Secrets = Secrets()

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str) -> str:
        return v.strip().upper()

    def transfer_cost(self, airport: str) -> float:
        tc = self.transfer_costs.get(airport.upper())
        return tc.amount if tc else 0.0

    def provider(self, name: str) -> ProviderConfig:
        return self.providers.get(name, ProviderConfig())

    @property
    def database_url(self) -> str:
        path = Path(self.database_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{path}"


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load config.yaml (or FLIGHT_RADAR_CONFIG) and secrets from the environment / .env."""
    load_dotenv(PROJECT_ROOT / ".env")
    cfg_path = Path(path or _env("FLIGHT_RADAR_CONFIG") or PROJECT_ROOT / "config.yaml")
    if not cfg_path.is_absolute():
        cfg_path = PROJECT_ROOT / cfg_path
    data: dict = {}
    if cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if db := _env("FLIGHT_RADAR_DB"):
        data["database_path"] = db
    data["secrets"] = {
        "travelpayouts_token": _env("TRAVELPAYOUTS_TOKEN"),
        "telegram_bot_token": _env("TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": _env("TELEGRAM_CHAT_ID"),
    }
    return AppConfig.model_validate(data)
