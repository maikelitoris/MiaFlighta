"""APScheduler setup. A job ticks every `tick_minutes`; each tick runs only the
routes whose own frequency says they are due, so restarting the app or changing
frequencies never causes duplicate searches, and missed runs are caught up."""

from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.schedulers.blocking import BlockingScheduler

from app.core.timeutils import utcnow
from app.services import digest, settings_service, weekend_finder
from app.services.search_service import SearchService

log = logging.getLogger(__name__)

JOB_ID = "flight_radar_tick"


def scheduled_tick(service: SearchService, *, force: bool = False):
    """One scheduler cycle: search due routes, then the Telegram update and the weekly
    report. force=True searches every enabled route (used by the cloud job, whose
    cron schedule already decides when a run is due). Returns the run summary."""
    summary = None
    try:
        summary = service.run(trigger="scheduled", force=force)
        if summary.skipped_reason:
            log.debug("Scheduled tick: %s", summary.skipped_reason)
    except Exception:  # keep the scheduler alive no matter what
        log.exception("Scheduled search crashed; will retry on next tick")
    # Telegram messages are independent of each other and of the search result:
    # a failure in one must never stop the next tick.
    try:
        if summary is not None and summary.configs_searched > 0:
            with service.session_factory() as session:
                if digest.digest_enabled(session, service.config):
                    digest.send_digest(session, service.config, service.notifier)
    except Exception:
        log.exception("Sending the price digest failed")
    try:
        run_weekend_finder(service, force=force)
    except Exception:
        log.exception("Weekend finder failed")
    try:
        with service.session_factory() as session:
            digest.maybe_send_weekly(session, service.config, service.notifier)
    except Exception:
        log.exception("Sending the weekly report failed")
    return summary


def run_weekend_finder(service: SearchService, *, force: bool = False):
    """Searches all weekends in the finder window (when enabled and due) and sends
    the two finder messages. Returns the FinderRun, or None when not run."""
    cfg = service.config.weekend_finder
    if not cfg.enabled:
        return None
    provider = service.providers.get("ryanair")
    with service.session_factory() as session:
        if provider is not None and not settings_service.provider_enabled(session, service.config, "ryanair"):
            provider = None
        # A long window (e.g. a whole year) has its own, slower interval; `force` from the
        # cloud job only overrides the normal interval, not that one.
        hours = cfg.every_hours or digest.search_every_hours(session, service.config)
        if (cfg.every_hours or not force) and not weekend_finder.is_due(session, hours, utcnow()):
            return None
        result = weekend_finder.run_finder(session, service.config, provider)
        if result.skipped_reason:
            log.info("Weekend finder skipped: %s", result.skipped_reason)
            return result
        if cfg.notify and digest.digest_enabled(session, service.config):
            messages = weekend_finder.finder_messages(session, service.config, provider)
            if messages:
                r = digest.send_blocks(service.notifier, messages)
                log.info("Telegram weekend finder: %s %s (%d messages)", r.status, r.error or "", len(messages))
        weekend_finder.finish_run(session, service.config)
        return result


def build_scheduler(service: SearchService, tick_minutes: float, *, blocking: bool = False) -> BaseScheduler:
    scheduler: BaseScheduler = BlockingScheduler() if blocking else BackgroundScheduler()
    scheduler.add_job(
        scheduled_tick,
        "interval",
        args=[service],
        minutes=tick_minutes,
        id=JOB_ID,
        next_run_time=datetime.now(),  # first check right at startup
        coalesce=True,  # if the PC slept, run once, not N times
        max_instances=1,
        misfire_grace_time=3600,
        replace_existing=True,
    )
    return scheduler
