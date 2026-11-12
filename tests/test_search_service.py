"""End-to-end search orchestration with fake providers, health handling and scheduling."""

from datetime import date, timedelta

import httpx
from sqlalchemy import func, select

from app.core.errors import ProviderBlockedError, ProviderTemporaryError
from app.core.timeutils import utcnow
from app.models.db_models import FlightOffer, PriceHistory, ProviderState, SearchConfiguration, SearchRun
from app.models.schemas import PriceType
from app.providers.base import FlightProvider, HealthResult, ProviderResult
from app.providers.travelpayouts import TravelpayoutsProvider
from app.scheduler.scheduler import JOB_ID, build_scheduler, scheduled_tick
from app.services import provider_health, settings_service
from app.services.search_service import SearchService, is_due
from tests.conftest import future, make_offer, tp_item
from tests.test_deals_and_alerts import FakeNotifier


class FakeProvider(FlightProvider):
    """TEST provider returning canned TEST DATA offers or raising."""

    name = "fake"
    price_type = PriceType.INDICATIVE
    supports_month_search = True

    def __init__(self, name="fake", error: Exception | None = None, price=45.0):
        self.name = name
        self.error = error
        self.price = price
        self.calls = 0

    def is_configured(self):
        return True

    def search_flights(self, query):
        self.calls += 1
        if self.error:
            raise self.error
        offers = [make_offer(provider=self.name, origin=query.origin, destination=query.destination,
                             trip_type=query.trip_type, price=self.price,
                             return_departure_at=future(17, 12, 1) if query.trip_type.value == "return" else None,
                             return_stops=0 if query.trip_type.value == "return" else None)]
        return ProviderResult(offers=offers, requests_made=1)

    def health_check(self):
        return HealthResult(True, "ok")


def only_route(session, route_id=1):
    for c in session.scalars(select(SearchConfiguration)):
        c.enabled = c.id == route_id
    session.commit()


def service(config, session_factory, providers, notifier=None):
    for name in providers:
        config.providers.setdefault(name, config.provider("travelpayouts").model_copy())
    return SearchService(config, session_factory, providers, notifier or FakeNotifier())


def test_full_run_stores_offers_and_alerts(config, session_factory, session):
    only_route(session)
    notifier = FakeNotifier()
    svc = service(config, session_factory, {"fake": FakeProvider()}, notifier)
    summary = svc.run(trigger="manual", force=True)
    assert summary.configs_searched == 1 and not summary.errors
    assert summary.offers_seen >= 1 and summary.alerts_sent == 1
    assert session.scalar(select(func.count(FlightOffer.id))) == 1
    run = session.scalars(select(SearchRun)).first()
    assert run.status == "success" and run.provider == "fake" and run.completed_at
    assert session.get(SearchConfiguration, 1).last_run_at is not None
    assert "DEAL" in notifier.messages[0]


def test_failing_provider_does_not_stop_others(config, session_factory, session):
    only_route(session)
    bad = FakeProvider("bad", error=ProviderTemporaryError("down"))
    good = FakeProvider("good")
    summary = service(config, session_factory, {"bad": bad, "good": good}).run(force=True)
    assert good.calls >= 1
    assert session.scalar(select(func.count(FlightOffer.id))) == 1
    assert any("down" in e for e in summary.errors)
    states = {s.provider: s for s in session.scalars(select(ProviderState))}
    assert states["bad"].status == "failing" and states["bad"].consecutive_failures == 1
    assert states["good"].status == "ok"


def test_blocked_provider_is_paused_and_skipped(config, session_factory, session):
    only_route(session)
    blocked = FakeProvider("blocked", error=ProviderBlockedError("captcha"))
    svc = service(config, session_factory, {"blocked": blocked})
    svc.run(force=True)
    session.expire_all()
    state = session.get(ProviderState, "blocked")
    assert state.status == "blocked" and state.paused_until > utcnow()
    svc.run(force=True)
    assert blocked.calls == 1  # not called again while paused
    statuses = [r.status for r in session.scalars(select(SearchRun).order_by(SearchRun.id))]
    assert statuses == ["blocked", "skipped"]
    provider_health.resume(session, "blocked")
    svc.run(force=True)
    assert blocked.calls == 2


def test_disabled_provider_not_used(config, session_factory, session):
    only_route(session)
    p = FakeProvider()
    settings_service.set_provider_enabled(session, "fake", False)
    service(config, session_factory, {"fake": p}).run(force=True)
    assert p.calls == 0


def test_due_logic_and_no_repeat(config, session_factory, session):
    only_route(session)
    p = FakeProvider()
    svc = service(config, session_factory, {"fake": p})
    svc.run(trigger="scheduled")
    calls = p.calls
    summary = svc.run(trigger="scheduled")  # not due again yet
    assert p.calls == calls and summary.skipped_reason == "no routes are due"
    cfg = session.get(SearchConfiguration, 1)
    now = utcnow()
    assert not is_due(cfg, now)
    assert is_due(cfg, now + timedelta(hours=cfg.frequency_hours, minutes=1))
    cfg.enabled = False
    assert not is_due(cfg, now + timedelta(days=1))


def test_failed_route_stays_due(config, session_factory, session):
    only_route(session)
    svc = service(config, session_factory, {"bad": FakeProvider("bad", error=ProviderTemporaryError("x"))})
    svc.run(trigger="scheduled")
    session.expire_all()
    assert session.get(SearchConfiguration, 1).last_run_at is None  # retried on next tick


def test_travelpayouts_end_to_end_with_mock_http(config, session_factory, session):
    """Real adapter + mocked HTTP returning TEST DATA; repeated run hits the cache."""
    from app.database.db import DbResponseCache

    only_route(session)
    payload = {"success": True, "currency": "eur", "data": [
        tp_item(future(5, 7), future(12, 18, offset_hours=1), 49),
        tp_item(future(6, 7), future(7, 18, offset_hours=1), 30),  # 1-day stay: filtered out
    ]}
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=payload)

    tp = TravelpayoutsProvider("TEST-TOKEN", transport=httpx.MockTransport(handler), sleep=lambda s: None,
                               min_interval_seconds=0, cache=DbResponseCache(session_factory), cache_ttl_seconds=600)
    svc = service(config, session_factory, {"travelpayouts": tp})
    s1 = svc.run(force=True, today=date.today())
    n_requests = calls["n"]
    assert n_requests >= 2  # one per month in the window
    offers = list(session.scalars(select(FlightOffer)))
    assert len(offers) == 1 and offers[0].price == 49 and offers[0].price_type == "indicative"
    assert s1.alerts_created == 1
    observations = session.scalar(select(func.count(PriceHistory.id)))
    # second run within cache TTL: no new HTTP requests, no fake "new observation"
    svc.run(force=True, today=date.today())
    assert calls["n"] == n_requests
    assert session.scalar(select(func.count(PriceHistory.id))) == observations


def test_scheduler_job_configuration(config, session_factory):
    svc = service(config, session_factory, {"fake": FakeProvider()})
    sched = build_scheduler(svc, 30)
    job = sched.get_job(JOB_ID)
    assert job is not None and job.trigger.interval == timedelta(minutes=30)
    assert job.coalesce is True and job.max_instances == 1


def test_scheduled_tick_survives_exceptions():
    class Boom:
        def run(self, **kw):
            raise RuntimeError("bug")

    scheduled_tick(Boom())  # must not raise
