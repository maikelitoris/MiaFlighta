"""Runs searches: for every due route x enabled provider, query, filter, store,
then evaluate deals and notify. Providers fail independently."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import AppConfig
from app.core.errors import (
    ProviderAuthError,
    ProviderBlockedError,
    ProviderError,
    ProviderNotConfiguredError,
)
from app.core.timeutils import utcnow
from app.models.db_models import DailyFare, SearchConfiguration, SearchRun
from app.notifications.telegram import Notifier
from app.providers.base import FlightProvider
from app.services import provider_health, settings_service
from app.services.alert_service import process_alerts
from app.services.offer_store import save_offers
from app.services.search_planner import offer_matches, plan_queries

log = logging.getLogger(__name__)

_run_lock = threading.Lock()  # never run two searches at once (scheduler + manual)


@dataclass
class RunSummary:
    configs_searched: int = 0
    runs: int = 0
    offers_seen: int = 0
    new_offers: int = 0
    requests_made: int = 0
    alerts_sent: int = 0
    alerts_created: int = 0
    errors: list[str] = field(default_factory=list)
    skipped_reason: str | None = None

    def text(self) -> str:
        if self.skipped_reason:
            return f"Search skipped: {self.skipped_reason}"
        parts = [
            f"{self.configs_searched} routes searched",
            f"{self.offers_seen} offers seen ({self.new_offers} new)",
            f"{self.requests_made} API requests",
            f"{self.alerts_created} alerts ({self.alerts_sent} sent)",
        ]
        if self.errors:
            parts.append(f"{len(self.errors)} errors")
        return ", ".join(parts)


def is_due(cfg: SearchConfiguration, now: datetime) -> bool:
    if not cfg.enabled:
        return False
    if cfg.last_run_at is None:
        return True
    return cfg.last_run_at + timedelta(hours=cfg.frequency_hours) <= now


class SearchService:
    def __init__(
        self,
        config: AppConfig,
        session_factory: sessionmaker[Session],
        providers: dict[str, FlightProvider],
        notifier: Notifier,
    ) -> None:
        self.config = config
        self.session_factory = session_factory
        self.providers = providers
        self.notifier = notifier

    def run(
        self,
        *,
        trigger: str = "manual",
        config_ids: list[int] | None = None,
        force: bool = False,
        today: date | None = None,
    ) -> RunSummary:
        if not _run_lock.acquire(blocking=False):
            return RunSummary(skipped_reason="another search is already running")
        try:
            return self._run(trigger=trigger, config_ids=config_ids, force=force, today=today or date.today())
        finally:
            _run_lock.release()

    def _run(self, *, trigger: str, config_ids: list[int] | None, force: bool, today: date) -> RunSummary:
        summary = RunSummary()
        seen_offer_ids: list[int] = []
        with self.session_factory() as session:
            now = utcnow()
            configs = settings_service.enabled_configs(session)
            if config_ids is not None:
                configs = [c for c in configs if c.id in set(config_ids)]
            if not force:
                configs = [c for c in configs if is_due(c, now)]
            if not configs:
                summary.skipped_reason = "no routes are due"
                return summary

            budgets = {name: self.config.provider(name).max_requests_per_run for name in self.providers}
            self._fares_saved: set[tuple] = set()  # daily fares already stored in this run
            for cfg in configs:
                any_success = False
                for name, provider in self.providers.items():
                    if not settings_service.provider_enabled(session, self.config, name):
                        continue
                    ok = self._run_provider(session, cfg, name, provider, trigger, today, budgets, summary, seen_offer_ids)
                    any_success = any_success or ok
                summary.configs_searched += 1
                if any_success:
                    cfg.last_run_at = utcnow()
                    session.commit()

            try:
                alerts = process_alerts(session, self.config, seen_offer_ids, self.notifier)
                summary.alerts_created = len(alerts)
                summary.alerts_sent = sum(a.notification_status == "sent" for a in alerts)
            except Exception as exc:  # alerts must never break data collection
                log.exception("Alert processing failed")
                summary.errors.append(f"alerts: {exc}")
        log.info("Search finished (%s): %s", trigger, summary.text())
        return summary

    def _save_daily_fares(self, session: Session, fares: list, run_id: int) -> None:
        """Append fare-calendar observations (once per day/direction per run)."""
        now = utcnow()
        for f in fares:
            key = (f.provider, f.origin, f.destination, f.day)
            if key in self._fares_saved:
                continue
            self._fares_saved.add(key)
            session.add(DailyFare(provider=f.provider, origin=f.origin, destination=f.destination, day=f.day,
                                  price=f.price, currency=f.currency, departure_at=f.departure_at,
                                  arrival_at=f.arrival_at, flight_number=f.flight_number, observed_at=now,
                                  search_run_id=run_id))
        session.commit()

    def _run_provider(
        self,
        session: Session,
        cfg: SearchConfiguration,
        name: str,
        provider: FlightProvider,
        trigger: str,
        today: date,
        budgets: dict[str, int],
        summary: RunSummary,
        seen_offer_ids: list[int],
    ) -> bool:
        run = SearchRun(provider=name, search_configuration_id=cfg.id, trigger=trigger, started_at=utcnow())
        session.add(run)
        session.commit()
        errors: list[str] = []
        try:
            if provider_health.is_paused(session, name):
                run.status = "skipped"
                errors.append("provider paused after a previous block/denial — see Settings")
                return False
            if not provider.is_configured():
                raise ProviderNotConfiguredError(f"{name} is not configured (missing API token)")

            queries = plan_queries(cfg, today, currency=self.config.currency,
                                   month_search=provider.supports_month_search)
            for query in queries:
                if budgets[name] <= 0:
                    errors.append("request budget for this run exhausted; remaining queries skipped")
                    break
                result = provider.search_flights(query)
                budgets[name] -= result.requests_made
                run.requests_made += result.requests_made
                run.cache_hits += int(result.from_cache)
                errors.extend(result.warnings)
                matching = [o for o in result.offers if offer_matches(cfg, o, today)]
                saved = save_offers(
                    session, matching, search_run_id=run.id, search_configuration_id=cfg.id,
                    record_history=not result.from_cache,
                )
                run.results_count += len(saved.offer_ids)
                run.new_offers += saved.new
                if not result.from_cache:
                    seen_offer_ids.extend(saved.offer_ids)
                    self._save_daily_fares(session, result.daily_fares, run.id)
            run.status = "success"
            provider_health.record_success(session, name)
            return True
        except ProviderBlockedError as exc:
            run.status = "blocked"
            errors.append(str(exc))
            provider_health.record_failure(session, name, str(exc), status="blocked",
                                           pause_hours=self.config.scheduler.blocked_pause_hours)
        except ProviderNotConfiguredError as exc:
            run.status = "failed"
            errors.append(str(exc))
            provider_health.record_failure(session, name, str(exc), status="not_configured")
        except ProviderAuthError as exc:
            run.status = "failed"
            errors.append(str(exc))
            provider_health.record_failure(session, name, str(exc), status="auth_error")
        except ProviderError as exc:
            run.status = "failed"
            errors.append(str(exc))
            provider_health.record_failure(session, name, str(exc))
        except Exception as exc:  # unexpected bug in a provider must not stop others
            log.exception("Unexpected error in provider %s", name)
            run.status = "failed"
            errors.append(f"unexpected: {exc!r}")
            provider_health.record_failure(session, name, repr(exc))
        finally:
            run.completed_at = utcnow()
            run.errors = "\n".join(errors) or None
            session.commit()
            summary.runs += 1
            summary.offers_seen += run.results_count
            summary.new_offers += run.new_offers
            summary.requests_made += run.requests_made
            if run.status not in ("success", "skipped"):
                summary.errors.append(f"{cfg.label} [{name}]: {run.errors}")
                log.warning("Search %s via %s: %s", cfg.label, name, run.errors)
        return False
