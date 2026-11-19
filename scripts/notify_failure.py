"""Sends a short Telegram message when the cloud job fails, with a link to its log.
Used by .github/workflows/flight-radar.yml. Never raises."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    try:
        from app.notifications.telegram import TelegramNotifier

        n = TelegramNotifier(os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID"))
        url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
               f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}")
        r = n.send("⚠️ <b>Flight Radar cloud run had a problem</b>\n"
                   "Prices may not have been checked this time. The next run is in about 12 h.\n"
                   f'<a href="{url}">Open the run log</a>')
        print(f"Telegram: {r.status} {r.error or ''}")
    except Exception as exc:  # the failure notice must never fail the job itself
        print(f"Could not send the failure notice: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
