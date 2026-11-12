"""Date/time helpers. Flight times are stored as LOCAL wall-clock times (naive) plus,
when known, the UTC offset in minutes. All "system" timestamps are naive UTC."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    """Naive UTC 'now' (SQLite has no timezone support)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def split_local(dt: datetime | None) -> tuple[datetime | None, int | None]:
    """Aware datetime -> (naive local wall time, utc offset minutes)."""
    if dt is None:
        return None, None
    if dt.tzinfo is None:
        return dt, None
    offset = dt.utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset is not None else None
    return dt.replace(tzinfo=None), minutes


def localize(naive_local: datetime, tz_name: str | None, offset_min: int | None = None) -> datetime | None:
    """Rebuild an aware datetime from stored local wall time."""
    if offset_min is not None:
        return naive_local.replace(tzinfo=timezone(timedelta(minutes=offset_min)))
    if tz_name:
        return naive_local.replace(tzinfo=ZoneInfo(tz_name))
    return None


def derive_arrival(
    departure_local: datetime,
    departure_offset_min: int | None,
    origin_tz: str | None,
    duration_minutes: int | None,
    destination_tz: str | None,
) -> datetime | None:
    """Arrival local wall time = departure + duration, converted to the destination timezone."""
    if not duration_minutes or not destination_tz:
        return None
    aware = localize(departure_local, origin_tz, departure_offset_min)
    if aware is None:
        return None
    arrival = (aware + timedelta(minutes=duration_minutes)).astimezone(ZoneInfo(destination_tz))
    return arrival.replace(tzinfo=None)


def month_starts(start: date, end: date) -> list[date]:
    """First day of every month touched by [start, end]."""
    months = []
    cur = start.replace(day=1)
    while cur <= end:
        months.append(cur)
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months
