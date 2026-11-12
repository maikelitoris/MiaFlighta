"""Shared fixtures. Everything here is TEST DATA — never real flight prices.
No test touches the network: HTTP is mocked with httpx.MockTransport."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.config import AppConfig, load_config
from app.database.db import init_database
from app.models.schemas import FlightOfferData, PriceType, TripType

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def config(tmp_path) -> AppConfig:
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    cfg = cfg.model_copy(update={"database_path": str(tmp_path / "test.db")})
    cfg.secrets.travelpayouts_token = "TEST-TOKEN"
    cfg.secrets.telegram_bot_token = None
    cfg.secrets.telegram_chat_id = None
    return cfg


@pytest.fixture
def session_factory(config):
    return init_database(config, url=f"sqlite:///{config.database_path}")


@pytest.fixture
def session(session_factory):
    with session_factory() as s:
        yield s


@pytest.fixture
def tp_fixture() -> dict:
    return json.loads((FIXTURES / "travelpayouts_prices_for_dates.TEST_DATA.json").read_text())


def future(days: int, hour: int = 10, minute: int = 0, offset_hours: int = 0) -> datetime:
    d = date.today() + timedelta(days=days)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=timezone(timedelta(hours=offset_hours)))


def make_offer(**overrides) -> FlightOfferData:
    """TEST DATA offer factory."""
    base = dict(
        provider="test_provider",
        airline="FR",
        flight_number="1000",
        origin="OPO",
        destination="KRK",
        trip_type=TripType.RETURN,
        departure_at=future(10, 9),
        return_departure_at=future(17, 12, offset_hours=1),
        price=120.0,
        currency="EUR",
        passengers=1,
        stops=0,
        return_stops=0,
        duration_minutes=200,
        return_duration_minutes=210,
        baggage_included=None,
        booking_url="https://example.invalid/test-offer",
        price_type=PriceType.INDICATIVE,
        collected_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return FlightOfferData(**base)


def tp_item(dep: datetime, ret: datetime | None, price: float, **extra) -> dict:
    """One TEST DATA row in Travelpayouts format."""
    item = {
        "origin": "OPO", "destination": "KRK", "origin_airport": "OPO", "destination_airport": "KRK",
        "price": price, "airline": "FR", "flight_number": "1234",
        "departure_at": dep.isoformat(), "transfers": 0, "return_transfers": 0,
        "duration_to": 205, "duration_back": 205, "link": "/search/TESTDATA",
    }
    if ret is not None:
        item["return_at"] = ret.isoformat()
    item.update(extra)
    return item
