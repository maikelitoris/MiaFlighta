"""Deal scoring, historical comparison, budget filter and duplicate-alert prevention."""

from datetime import timedelta

from sqlalchemy import select

from app.models.db_models import Alert, FlightOffer, PriceHistory
from app.models.schemas import TripType
from app.notifications.telegram import SendResult
from app.services import settings_service
from app.services.alert_service import process_alerts
from app.services.deal_engine import assess, assess_offer, comparable_baseline
from app.services.offer_store import save_offers
from tests.conftest import future, make_offer


class FakeNotifier:
    def __init__(self, status="sent"):
        self.messages = []
        self.status = status

    def send(self, text):
        self.messages.append(text)
        return SendResult(self.status, None if self.status == "sent" else "test failure")


def store(session, **kw) -> FlightOffer:
    res = save_offers(session, [make_offer(**kw)])
    return session.get(FlightOffer, res.offer_ids[0])


def add_comparables(session, n, price, days_start=20, **kw):
    for i in range(n):
        store(session, departure_at=future(days_start + i, 9), return_departure_at=future(days_start + i + 7, 12),
              price=price, flight_number=str(5000 + i), **kw)


# ---------------------------------------------------------------- scoring

def test_score_categories(session, config):
    cheap = store(session, price=45, trip_type=TripType.ONE_WAY, return_departure_at=None, return_stops=None)
    a = assess(cheap, config, None)
    # <50 (+40) direct (+10) convenient 09:00 (+10) = 60 -> good
    assert a.score == 60 and a.category == "good"
    assert any("direct" in r for r in a.reasons)


def test_no_history_points_without_enough_data(session, config):
    add_comparables(session, 3, 200)
    offer = store(session, price=60)
    baseline, samples = comparable_baseline(session, offer, config)
    assert baseline is None and samples == 3
    a = assess_offer(session, offer, config)
    assert a.baseline is None and a.discount_pct is None
    assert not any("median" in r for r in a.reasons)


def test_history_discount_points(session, config):
    add_comparables(session, 8, 200)
    offer = store(session, price=90)
    a = assess_offer(session, offer, config)
    assert a.baseline is not None and a.baseline.median == 200 and a.baseline.samples == 8
    assert a.discount_pct == 55.0
    # <100 (+40) + history (+30) + direct (+10) + convenient (+10) = 90
    assert a.score == 90 and a.category == "excellent"


def test_comparable_excludes_different_stays_and_baggage(session, config):
    # stays of 14 days are not comparable to a 7-day stay (tolerance 2)
    for i in range(8):
        store(session, departure_at=future(20 + i), return_departure_at=future(34 + i), price=300,
              flight_number=str(6000 + i))
    # baggage-included journeys are not comparable to unknown-baggage ones
    add_comparables(session, 8, 300, days_start=40, baggage_included=True)
    offer = store(session, price=90)
    baseline, samples = comparable_baseline(session, offer, config)
    assert baseline is None and samples == 0


def test_comparable_ignores_other_currency(session, config):
    add_comparables(session, 8, 200, currency="PLN")
    offer = store(session, price=90)
    assert comparable_baseline(session, offer, config)[0] is None


def test_history_uses_lookback_window(session, config):
    add_comparables(session, 8, 200)
    for h in session.scalars(select(PriceHistory)):
        h.observed_at = h.observed_at - timedelta(days=config.scoring.history_lookback_days + 1)
    session.commit()
    offer = store(session, price=90)
    assert comparable_baseline(session, offer, config)[0] is None


def test_katowice_transfer_cost_included(session, config):
    krk = store(session, price=95)
    ktw = store(session, price=95, destination="KTW")
    a_krk, a_ktw = assess(krk, config, None), assess(ktw, config, None)
    assert a_krk.effective_price_pp == 95
    assert a_ktw.transfer_cost_pp == 30 and a_ktw.effective_price_pp == 125  # 15 each way
    assert a_ktw.score < a_krk.score  # 125 no longer below the 100 threshold


def test_baggage_and_inconvenient_time(session, config):
    o = store(session, price=500, baggage_included=True, departure_at=future(10, 5), stops=1)
    a = assess(o, config, None)
    assert a.score == 10 and a.category == "normal"  # only baggage


def test_return_thresholds_use_total_return_price(session, config):
    o = store(session, price=60)
    one_way = store(session, price=60, trip_type=TripType.ONE_WAY, return_departure_at=None,
                    departure_at=future(12), return_stops=None)
    # 60 return is < 100 (+40); 60 one-way is < 70 but not < 50 (+25)
    assert "below 100" in assess(o, config, None).reasons[0]
    assert "below 70" in assess(one_way, config, None).reasons[0]


# ---------------------------------------------------------------- alerts

def test_alert_sent_once_for_unchanged_offer(session, config):
    offer = store(session, price=45)
    n = FakeNotifier()
    first = process_alerts(session, config, [offer.id], n)
    second = process_alerts(session, config, [offer.id], n)
    assert len(first) == 1 and first[0].notification_status == "sent"
    assert second == []
    assert len(n.messages) == 1


def test_realert_on_price_drop(session, config):
    offer = store(session, price=80)
    n = FakeNotifier()
    assert len(process_alerts(session, config, [offer.id], n)) == 1
    save_offers(session, [make_offer(price=79)])  # -1.25%: not enough
    assert process_alerts(session, config, [offer.id], n) == []
    save_offers(session, [make_offer(price=70)])  # -12.5%: re-alert
    assert len(process_alerts(session, config, [offer.id], n)) == 1


def test_failed_send_is_retried_next_run(session, config):
    offer = store(session, price=45)
    failing = FakeNotifier("failed")
    a = process_alerts(session, config, [offer.id], failing)
    assert a[0].notification_status == "failed" and a[0].error
    ok = FakeNotifier()
    assert len(process_alerts(session, config, [offer.id], ok)) == 1


def test_over_budget_not_alerted(session, config):
    offer = store(session, price=45)
    settings_service.set_budget(session, 40)
    assert process_alerts(session, config, [offer.id], FakeNotifier()) == []


def test_normal_deals_not_alerted(session, config):
    offer = store(session, price=149, stops=1, departure_at=future(10, 5))
    assert process_alerts(session, config, [offer.id], FakeNotifier()) == []


def test_past_departures_not_alerted(session, config):
    offer = store(session, price=45, departure_at=future(-2), return_departure_at=future(3))
    assert process_alerts(session, config, [offer.id], FakeNotifier()) == []


def test_telegram_disabled_records_alert_without_sending(session, config):
    offer = store(session, price=45)
    settings_service.set_setting(session, settings_service.TELEGRAM_ENABLED, False)
    n = FakeNotifier()
    alerts = process_alerts(session, config, [offer.id], n)
    assert alerts[0].notification_status == "disabled" and n.messages == []
    assert session.scalars(select(Alert)).one().notification_status == "disabled"


def test_max_alerts_per_run(session, config):
    config.alerts.max_alerts_per_run = 2
    ids = [store(session, price=40 + i, flight_number=str(7000 + i), departure_at=future(10 + i)).id for i in range(5)]
    n = FakeNotifier()
    assert len(process_alerts(session, config, ids, n)) == 2
