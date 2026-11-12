"""SQLAlchemy ORM models (SQLite)."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.timeutils import utcnow


class Base(DeclarativeBase):
    pass


class Airport(Base):
    __tablename__ = "airports"

    iata: Mapped[str] = mapped_column(String(3), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    city: Mapped[str] = mapped_column(String(80))
    country: Mapped[str] = mapped_column(String(80))
    timezone: Mapped[str] = mapped_column(String(64))


class SearchConfiguration(Base):
    __tablename__ = "search_configurations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    origin: Mapped[str] = mapped_column(ForeignKey("airports.iata"), index=True)
    destination: Mapped[str] = mapped_column(ForeignKey("airports.iata"), index=True)
    trip_type: Mapped[str] = mapped_column(String(8))  # "one_way" | "return"
    # Date range: absolute dates win; otherwise a rolling window relative to today.
    date_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    days_ahead_min: Mapped[int] = mapped_column(Integer, default=1)
    days_ahead_max: Mapped[int] = mapped_column(Integer, default=60)
    min_stay_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_stay_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    departure_weekdays: Mapped[str] = mapped_column(String(20), default="")  # "4,5"
    return_weekdays: Mapped[str] = mapped_column(String(20), default="")
    passengers: Mapped[int] = mapped_column(Integer, default=1)
    max_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # per passenger
    frequency_hours: Mapped[float] = mapped_column(Float, default=6)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    origin_airport: Mapped[Airport] = relationship(foreign_keys=[origin])
    destination_airport: Mapped[Airport] = relationship(foreign_keys=[destination])

    @property
    def label(self) -> str:
        kind = "return" if self.trip_type == "return" else "one-way"
        return f"{self.origin}→{self.destination} ({kind})"

    @staticmethod
    def parse_weekdays(value: str | None) -> set[int]:
        if not value:
            return set()
        return {int(x) for x in value.split(",") if x.strip() != ""}


class FlightOffer(Base):
    """One itinerary. The price columns hold the LATEST observation;
    every observation is kept in PriceHistory and never overwritten."""

    __tablename__ = "flight_offers"
    __table_args__ = (
        Index("ix_offer_route", "origin", "destination", "trip_type", "departure_at"),
        Index("ix_offer_collected", "collected_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(40))
    airline: Mapped[str | None] = mapped_column(String(8), nullable=True)
    flight_number: Mapped[str | None] = mapped_column(String(12), nullable=True)
    origin: Mapped[str] = mapped_column(ForeignKey("airports.iata"))
    destination: Mapped[str] = mapped_column(ForeignKey("airports.iata"))
    trip_type: Mapped[str] = mapped_column(String(8))
    # Local wall-clock times at the respective airport.
    departure_at: Mapped[datetime] = mapped_column(DateTime)
    departure_utc_offset_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    arrival_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    return_departure_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    return_departure_utc_offset_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    return_arrival_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    arrival_is_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    price: Mapped[float] = mapped_column(Float)  # total for all passengers
    currency: Mapped[str] = mapped_column(String(3))
    passengers: Mapped[int] = mapped_column(Integer, default=1)
    stops: Mapped[int | None] = mapped_column(Integer, nullable=True)
    return_stops: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    return_duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    baggage_included: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # None = unknown
    baggage_info: Mapped[str | None] = mapped_column(String(200), nullable=True)
    booking_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_type: Mapped[str] = mapped_column(String(12))  # "indicative" | "confirmed"
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    search_configuration_id: Mapped[int | None] = mapped_column(
        ForeignKey("search_configurations.id", ondelete="SET NULL"), nullable=True
    )

    history: Mapped[list[PriceHistory]] = relationship(
        back_populates="offer", cascade="all, delete-orphan", order_by="PriceHistory.observed_at"
    )
    alerts: Mapped[list[Alert]] = relationship(back_populates="offer", cascade="all, delete-orphan")

    @property
    def price_per_passenger(self) -> float:
        return self.price / max(self.passengers, 1)

    @property
    def is_direct(self) -> bool | None:
        if self.stops is None:
            return None
        if self.trip_type == "return":
            if self.return_stops is None:
                return None
            return self.stops == 0 and self.return_stops == 0
        return self.stops == 0

    @property
    def stay_days(self) -> int | None:
        if self.return_departure_at is None:
            return None
        return (self.return_departure_at.date() - self.departure_at.date()).days


class PriceHistory(Base):
    __tablename__ = "price_history"
    __table_args__ = (Index("ix_history_offer_time", "offer_id", "observed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("flight_offers.id", ondelete="CASCADE"))
    observed_price: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3))
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    provider: Mapped[str] = mapped_column(String(40))
    search_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("search_runs.id", ondelete="SET NULL"), nullable=True
    )

    offer: Mapped[FlightOffer] = relationship(back_populates="history")


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("flight_offers.id", ondelete="CASCADE"), index=True)
    alert_type: Mapped[str] = mapped_column(String(30))  # excellent_deal | good_deal
    score: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # pending | sent | failed | not_configured
    notification_status: Mapped[str] = mapped_column(String(20), default="pending")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    offer: Mapped[FlightOffer] = relationship(back_populates="alerts")


class SearchRun(Base):
    __tablename__ = "search_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    search_configuration_id: Mapped[int | None] = mapped_column(
        ForeignKey("search_configurations.id", ondelete="SET NULL"), nullable=True
    )
    trigger: Mapped[str] = mapped_column(String(20), default="manual")  # manual | scheduled
    # running | success | failed | blocked | skipped
    status: Mapped[str] = mapped_column(String(20), default="running")
    results_count: Mapped[int] = mapped_column(Integer, default=0)
    new_offers: Mapped[int] = mapped_column(Integer, default=0)
    requests_made: Mapped[int] = mapped_column(Integer, default=0)
    cache_hits: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProviderState(Base):
    """Health of each provider, used to skip blocked/broken providers."""

    __tablename__ = "provider_states"

    provider: Mapped[str] = mapped_column(String(40), primary_key=True)
    # ok | failing | blocked | auth_error | not_configured | unknown
    status: Mapped[str] = mapped_column(String(20), default="unknown")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    paused_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AppSetting(Base):
    """Runtime settings changed from the dashboard or Telegram (JSON values)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class CachedResponse(Base):
    __tablename__ = "response_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(40))
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
