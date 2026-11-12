"""First-run diagnostic: make ONE real request to the Travelpayouts Data API and show
what came back (raw + normalized), without saving anything.

    python scripts/check_travelpayouts.py                 # OPO->KRK return, next month
    python scripts/check_travelpayouts.py LIS KTW one_way 2027-01

Uses 1 request of the free quota. Requires TRAVELPAYOUTS_TOKEN in .env.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import load_config  # noqa: E402
from app.models.schemas import SearchQuery, TripType  # noqa: E402
from app.providers.travelpayouts import API_URL, TravelpayoutsProvider  # noqa: E402


def main() -> int:
    args = sys.argv[1:]
    origin, dest = (args[0], args[1]) if len(args) >= 2 else ("OPO", "KRK")
    trip = TripType(args[2]) if len(args) >= 3 else TripType.RETURN
    if len(args) >= 4:
        month = datetime.strptime(args[3], "%Y-%m").date()
    else:
        t = date.today()
        month = date(t.year + (t.month == 12), t.month % 12 + 1, 1)
    config = load_config()
    if not config.secrets.travelpayouts_token:
        print("TRAVELPAYOUTS_TOKEN is not set. Copy .env.example to .env and add your token.")
        return 1
    provider = TravelpayoutsProvider(config.secrets.travelpayouts_token)
    query = SearchQuery(origin=origin, destination=dest, trip_type=trip, departure_month=month,
                        currency=config.currency)
    params = provider.build_params(query)
    print(f"GET {API_URL} {params}")
    payload, _ = provider.http.get_json(API_URL, params, provider._headers(), use_cache=False)
    rows = payload.get("data") or []
    print(f"success={payload.get('success')} currency={payload.get('currency')} rows={len(rows)}")
    print("First raw rows:")
    print(json.dumps(rows[:3], indent=2, ensure_ascii=False))
    result = provider.parse_response(payload, query, collected_at=datetime.now(timezone.utc))
    print(f"\nNormalized offers: {len(result.offers)} (skipped {result.skipped_items})")
    for o in result.offers[:10]:
        ret = f" -> back {o.return_departure_at:%a %d %b %H:%M}" if o.return_departure_at else ""
        print(f"  {o.price:8.2f} {o.currency} {o.airline or '?':>3} {o.flight_number or '':>5} "
              f"{o.departure_at:%a %d %b %H:%M}{ret} stops={o.stops}")
    if not rows:
        print("\nNo cached prices for this route/month. That is normal for less-searched routes: "
              "the Data API only knows prices Aviasales users searched recently.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
