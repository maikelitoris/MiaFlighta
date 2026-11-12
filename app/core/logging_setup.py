"""Logging to console and a rotating file in data/logs/."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from app.core.config import PROJECT_ROOT

_configured = False


def setup_logging(level: str | None = None) -> None:
    global _configured
    if _configured:
        return
    level = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    log_dir = PROJECT_ROOT / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    file_handler = RotatingFileHandler(
        log_dir / "flight_radar.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(file_handler)
    # httpx logs full request URLs at INFO; keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    _configured = True
