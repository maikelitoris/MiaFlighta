"""Engine/session creation and idempotent seeding from config.yaml."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import create_engine, delete, event, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import AppConfig
from app.core.timeutils import utcnow
from app.models.db_models import Airport, Base, CachedResponse, SearchConfiguration

log = logging.getLogger(__name__)


def make_engine(url: str) -> Engine:
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")  # dashboard + scheduler can read/write together
        cur.close()

    return engine


# Columns added after the first release: (table, column, SQL type). create_all()
# never alters existing tables, so older databases get them added here.
_ADDED_COLUMNS = [
    ("search_configurations", "return_date_from", "DATE"),
    ("search_configurations", "return_date_to", "DATE"),
]


def _migrate(engine: Engine) -> None:
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, column, sql_type in _ADDED_COLUMNS:
            if table in insp.get_table_names() and column not in {c["name"] for c in insp.get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
                log.info("Database upgraded: added %s.%s", table, column)


def make_session_factory(config: AppConfig, url: str | None = None) -> sessionmaker[Session]:
    engine = make_engine(url or config.database_url)
    Base.metadata.create_all(engine)
    _migrate(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def seed_airports(session: Session, config: AppConfig) -> int:
    added = 0
    for a in config.airports:
        existing = session.get(Airport, a.iata)
        if existing is None:
            session.add(Airport(**a.model_dump()))
            added += 1
    session.commit()
    return added


def seed_search_configurations(session: Session, config: AppConfig, *, sync: bool = False) -> int:
    """Create route configurations from config.routes. By default only when the
    table is empty; with sync=True, add missing origin/destination/trip_type combos."""
    existing = session.scalars(select(SearchConfiguration)).all()
    if existing and not sync:
        return 0
    keys = {(c.origin, c.destination, c.trip_type) for c in existing}
    r = config.routes
    added = 0
    for origin in r.origins:
        for dest in r.destinations:
            for trip in r.trip_types:
                if (origin, dest, trip) in keys:
                    continue
                session.add(
                    SearchConfiguration(
                        origin=origin.upper(),
                        destination=dest.upper(),
                        trip_type=trip,
                        days_ahead_min=r.days_ahead_min,
                        days_ahead_max=r.days_ahead_max,
                        min_stay_days=r.min_stay_days if trip == "return" else None,
                        max_stay_days=r.max_stay_days if trip == "return" else None,
                        departure_weekdays=",".join(map(str, r.departure_weekdays)),
                        return_weekdays=",".join(map(str, r.return_weekdays)) if trip == "return" else "",
                        passengers=r.passengers,
                        max_price=r.max_price,
                        frequency_hours=r.frequency_hours,
                        enabled=True,
                    )
                )
                added += 1
    session.commit()
    return added


def init_database(config: AppConfig, *, sync_routes: bool = False, url: str | None = None) -> sessionmaker[Session]:
    factory = make_session_factory(config, url)
    with factory() as session:
        a = seed_airports(session, config)
        c = seed_search_configurations(session, config, sync=sync_routes)
    if a or c:
        log.info("Seeded %d airports and %d search configurations", a, c)
    return factory


class DbResponseCache:
    """Response cache stored in SQLite (implements app.core.http.ResponseCache)."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def get(self, key: str) -> Any | None:
        with self.session_factory() as s:
            row = s.get(CachedResponse, key)
            if row is None or row.expires_at <= utcnow():
                return None
            return json.loads(row.payload)

    def set(self, key: str, provider: str, value: Any, ttl_seconds: float) -> None:
        now = utcnow()
        with self.session_factory() as s:
            s.merge(
                CachedResponse(
                    key=key,
                    provider=provider,
                    payload=json.dumps(value),
                    created_at=now,
                    expires_at=now + timedelta(seconds=ttl_seconds),
                )
            )
            s.execute(delete(CachedResponse).where(CachedResponse.expires_at <= now))
            s.commit()
