"""Common interface for all flight data providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

from app.models.schemas import FlightOfferData, PriceType, SearchQuery


@dataclass
class ProviderResult:
    offers: list[FlightOfferData] = field(default_factory=list)
    from_cache: bool = False  # True = no fresh observation was made
    requests_made: int = 0
    skipped_items: int = 0  # raw results that could not be normalized
    warnings: list[str] = field(default_factory=list)


@dataclass
class HealthResult:
    ok: bool
    status: str  # ok | not_configured | auth_error | blocked | failing
    message: str = ""


class FlightProvider(ABC):
    """Every data source implements this. Providers must:
    * return only real data from the source (never invented prices);
    * mark prices as INDICATIVE unless the source confirms they are bookable;
    * raise app.core.errors.ProviderError subclasses on failure.
    """

    name: ClassVar[str]
    price_type: ClassVar[PriceType]
    coverage_note: ClassVar[str] = ""
    # True if one query can cover a whole month (fewer API calls).
    supports_month_search: ClassVar[bool] = False

    def get_provider_name(self) -> str:
        return self.name

    @abstractmethod
    def is_configured(self) -> bool: ...

    @abstractmethod
    def search_flights(self, query: SearchQuery) -> ProviderResult: ...

    def get_offer_details(self, offer: FlightOfferData) -> FlightOfferData | None:
        """Re-check / enrich an offer. Return None if the provider cannot do this."""
        return None

    @abstractmethod
    def health_check(self) -> HealthResult: ...

    def close(self) -> None:  # pragma: no cover - optional
        pass
