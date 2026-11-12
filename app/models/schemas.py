"""Provider-independent, validated data models (Pydantic)."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator


class TripType(StrEnum):
    ONE_WAY = "one_way"
    RETURN = "return"


class PriceType(StrEnum):
    INDICATIVE = "indicative"  # cached / estimated, may no longer be bookable
    CONFIRMED = "confirmed"  # live, bookable price confirmed by the provider


def _iata(v: str) -> str:
    v = (v or "").strip().upper()
    if len(v) != 3 or not v.isalpha():
        raise ValueError(f"invalid IATA code: {v!r}")
    return v


class SearchQuery(BaseModel):
    origin: str
    destination: str
    trip_type: TripType
    # Either a whole month (providers that support month search) or a specific date.
    departure_month: date | None = None
    departure_date: date | None = None
    return_date: date | None = None
    passengers: int = Field(1, ge=1, le=9)
    currency: str = "EUR"
    # Optional hints for providers that build return trips themselves (e.g. Ryanair).
    min_stay_days: int | None = None
    max_stay_days: int | None = None
    return_weekdays: list[int] = []

    @field_validator("origin", "destination")
    @classmethod
    def _v_iata(cls, v: str) -> str:
        return _iata(v)

    @field_validator("currency")
    @classmethod
    def _cur(cls, v: str) -> str:
        return v.strip().upper()

    @model_validator(mode="after")
    def _one_date(self) -> SearchQuery:
        if (self.departure_month is None) == (self.departure_date is None):
            raise ValueError("set exactly one of departure_month or departure_date")
        return self

    def describe(self) -> str:
        when = self.departure_date.isoformat() if self.departure_date else self.departure_month.strftime("%Y-%m")
        return f"{self.origin}->{self.destination} {self.trip_type.value} {when}"


class FlightOfferData(BaseModel):
    """A normalized offer as returned by any provider. Datetimes should be
    timezone-aware when the provider gives an offset; unknown values are None."""

    provider: str
    airline: str | None = None
    flight_number: str | None = None
    origin: str
    destination: str
    trip_type: TripType
    departure_at: datetime
    arrival_at: datetime | None = None
    return_departure_at: datetime | None = None
    return_arrival_at: datetime | None = None
    price: float = Field(gt=0)  # total for all passengers
    currency: str
    passengers: int = Field(1, ge=1)
    stops: int | None = Field(None, ge=0)
    return_stops: int | None = Field(None, ge=0)
    duration_minutes: int | None = Field(None, ge=0)
    return_duration_minutes: int | None = Field(None, ge=0)
    baggage_included: bool | None = None
    baggage_info: str | None = None
    booking_url: str | None = None
    price_type: PriceType
    collected_at: datetime

    @field_validator("origin", "destination")
    @classmethod
    def _v_iata(cls, v: str) -> str:
        return _iata(v)

    @field_validator("currency")
    @classmethod
    def _cur(cls, v: str) -> str:
        v = v.strip().upper()
        if len(v) != 3:
            raise ValueError(f"invalid currency: {v!r}")
        return v

    @field_validator("airline")
    @classmethod
    def _airline(cls, v: str | None) -> str | None:
        return v.strip().upper() or None if v else None

    @model_validator(mode="after")
    def _return_consistency(self) -> FlightOfferData:
        if self.trip_type == TripType.RETURN and self.return_departure_at is None:
            raise ValueError("return offers need return_departure_at")
        if self.trip_type == TripType.ONE_WAY and self.return_departure_at is not None:
            raise ValueError("one-way offers cannot have return_departure_at")
        return self

    def fingerprint(self) -> str:
        """Provider-independent itinerary identity used for de-duplication.
        Uses local wall-clock times so the same flight matches across providers."""

        def fmt(dt: datetime | None) -> str:
            return dt.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M") if dt else ""

        parts = [
            self.trip_type.value,
            self.origin,
            self.destination,
            self.airline or "",
            (self.flight_number or "").lstrip("0"),
            fmt(self.departure_at),
            fmt(self.return_departure_at),
            str(self.passengers),
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:40]
