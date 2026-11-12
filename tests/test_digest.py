"""12-hour Telegram digest. All offers here are TEST DATA."""

from datetime import timedelta

from app.core.timeutils import utcnow
from app.notifications.telegram import SendResult
from app.services import digest, settings_service
from app.services.offer_store import save_offers
from tests.conftest import make_offer


class FakeNotifier:
    configured = True

    def __init__(self, status="sent"):
        self.sent, self.status = [], status

    def send(self, text):
        self.sent.append(text)
        return SendResult(self.status)


def test_digest_lists_cheapest_offer_per_route(session, config):
    save_offers(session, [make_offer(price=140), make_offer(price=95, flight_number="2000"),
                          make_offer(destination="KTW", price=60, flight_number="3000")])
    text = digest.build_digest(session, config)
    assert "OPO→KRK" in text and "OPO→KTW" in text
    assert "95" in text and "140" not in text  # only the cheapest per route
    assert text.index("OPO→KTW") < text.index("OPO→KRK")  # cheapest first (60 + transfer < 95)


def test_digest_respects_budget(session, config):
    settings_service.set_budget(session, 50)
    save_offers(session, [make_offer(price=120)])
    text = digest.build_digest(session, config)
    assert "No offers within your budget" in text and "above budget" in text


def test_digest_sent_once_per_interval(session, config):
    n = FakeNotifier()
    now = utcnow()
    assert digest.maybe_send_digest(session, config, n, now=now) == "sent"
    assert digest.maybe_send_digest(session, config, n, now=now + timedelta(hours=6)) == "not_due"
    assert digest.maybe_send_digest(session, config, n, now=now + timedelta(hours=12)) == "sent"
    assert len(n.sent) == 2


def test_failed_digest_is_retried(session, config):
    now = utcnow()
    assert digest.maybe_send_digest(session, config, FakeNotifier("failed"), now=now) == "failed"
    assert digest.maybe_send_digest(session, config, FakeNotifier(), now=now + timedelta(minutes=30)) == "sent"


def test_digest_off_when_telegram_disabled(session, config):
    settings_service.set_setting(session, settings_service.TELEGRAM_ENABLED, False)
    n = FakeNotifier()
    assert digest.maybe_send_digest(session, config, n) == "disabled" and not n.sent
