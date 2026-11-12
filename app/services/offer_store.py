"""Persist normalized offers: de-duplicate by itinerary fingerprint, update the
latest price, and APPEND every fresh observation to price_history."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutils import derive_arrival, split_local
from app.models.db_models import Airport, FlightOffer, PriceHistory
from app.models.schemas import FlightOfferData


@dataclass
class SaveResult:
    offer_ids: list[int] = field(default_factory=list)
    new: int = 0
    updated: int = 0
    duplicates_in_batch: int = 0


def dedupe(offers: list[FlightOfferData]) -> tuple[list[FlightOfferData], int]:
    """Keep the cheapest instance of each itinerary within a batch."""
    best: dict[str, FlightOfferData] = {}
    for o in offers:
        fp = o.fingerprint()
        if fp not in best or o.price < best[fp].price:
            best[fp] = o
    return list(best.values()), len(offers) - len(best)


def _naive_utc(dt):
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def save_offers(
    session: Session,
    offers: list[FlightOfferData],
    *,
    search_run_id: int | None = None,
    search_configuration_id: int | None = None,
    record_history: bool = True,
) -> SaveResult:
    result = SaveResult()
    unique, result.duplicates_in_batch = dedupe(offers)
    tz_cache: dict[str, str | None] = {}

    def tz(code: str) -> str | None:
        if code not in tz_cache:
            ap = session.get(Airport, code)
            tz_cache[code] = ap.timezone if ap else None
        return tz_cache[code]

    for data in unique:
        fp = data.fingerprint()
        dep_local, dep_off = split_local(data.departure_at)
        ret_local, ret_off = split_local(data.return_departure_at)
        arr_local, _ = split_local(data.arrival_at)
        ret_arr_local, _ = split_local(data.return_arrival_at)
        estimated = False
        if arr_local is None:
            arr_local = derive_arrival(dep_local, dep_off, tz(data.origin), data.duration_minutes, tz(data.destination))
            estimated = arr_local is not None
        if ret_local is not None and ret_arr_local is None:
            ret_arr_local = derive_arrival(
                ret_local, ret_off, tz(data.destination), data.return_duration_minutes, tz(data.origin)
            )
            estimated = estimated or ret_arr_local is not None
        collected = _naive_utc(data.collected_at)

        offer = session.scalars(select(FlightOffer).where(FlightOffer.fingerprint == fp)).first()
        fields = dict(
            provider=data.provider,
            airline=data.airline,
            flight_number=data.flight_number,
            origin=data.origin,
            destination=data.destination,
            trip_type=data.trip_type.value,
            departure_at=dep_local,
            departure_utc_offset_min=dep_off,
            arrival_at=arr_local,
            return_departure_at=ret_local,
            return_departure_utc_offset_min=ret_off,
            return_arrival_at=ret_arr_local,
            arrival_is_estimated=estimated,
            price=data.price,
            currency=data.currency,
            passengers=data.passengers,
            stops=data.stops,
            return_stops=data.return_stops,
            duration_minutes=data.duration_minutes,
            return_duration_minutes=data.return_duration_minutes,
            baggage_included=data.baggage_included,
            baggage_info=data.baggage_info,
            booking_url=data.booking_url,
            price_type=data.price_type.value,
        )
        if offer is None:
            offer = FlightOffer(
                fingerprint=fp, first_seen_at=collected, collected_at=collected,
                search_configuration_id=search_configuration_id, **fields,
            )
            session.add(offer)
            session.flush()
            result.new += 1
        elif record_history:
            # Only fresh observations update the "latest" values.
            for k, v in fields.items():
                if v is not None or k in ("baggage_included",):
                    setattr(offer, k, v)
            offer.collected_at = collected
            if search_configuration_id and offer.search_configuration_id is None:
                offer.search_configuration_id = search_configuration_id
            result.updated += 1
        if record_history:
            session.add(
                PriceHistory(
                    offer_id=offer.id,
                    observed_price=data.price,
                    currency=data.currency,
                    observed_at=collected,
                    provider=data.provider,
                    search_run_id=search_run_id,
                )
            )
        result.offer_ids.append(offer.id)
    session.commit()
    return result
