"""12-hour price digest and weekly report. All prices are TEST DATA."""

from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.core.timeutils import utcnow
from app.models.db_models import PriceHistory, SearchConfiguration
from app.scheduler.scheduler import scheduled_tick
from app.services import digest, settings_service
from app.services import trips as ts
from app.services.offer_store import save_offers
from app.services.search_service import SearchService
from tests.conftest import make_offer
from tests.test_deals_and_alerts import FakeNotifier

OUT = date.today() + timedelta(days=30)
BACK = OUT + timedelta(days=3)


def trip_offer(price, origin="OPO", dest="KRK", **kw):
    """TEST DATA return offer for exactly the watched trip dates."""
    return make_offer(
        provider="ryanair", origin=origin, destination=dest, price=price,
        departure_at=datetime(OUT.year, OUT.month, OUT.day, 9, 40),
        arrival_at=datetime(OUT.year, OUT.month, OUT.day, 14, 20),
        return_departure_at=datetime(BACK.year, BACK.month, BACK.day, 17, 55),
        return_arrival_at=datetime(BACK.year, BACK.month, BACK.day, 20, 45),
        booking_url="https://example.invalid/TEST", **kw)


def setup_trip(config, session):
    for c in session.scalars(select(SearchConfiguration)):
        session.delete(c)
    session.commit()
    ts.add_trip(session, config, OUT, BACK, ["OPO", "LIS"], ["KRK"])


def text(blocks):
    return "\n\n".join(blocks)


def test_digest_lists_every_trip_even_without_a_deal(config, session):
    setup_trip(config, session)
    save_offers(session, [trip_offer(130.0)])
    save_offers(session, [trip_offer(123.28), trip_offer(126.98, origin="LIS")])
    (msg,) = digest.digest_blocks(session, config)  # one trip = one message
    preview = msg.split("\n")[0]
    assert "€123.28 Porto ⇄ Kraków" in preview  # readable as a phone notification
    assert "🥇 <b>€123.28</b> Porto ⇄ Kraków" in msg
    assert "Also: Lisbon ⇄ Kraków €126.98" in msg
    assert "🔻 €6.72 vs last check" in msg
    assert "Lowest seen so far: €123.28" in msg
    assert f"out {OUT:%a %d %b} 09:40 · back {BACK:%a %d %b} 17:55" in msg  # no calendar data: totals only
    assert "Confirm on the airline's site" in msg  # notes folded into the last message


def test_digest_says_when_nothing_was_found(config, session):
    setup_trip(config, session)
    (msg,) = digest.digest_blocks(session, config)
    assert "no fares yet" in msg.split("\n")[0]
    assert "No fare found for these exact dates yet" in msg
    assert "€" not in msg  # no invented price


def test_digest_with_no_trips(config, session):
    for c in session.scalars(select(SearchConfiguration)):
        session.delete(c)
    session.commit()
    assert "no upcoming trips" in text(digest.digest_blocks(session, config))


def test_one_message_per_trip(config, session):
    setup_trip(config, session)
    ts.add_trip(session, config, OUT + timedelta(days=7), BACK + timedelta(days=7), ["OPO"], ["KRK"])
    n = FakeNotifier()
    digest.send_blocks(n, digest.digest_blocks(session, config))
    assert len(n.messages) == 2 and all(m.startswith("<b>✈️") for m in n.messages)


def test_two_travellers_shown_side_by_side_with_total(config, session):
    setup_trip(config, session)
    ts.add_trip(session, config, OUT, BACK, ["KRK"], ["OPO"], traveler="Mia")
    save_offers(session, [trip_offer(123.28), trip_offer(98.50, origin="KRK", dest="OPO")])
    (msg,) = digest.digest_blocks(session, config)
    preview = msg.split("\n")[0]
    assert "You €123.28" in preview and "Mia €98.50" in preview and "together €221.78" in preview
    assert "👤 <b>You</b> · from Porto, Lisbon" in msg and "👤 <b>Mia</b> · from Kraków" in msg
    assert msg.index("👤 <b>You</b>") < msg.index("👤 <b>Mia</b>")
    assert "👫 <b>Together: €221.78</b> (you €123.28 + Mia €98.50)" in msg


def test_traveller_without_fare_shows_dash_and_no_total(config, session):
    setup_trip(config, session)
    ts.add_trip(session, config, OUT, BACK, ["KRK"], ["OPO"], traveler="Mia")
    save_offers(session, [trip_offer(123.28)])
    (msg,) = digest.digest_blocks(session, config)
    assert "Mia —" in msg.split("\n")[0] and "together" not in msg.split("\n")[0]
    assert "Together" not in msg


def test_weekly_report_picks_lowest_of_week(config, session):
    setup_trip(config, session)
    save_offers(session, [trip_offer(140.0), trip_offer(150.0, origin="LIS")])
    save_offers(session, [trip_offer(110.0)])
    save_offers(session, [trip_offer(125.0)])
    hist = sorted(session.scalars(select(PriceHistory)), key=lambda h: h.id)
    for i, h in enumerate(hist):  # spread observations over the week
        h.observed_at = utcnow() - timedelta(days=5 - i)
    session.commit()
    msg = text(digest.weekly_blocks(session, config))
    assert "weekly report" in msg
    assert "low <b>€110.00</b>" in msg and "now €125.00" in msg and "high €140.00" in msg
    assert "Best pick right now" in msg and "€15.00 above this week's low" in msg


def test_weekly_report_does_not_overclaim_with_little_history(config, session):
    setup_trip(config, session)
    save_offers(session, [trip_offer(123.28)])
    msg = text(digest.weekly_blocks(session, config))
    assert "too little to say" in msg and "good moment" not in msg


def test_weekly_report_old_observations_excluded(config, session):
    setup_trip(config, session)
    save_offers(session, [trip_offer(80.0)])
    for h in session.scalars(select(PriceHistory)):
        h.observed_at = utcnow() - timedelta(days=20)
    session.commit()
    assert "No prices observed this week" in text(digest.weekly_blocks(session, config))


def test_last_weekly_slot():
    sun_19 = datetime(2026, 11, 15, 19, 0)  # a Sunday
    assert digest.last_weekly_slot(datetime(2026, 11, 15, 19, 30), 6, 19) == sun_19
    assert digest.last_weekly_slot(datetime(2026, 11, 15, 18, 0), 6, 19) == sun_19 - timedelta(days=7)
    assert digest.last_weekly_slot(datetime(2026, 11, 18, 8, 0), 6, 19) == sun_19


def test_weekly_sent_once_per_slot_and_caught_up(config, session):
    setup_trip(config, session)
    n = FakeNotifier()
    sat = datetime(2026, 11, 14, 12, 0)
    assert digest.maybe_send_weekly(session, config, n, now_local=sat) is None  # first start: just records
    assert n.messages == []
    # PC was off on Sunday 19:00; switched on Monday morning -> report is caught up once
    mon = datetime(2026, 11, 16, 8, 0)
    assert digest.maybe_send_weekly(session, config, n, now_local=mon).status == "sent"
    sent = len(n.messages)
    assert digest.maybe_send_weekly(session, config, n, now_local=mon + timedelta(hours=1)) is None
    assert len(n.messages) == sent and "weekly report" in n.messages[0]


def test_weekly_can_be_turned_off(config, session):
    settings_service.set_setting(session, digest.WEEKLY_ENABLED, False)
    assert digest.maybe_send_weekly(session, config, FakeNotifier(), now_local=datetime(2026, 11, 16)) is None


def test_send_blocks_splits_long_messages():
    n = FakeNotifier()
    blocks = ["x" * 3000, "y" * 3000, "z" * 5000]
    assert digest.send_blocks(n, blocks).status == "sent"
    assert len(n.messages) == 3 and all(len(m) <= 4096 for m in n.messages)  # one per block, cut to fit
    packed = FakeNotifier()
    digest.send_blocks(packed, ["x" * 3000, "y" * 3000, "z" * 10], separate=False)
    assert len(packed.messages) == 2


class OneRouteService(SearchService):
    """Pretends one configuration was searched (no provider calls)."""

    def run(self, **kw):
        from app.services.search_service import RunSummary
        return RunSummary(configs_searched=1)


def test_scheduled_tick_sends_digest_after_each_search(config, session_factory, session):
    setup_trip(config, session)
    save_offers(session, [trip_offer(123.28)])
    n = FakeNotifier()
    svc = OneRouteService(config, session_factory, {}, n)
    scheduled_tick(svc)
    assert len(n.messages) == 1 and "€123.28" in n.messages[0]
    settings_service.set_setting(session, digest.DIGEST_ENABLED, False)
    scheduled_tick(svc)
    assert len(n.messages) == 1  # turned off: nothing more


def test_cloud_tick_forces_search_and_returns_summary(config, session_factory, session):
    setup_trip(config, session)
    calls = {}

    class Recording(OneRouteService):
        def run(self, **kw):
            calls.update(kw)
            return super().run(**kw)

    summary = scheduled_tick(Recording(config, session_factory, {}, FakeNotifier()), force=True)
    assert calls["force"] is True and summary.configs_searched == 1
