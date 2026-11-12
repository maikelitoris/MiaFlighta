"""Diagnostic: query Ryanair's fare finder once for one route/month and print the
result (nothing is saved). About 2-3 requests.

    python scripts/check_ryanair.py                  # OPO -> KRK, next month, one-way
    python scripts/check_ryanair.py LIS KRK 2027-01
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.schemas import SearchQuery, TripType  # noqa: E402
from app.providers.ryanair import API_BASE, RyanairProvider  # noqa: E402


def main() -> int:
    args = sys.argv[1:]
    origin, dest = (args[0].upper(), args[1].upper()) if len(args) >= 2 else ("OPO", "KRK")
    if len(args) >= 3:
        month = datetime.strptime(args[2], "%Y-%m").date()
    else:
        t = date.today()
        month = date(t.year + (t.month == 12), t.month % 12 + 1, 1)
    p = RyanairProvider()
    print(f"Warm-up: GET https://www.ryanair.com/ie/en -> HTTP {p.http.visit('https://www.ryanair.com/ie/en')}")
    p._warmed = True
    url = f"{API_BASE}oneWayFares/{origin}/{dest}/cheapestPerDay"
    params = {"outboundMonthOfDate": month.isoformat(), "currency": "EUR"}
    print(f"GET {url} {params}")
    try:
        payload, _ = p.http.get_json(url, params, use_cache=False)
        fares = (payload.get("outbound") or {}).get("fares") or []
        print(f"Raw: {len(fares)} day entries. First 3:")
        print(json.dumps(fares[:3], indent=2))
    except Exception as exc:  # show the problem instead of a traceback
        print(f"cheapestPerDay failed: {exc}")
    result = p.search_flights(SearchQuery(origin=origin, destination=dest, trip_type=TripType.ONE_WAY,
                                          departure_month=month))
    print(f"\nNormalized one-way offers: {len(result.offers)} ({result.requests_made} requests)")
    for o in result.offers[:15]:
        arr = f"{o.arrival_at:%H:%M}" if o.arrival_at else "?"
        print(f"  {o.departure_at:%a %d %b %H:%M} -> {arr}  {o.price:7.2f} {o.currency}")
    if not result.offers:
        print("No fares: Ryanair may not fly this route in that month, or the endpoint changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
