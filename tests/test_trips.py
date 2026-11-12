"""Trips (exact out/back date pairs) and request estimates. All fares are TEST DATA."""

from datetime import date

import httpx
import pytest
from sqlalchemy import select

from app.core.config import TripConfig
from app.database.db import seed_search_configurations
from app.models.db_models import FlightOffer, SearchConfiguration
from app.services import trips as ts
from app.services.search_planner import offer_matches
from app.services.search_service import SearchService
from tests.conftest import make_offer
from tests.test_ryanair import FakeRyanair, month_payload
from tests.test_search_service import FakeNotifier
from app.providers.ryanair import RyanairProvider

TODAY = date(2027, 1, 10)
OUT, BACK = date(2027, 1, 29), date(2027, 2, 1)  # Fri -> Mon


def clear_routes(session):
    for c in session.scalars(select(SearchConfiguration)):
        session.delete(c)
    session.commit()


def test_add_trip_creates_one_config_per_airport_pair(config, session):
    clear_routes(session)
    added = ts.add_trip(session, config, OUT, BACK, ["OPO", "LIS"], ["KRK", "KTW"], today=TODAY)
    assert added == 4
    configs = session.scalars(select(SearchConfiguration)).all()
    assert all(ts.is_trip_config(c) for c in configs)
    assert {(c.origin, c.destination) for c in configs} == {("OPO", "KRK"), ("OPO", "KTW"),
                                                           ("LIS", "KRK"), ("LIS", "KTW")}
    assert all(c.min_stay_days == 3 and c.date_from == OUT == c.date_to for c in configs)


def test_add_trip_twice_does_not_duplicate(config, session):
    clear_routes(session)
    ts.add_trip(session, config, OUT, BACK, ["OPO"], ["KRK"], today=TODAY)
    trip = ts.list_trips(session)[0]
    ts.set_trip_enabled(session, trip, False)
    assert ts.add_trip(session, config, OUT, BACK, ["OPO", "LIS"], ["KRK"], today=TODAY) == 1
    assert len(session.scalars(select(SearchConfiguration)).all()) == 2
    assert ts.list_trips(session)[0].enabled  # re-adding re-enables


@pytest.mark.parametrize("out,back,msg", [
    (date(2027, 2, 1), date(2027, 1, 29), "before"),
    (date(2027, 1, 5), date(2027, 1, 8), "past"),
])
def test_invalid_dates_rejected(config, session, out, back, msg):
    with pytest.raises(ValueError, match=msg):
        ts.add_trip(session, config, out, back, ["OPO"], ["KRK"], today=TODAY)


def test_list_trips_groups_and_ignores_rolling_routes(config, session):
    # the fixture DB already holds rolling routes; they must not appear as trips
    ts.add_trip(session, config, OUT, BACK, ["OPO", "LIS"], ["KRK"], today=TODAY)
    ts.add_trip(session, config, date(2027, 2, 5), date(2027, 2, 8), ["OPO"], ["KRK"], today=TODAY)
    trips = ts.list_trips(session)
    assert [(t.outbound, t.back) for t in trips] == [(OUT, BACK), (date(2027, 2, 5), date(2027, 2, 8))]
    assert trips[0].origins == ["OPO", "LIS"] and trips[0].nights == 3
    assert "Fri 29 Jan 2027" in trips[0].label and "Mon 01 Feb 2027" in trips[0].label


def test_repeat_weekly():
    assert ts.repeat_weekly(OUT, BACK, 3) == [
        (OUT, BACK), (date(2027, 2, 5), date(2027, 2, 8)), (date(2027, 2, 12), date(2027, 2, 15))]


def test_trip_config_matches_only_exact_dates(config, session):
    clear_routes(session)
    ts.add_trip(session, config, OUT, BACK, ["OPO"], ["KRK"], today=TODAY)
    c = session.scalars(select(SearchConfiguration)).one()
    exact = make_offer(departure_at=make_offer().departure_at.replace(year=2027, month=1, day=29),
                       return_departure_at=make_offer().return_departure_at.replace(year=2027, month=2, day=1))
    sunday = make_offer(departure_at=exact.departure_at,
                        return_departure_at=exact.return_departure_at.replace(month=1, day=31))
    assert offer_matches(c, exact, TODAY)
    assert not offer_matches(c, sunday, TODAY)


def test_delete_trip_keeps_offers(config, session):
    clear_routes(session)
    ts.add_trip(session, config, OUT, BACK, ["OPO"], ["KRK"], today=TODAY)
    cid = session.scalars(select(SearchConfiguration)).one().id
    from app.services.offer_store import save_offers
    save_offers(session, [make_offer()], search_configuration_id=cid)
    ts.delete_trip(session, ts.list_trips(session)[0])
    assert ts.list_trips(session) == []
    assert session.scalars(select(FlightOffer)).one().search_configuration_id is None


def test_disable_rolling_routes(config, session):
    ts.add_trip(session, config, OUT, BACK, ["OPO"], ["KRK"], today=TODAY)
    assert ts.disable_rolling_routes(session) > 0
    enabled = [c for c in session.scalars(select(SearchConfiguration)) if c.enabled]
    assert len(enabled) == 1 and ts.is_trip_config(enabled[0])


def test_seed_trips_from_config_without_rolling_routes(config, session):
    clear_routes(session)
    routes = config.routes.model_copy(update={"seed_rolling_routes": False, "origins": ["OPO", "LIS"]})
    cfg = config.model_copy(update={"routes": routes,
                                    "trips": [TripConfig(outbound=date(2099, 1, 30), back=date(2099, 2, 2)),
                                              TripConfig(outbound=date(2000, 1, 1), back=date(2000, 1, 3))]})
    assert seed_search_configurations(session, cfg) == 4  # 2 origins x 2 destinations, past trip skipped
    assert all(ts.is_trip_config(c) for c in session.scalars(select(SearchConfiguration)))


def test_trip_config_rejects_reversed_dates():
    with pytest.raises(ValueError):
        TripConfig(outbound=date(2027, 2, 1), back=date(2027, 1, 29))


# ---------------------------------------------------------------- request estimate + end to end
def ryanair_fake():
    jan, feb = date(2027, 1, 1), date(2027, 2, 1)
    return FakeRyanair({
        ("OPO", "KRK", "2027-01"): month_payload(jan, {29: 77.29, 30: 50.0}),
        ("KRK", "OPO", "2027-02"): month_payload(feb, {1: 45.99, 2: 20.0}),
        ("LIS", "KRK", "2027-01"): month_payload(jan, {29: 87.99}),
        ("KRK", "LIS", "2027-02"): month_payload(feb, {1: 38.99}),
    })


def test_estimate_counts_shared_months_once(config, session):
    clear_routes(session)
    ts.add_trip(session, config, OUT, BACK, ["OPO", "LIS"], ["KRK", "KTW"], today=TODAY)
    # a second trip in the same months costs no extra Ryanair requests
    ts.add_trip(session, config, date(2027, 1, 22), date(2027, 2, 1), ["OPO", "LIS"], ["KRK", "KTW"], today=TODAY)
    configs = list(session.scalars(select(SearchConfiguration)))
    providers = {"ryanair": RyanairProvider()}
    (u,) = ts.estimate_usage(configs, config, providers, today=TODAY)
    assert u.requests_per_run == 1 + 4 + 4  # warm-up + 4 outbound Jan + 4 inbound Feb
    assert u.runs_per_day == 2 and u.requests_per_day == 18  # every 12 h
    assert not u.over_cap


def test_estimate_flags_over_cap(config, session):
    clear_routes(session)
    for i in range(12):  # 12 trips in 12 different months
        out = date(2027, 2 + i % 11, 5) if i < 11 else date(2028, 1, 5)
        ts.add_trip(session, config, out, out.replace(day=8), ["OPO", "LIS"], ["KRK", "KTW"], today=TODAY)
    configs = list(session.scalars(select(SearchConfiguration)))
    (u,) = ts.estimate_usage(configs, config, {"ryanair": RyanairProvider()}, today=TODAY)
    assert u.over_cap and u.requests_per_run > config.provider("ryanair").max_requests_per_run


def test_trip_search_end_to_end_with_mocked_ryanair(config, session_factory, session):
    clear_routes(session)
    ts.add_trip(session, config, OUT, BACK, ["OPO", "LIS"], ["KRK"], today=TODAY)
    fake = ryanair_fake()
    ryanair = RyanairProvider(transport=httpx.MockTransport(fake), sleep=lambda s: None, min_interval_seconds=0)
    svc = SearchService(config, session_factory, {"ryanair": ryanair}, FakeNotifier())
    summary = svc.run(trigger="manual", force=True, today=TODAY)
    assert not summary.errors
    (trip,) = ts.list_trips(session)
    offers = ts.cheapest_offers(session, trip)
    # only Fri 29 Jan -> Mon 1 Feb, never the cheaper TEST fares on other days
    assert [(o.origin, round(o.price, 2)) for o in offers] == [("OPO", 123.28), ("LIS", 126.98)]
    assert summary.requests_made == 5  # warm-up + 2 outbound + 2 inbound months
