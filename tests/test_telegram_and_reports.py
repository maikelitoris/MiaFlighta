"""Telegram message formatting, notifier behaviour (mocked) and command texts."""

import asyncio

from app.models.db_models import Airport, FlightOffer, SearchConfiguration
from app.notifications import telegram as tg
from app.notifications.telegram import TelegramNotifier, format_deal_message
from app.services import reports, settings_service
from app.services.deal_engine import Baseline, assess
from app.services.offer_store import save_offers
from tests.conftest import make_offer


def stored(session, **kw):
    return session.get(FlightOffer, save_offers(session, [make_offer(**kw)]).offer_ids[0])


def test_deal_message_contains_required_fields(session, config):
    offer = stored(session, price=48, booking_url="https://example.invalid/x?a=1&b=2")
    baseline = Baseline(median=105, average=110, lowest=80, samples=9, lookback_days=60)
    a = assess(offer, config, baseline, 9)
    msg = format_deal_message(offer, a, session.get(Airport, "OPO"), session.get(Airport, "KRK"))
    assert "EXCELLENT FLIGHT DEAL" in msg
    assert "Porto (OPO) → Kraków (KRK)" in msg
    assert "€48.00" in msg and "return" in msg
    assert "Ryanair (FR)" in msg and "flight 1000" in msg
    assert "Stops: Direct" in msg
    assert "Baggage:" in msg
    assert "Departure:" in msg and "Return:" in msg
    assert "€105.00" in msg and "-54%" in msg
    assert f"Deal score: <b>{a.score}/100</b>" in msg
    assert "Source: test_provider" in msg and "Last verified:" in msg
    assert "Indicative cached price" in msg
    assert 'href="https://example.invalid/x?a=1&amp;b=2"' in msg  # escaped for HTML


def test_message_without_history_says_so(session, config):
    offer = stored(session, price=45, destination="KTW")
    a = assess(offer, config, None, 2)
    msg = format_deal_message(offer, a, None, None, "bus estimate")
    assert "not enough comparable data yet (2 offers)" in msg
    assert "ground transfer" in msg and "Effective price" in msg


def test_notifier_not_configured():
    assert TelegramNotifier(None, None).send("hi").status == "not_configured"


def test_notifier_sends_with_bot(monkeypatch):
    sent = {}

    class FakeBot:
        def __init__(self, token):
            sent["token"] = token

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send_message(self, **kw):
            sent.update(kw)

    import telegram

    monkeypatch.setattr(telegram, "Bot", FakeBot)
    result = TelegramNotifier("TEST:TOKEN", "12345").send("<b>hello</b>")
    assert result.status == "sent"
    assert sent["chat_id"] == "12345" and sent["text"] == "<b>hello</b>" and sent["parse_mode"] == "HTML"


def test_notifier_failure_is_reported(monkeypatch):
    async def boom(self, text):
        raise RuntimeError("network down")

    monkeypatch.setattr(TelegramNotifier, "_send_async", boom)
    r = TelegramNotifier("T", "1").send("x")
    assert r.status == "failed" and "network down" in r.error


def test_budget_command(session, config):
    assert "€150.00" in reports.budget_command(session, config, [])
    assert "✅" in reports.budget_command(session, config, ["120"])
    assert settings_service.get_budget(session, config) == 120
    assert "✅" in reports.budget_command(session, config, ["3", "90,5"])
    assert session.get(SearchConfiguration, 3).max_price == 90.5
    reports.budget_command(session, config, ["3", "off"])
    assert session.get(SearchConfiguration, 3).max_price is None
    assert "Usage" in reports.budget_command(session, config, ["abc"])
    assert "Usage" in reports.budget_command(session, config, ["-5"])
    assert "Unknown route" in reports.budget_command(session, config, ["999", "50"])


def test_status_routes_and_deals_texts(session, config):
    assert "Flight Radar status" in reports.status_text(session, config)
    assert "not all airlines are checked" in reports.status_text(session, config)
    assert "#1 OPO→KRK (return)" in reports.routes_text(session, config)
    assert "No current offers" in reports.deals_text(session, config)
    stored(session, price=45)
    text = reports.deals_text(session, config)
    assert "OPO→KRK" in text and "€45.00" in text


def test_money_format():
    assert tg.money(48, "EUR") == "€48.00"
    assert tg.money(1234.5, "CZK") == "1,234.50 CZK"
