"""Keeps `python main.py run` alive: if it stops for any reason (crash, network
problem at startup, Telegram outage), it is restarted after a short pause.

    python scripts/keep_running.py

Use it with the Windows auto-start task (scripts/windows/install_autostart.ps1)
or any other startup mechanism. Stop it with Ctrl+C, or end the scheduled task.
The restart pause grows when the app keeps failing quickly, so a broken setup
does not loop at full speed. Restarts are logged in data/logs/keep_running.log,
crash tracebacks in data/logs/crash.log.
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "data" / "logs" / "keep_running.log"
MIN_PAUSE, MAX_PAUSE = 30, 15 * 60
HEALTHY_SECONDS = 10 * 60  # ran this long = it was working; reset the pause


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> int:
    cmd = [sys.executable, str(ROOT / "main.py"), "run", *sys.argv[1:]]
    pause = MIN_PAUSE
    log(f"supervisor started: {' '.join(cmd)}")
    while True:
        started = time.monotonic()
        try:
            # The app writes its own log (data/logs/flight_radar.log). Console output and
            # crash tracebacks go to crash.log, because a hidden window would lose them.
            with (LOG.parent / "crash.log").open("a", encoding="utf-8") as err:
                code = subprocess.call(cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=err)
        except KeyboardInterrupt:
            log("stopped by user")
            return 0
        ran = time.monotonic() - started
        pause = MIN_PAUSE if ran >= HEALTHY_SECONDS else min(MAX_PAUSE, pause * 2)
        log(f"flight radar exited with code {code} after {ran / 60:.1f} min; restarting in {pause} s")
        try:
            time.sleep(pause)
        except KeyboardInterrupt:
            log("stopped by user")
            return 0


if __name__ == "__main__":
    sys.exit(main())
