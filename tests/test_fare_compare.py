"""Going/coming-back legs and comparison with other dates. All fares are TEST DATA."""

from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import func, select

from app.core.timeutils import utcnow
from app.models.db_models import DailyFare, FlightOffer, SearchConfiguration
from app.providers.ryanair import RyanairProvider
from app.services import digest
from app.services import trips as ts
from app.services.fare_compare import compare_offer, history_text
from app.services.search_service import SearchService
from tests.test_ryanair import FakeRyanair, month_payload
from tests.test_search_service import FakeNotifier

TODAY = date(2027, 1, 10)
OUT, BACK = date(2027, 1, 29), date(2027, 2, 1)
JAN, FEB = date(2027, 1, 1), date(2027, 2, 1)

# TEST DATA calendars: OPO->KRK in January, KRK->OPO in January and February.
OUT_JAN = {d: 60.0 + d for d in range(1, 32) if d % 2 == 1}   # flights on odd days
OUT_JAN[29] = 77.29
BACK_JAN = {d: 40.0 + d for d in range(1, 32)}
BACK_FEB = {d: 50.0 for d in range(1, 29)}
BACK_FEB[1] = 45.99


def run_search(config, session_factory, session):
    for c in session.scalars(select(SearchConfiguration)):
        session.delete(c)
    session.commit()
    ts.add_trip(session, config, OUT, BACK, ["OPO"], ["KRK"], today=TODAY)
    fake = FakeRyanair({
        ("OPO", "KRK", "2027-01"): month_payload(JAN, OUT_JAN),
        ("KRK", "OPO", "2027-01"): month_payload(JAN, BACK_JAN),
        ("KRK", "OPO", "2027-02"): month_payload(FEB, BACK_FEB),
    })
    ryanair = RyanairProvider(transport=httpx.MockTransport(fake), sleep=lambda s: None, min_interval_seconds=0)
    svc = SearchService(config, session_factory, {"ryanair": ryanair}, FakeNotifier())
    summary = svc.run(trigger="manual", force=True, today=TODAY)
    assert not summary.errors
    return session.scalars(select(FlightOffer)).one()


def test_daily_fares_stored_once_per_run(config, session_factory, session):
    run_search(config, session_factory, session)
    n = session.scalar(select(func.count(DailyFare.id)))
    assert n == len(OUT_JAN) + len(BACK_JAN) + len(BACK_FEB)


def test_going_and_coming_back_legs(config, session_factory, session):
    offer = run_search(config, session_factory, session)
    cmp = compare_offer(session, offer)
    assert round(offer.price, 2) == 123.28
    assert (cmp.going.origin, cmp.going.destination, cmp.going.day, cmp.going.price) == ("OPO", "KRK", OUT, 77.29)
    assert (cmp.coming_back.origin, cmp.coming_back.day, cmp.coming_back.price) == ("KRK", BACK, 45.99)
    assert cmp.going.departure_at.strftime("%H:%M") == "06:30"


def test_compared_with_other_dates_of_same_length(config, session_factory, session):
    offer = run_search(config, session_factory, session)
    cmp = compare_offer(session, offer)
    # every odd January day has a 3-night return in the TEST calendars: 16 trips
    assert cmp.same_length_count == 16 and cmp.nights == 3
    cheaper = [d for d in OUT_JAN if d != 29 and OUT_JAN[d] + (BACK_JAN.get(d + 3) or BACK_FEB[d + 3 - 31]) < 123.28]
    assert cmp.cheaper_count == len(cheaper)
    assert cmp.cheapest[0].outbound == date(2027, 1, 1) and cmp.cheapest[0].price == 61.0 + 44.0
    assert all(a.outbound.weekday() == 4 for a in cmp.same_weekdays)  # other Fridays
    assert cmp.rank_text and "3-night trips" in cmp.rank_text
    assert "no trend yet" in history_text(cmp)


def test_not_enough_dates_is_said_plainly(config, session):
    session.add(FlightOffer(provider="ryanair", airline="FR", origin="OPO", destination="KRK", trip_type="return",
                            departure_at=datetime(2027, 1, 29, 9, 40), return_departure_at=datetime(2027, 2, 1, 17, 55),
                            price=123.28, currency="EUR", passengers=1, price_type="indicative",
                            fingerprint="TEST", collected_at=utcnow(), first_seen_at=utcnow()))
    session.commit()
    cmp = compare_offer(session, session.scalars(select(FlightOffer)).one())
    assert cmp.going is None and cmp.rank_text is None and cmp.verdict == "not enough dates to compare"


def test_digest_shows_both_flights_and_comparison(config, session_factory, session):
    run_search(config, session_factory, session)
    ts_trip = ts.list_trips(session)[0]
    # make the trip "upcoming" relative to the real clock for the digest
    msg = "\n".join(digest.leg_lines(session, ts.cheapest_offers(session, ts_trip)[0], {}))
    assert "🛫 Going Fri 29 Jan 06:30→11:20 OPO→KRK · €77.29" in msg
    assert "🛬 Back  Mon 01 Feb 06:30→11:20 KRK→OPO · €45.99" in msg
    assert "Open booking page (both flights)" in msg
    assert "3-night trips" in msg and "Cheaper dates: Fri 01 Jan → Mon 04 Jan €105.00" in msg
