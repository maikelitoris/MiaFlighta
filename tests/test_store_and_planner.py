"""Offer storage (de-duplication, history), DB cache, search planning, time handling."""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

from app.core.timeutils import derive_arrival, month_starts, split_local
from app.database.db import DbResponseCache, seed_search_configurations
from app.models.db_models import FlightOffer, PriceHistory, SearchConfiguration
from app.models.schemas import TripType
from app.services.offer_store import dedupe, save_offers
from app.services.search_planner import date_window, offer_matches, plan_queries
from tests.conftest import future, make_offer


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_history_is_appended_not_overwritten(session):
    save_offers(session, [make_offer(price=120)])
    save_offers(session, [make_offer(price=99)])
    save_offers(session, [make_offer(price=105)])
    assert count(session, FlightOffer) == 1
    offer = session.scalars(select(FlightOffer)).one()
    assert offer.price == 105  # latest
    assert [h.observed_price for h in offer.history] == [120, 99, 105]


def test_cached_results_do_not_create_observations(session):
    save_offers(session, [make_offer(price=120)])
    save_offers(session, [make_offer(price=80)], record_history=False)
    offer = session.scalars(select(FlightOffer)).one()
    assert offer.price == 120 and len(offer.history) == 1


def test_duplicates_in_batch_keep_cheapest(session):
    batch = [make_offer(price=130), make_offer(price=110, provider="other"), make_offer(price=125)]
    unique, dups = dedupe(batch)
    assert len(unique) == 1 and dups == 2 and unique[0].price == 110
    res = save_offers(session, batch)
    assert res.new == 1 and res.duplicates_in_batch == 2


def test_same_itinerary_from_two_providers_is_one_offer(session):
    save_offers(session, [make_offer(provider="a", price=100)])
    save_offers(session, [make_offer(provider="b", price=95)])
    offer = session.scalars(select(FlightOffer)).one()
    assert offer.provider == "b"
    assert {h.provider for h in offer.history} == {"a", "b"}


def test_different_dates_are_different_offers(session):
    save_offers(session, [make_offer(), make_offer(departure_at=future(11, 9))])
    assert count(session, FlightOffer) == 2


def test_local_times_and_derived_arrival(session):
    # OPO 09:00 WET (+00:00), 200 min flight -> arrives KRK 13:20 CET (+01:00) in winter
    dep = datetime(2027, 1, 15, 9, 0, tzinfo=timezone.utc)
    ret = datetime(2027, 1, 20, 12, 0, tzinfo=timezone(timedelta(hours=1)))
    save_offers(session, [make_offer(departure_at=dep, return_departure_at=ret)])
    o = session.scalars(select(FlightOffer)).one()
    assert o.departure_at == datetime(2027, 1, 15, 9, 0)
    assert o.departure_utc_offset_min == 0
    assert o.arrival_at == datetime(2027, 1, 15, 13, 20)
    # return KRK 12:00 CET + 210 min -> 14:30 Lisbon time
    assert o.return_arrival_at == datetime(2027, 1, 20, 14, 30)
    assert o.arrival_is_estimated is True
    assert o.stay_days == 5


def test_derive_arrival_across_dst():
    # 29 Mar 2027 Lisbon is on WEST (+01:00), Warsaw CEST (+02:00)
    arr = derive_arrival(datetime(2027, 3, 29, 6, 0), None, "Europe/Lisbon", 200, "Europe/Warsaw")
    assert arr == datetime(2027, 3, 29, 10, 20)
    assert derive_arrival(datetime(2027, 3, 29, 6, 0), None, "Europe/Lisbon", None, "Europe/Warsaw") is None


def test_split_local_and_months():
    naive, off = split_local(datetime(2027, 1, 1, 10, tzinfo=timezone(timedelta(hours=1))))
    assert naive == datetime(2027, 1, 1, 10) and off == 60
    assert month_starts(date(2026, 11, 20), date(2027, 1, 10)) == [date(2026, 11, 1), date(2026, 12, 1), date(2027, 1, 1)]


def test_db_response_cache_expiry(session_factory):
    cache = DbResponseCache(session_factory)
    cache.set("k", "p", {"a": 1}, ttl_seconds=60)
    assert cache.get("k") == {"a": 1}
    cache.set("k2", "p", {"b": 2}, ttl_seconds=-1)
    assert cache.get("k2") is None


def _cfg(**kw):
    base = dict(id=1, origin="OPO", destination="KRK", trip_type="return", days_ahead_min=1, days_ahead_max=60,
                min_stay_days=3, max_stay_days=10, departure_weekdays="", return_weekdays="", passengers=1,
                frequency_hours=6, enabled=True)
    base.update(kw)
    return SearchConfiguration(**base)


def test_plan_month_queries():
    today = date(2026, 11, 12)
    qs = plan_queries(_cfg(), today, currency="EUR", month_search=True)
    assert [q.departure_month for q in qs] == [date(2026, 11, 1), date(2026, 12, 1), date(2027, 1, 1)]
    assert all(q.trip_type == TripType.RETURN for q in qs)


def test_plan_daily_queries_respect_weekdays():
    today = date(2026, 11, 12)  # Thursday
    qs = plan_queries(_cfg(departure_weekdays="4", days_ahead_max=14), today, currency="EUR", month_search=False)
    assert [q.departure_date.weekday() for q in qs] == [4, 4]


def test_absolute_date_range_and_no_past():
    today = date(2026, 11, 12)
    start, end = date_window(_cfg(date_from=date(2026, 10, 1), date_to=date(2026, 12, 31)), today)
    assert start == today and end == date(2026, 12, 31)


def test_offer_filters():
    today = date.today()
    cfg = _cfg(min_stay_days=3, max_stay_days=7)
    assert offer_matches(cfg, make_offer(departure_at=future(10), return_departure_at=future(15)), today)
    assert not offer_matches(cfg, make_offer(departure_at=future(10), return_departure_at=future(11)), today)
    assert not offer_matches(cfg, make_offer(departure_at=future(10), return_departure_at=future(30)), today)
    assert not offer_matches(cfg, make_offer(departure_at=future(90), return_departure_at=future(95)), today)
    dep = future(10)
    fri_cfg = _cfg(departure_weekdays=str((dep.weekday() + 1) % 7))
    assert not offer_matches(fri_cfg, make_offer(departure_at=dep, return_departure_at=future(15)), today)


def test_seed_sync_adds_missing_routes(session, config):
    before = count(session, SearchConfiguration)
    assert before == 12
    assert seed_search_configurations(session, config) == 0
    session.delete(session.get(SearchConfiguration, 1))
    session.commit()
    assert seed_search_configurations(session, config, sync=True) == 1
    assert count(session, SearchConfiguration) == 12


def test_history_rows_have_provider_and_currency(session):
    save_offers(session, [make_offer(price=50, provider="travelpayouts")], search_run_id=None)
    h = session.scalars(select(PriceHistory)).one()
    assert h.provider == "travelpayouts" and h.currency == "EUR" and h.observed_price == 50
