"""APScheduler setup. A job ticks every `tick_minutes`; each tick runs only the
routes whose own frequency says they are due, so restarting the app or changing
frequencies never causes duplicate searches, and missed runs are caught up."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.schedulers.blocking import BlockingScheduler

from app.services.search_service import SearchService

log = logging.getLogger(__name__)

JOB_ID = "flight_radar_tick"


def scheduled_tick(service: SearchService, after_tick: Callable[[], object] | None = None) -> None:
    try:
        summary = service.run(trigger="scheduled")
        if summary.skipped_reason:
            log.debug("Scheduled tick: %s", summary.skipped_reason)
    except Exception:  # keep the scheduler alive no matter what
        log.exception("Scheduled search crashed; will retry on next tick")
    if after_tick is not None:
        try:
            after_tick()  # e.g. the 12-hour Telegram digest, sent after fresh results
        except Exception:
            log.exception("Post-search hook failed; will retry on next tick")


def build_scheduler(service: SearchService, tick_minutes: float, *, blocking: bool = False,
                    after_tick: Callable[[], object] | None = None) -> BaseScheduler:
    scheduler: BaseScheduler = BlockingScheduler() if blocking else BackgroundScheduler()
    scheduler.add_job(
        scheduled_tick,
        "interval",
        args=[service, after_tick],
        minutes=tick_minutes,
        id=JOB_ID,
        next_run_time=datetime.now(),  # first check right at startup
        coalesce=True,  # if the PC slept, run once, not N times
        max_instances=1,
        misfire_grace_time=3600,
        replace_existing=True,
    )
    return scheduler
