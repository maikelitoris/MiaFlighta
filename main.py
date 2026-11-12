"""Flight Radar command-line entry point.

    python main.py init              create the database and seed routes from config.yaml
    python main.py search            run a manual search now (all enabled routes)
    python main.py run               scheduler + Telegram bot (keep this running)
    python main.py dashboard         open the Streamlit dashboard
    python main.py status            print status
    python main.py check             check provider credentials / connectivity
    python main.py telegram-test     send a test Telegram message
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.core.logging_setup import setup_logging  # noqa: E402

log = logging.getLogger("flight_radar")


def _plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def cmd_init(args) -> int:
    from app.context import build_context

    ctx = build_context(sync_routes=args.sync_routes)
    print(f"Database ready at {ctx.config.database_url}")
    from app.services import reports

    with ctx.session_factory() as s:
        print(_plain(reports.routes_text(s, ctx.config)))
    return 0


def cmd_search(args) -> int:
    from app.context import build_context

    ctx = build_context()
    ids = [args.route] if args.route else None
    summary = ctx.search_service.run(trigger="manual", config_ids=ids, force=True)
    print(summary.text())
    for e in summary.errors:
        print(" -", e)
    return 0 if not summary.errors else 1


def cmd_status(args) -> int:
    from app.context import build_context
    from app.services import reports

    ctx = build_context()
    with ctx.session_factory() as s:
        print(_plain(reports.status_text(s, ctx.config)))
        print()
        print(_plain(reports.deals_text(s, ctx.config)))
    return 0


def cmd_check(args) -> int:
    from app.context import build_context

    ctx = build_context()
    ok = True
    for name, provider in ctx.providers.items():
        h = provider.health_check()
        ok = ok and h.ok
        print(f"{name}: {h.status} — {h.message}")
    print(f"telegram: {'configured' if ctx.notifier.configured else 'not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)'}")
    return 0 if ok else 1


def cmd_telegram_test(args) -> int:
    from app.context import build_context

    ctx = build_context()
    result = ctx.notifier.send(
        "✅ <b>Flight Radar test message</b>\nTelegram notifications are working. "
        "<i>(This is a test, not a flight offer.)</i>"
    )
    print(f"Telegram: {result.status}" + (f" — {result.error}" if result.error else ""))
    return 0 if result.status == "sent" else 1


def cmd_run(args) -> int:
    from app.context import build_context
    from app.scheduler.scheduler import build_scheduler

    ctx = build_context()
    tick = ctx.config.scheduler.tick_minutes
    print("Flight Radar is running. Keep this window open and the computer awake; "
          "searches only happen while this process runs. Ctrl+C to stop.")
    if ctx.notifier.configured and not args.no_bot:
        from app.notifications.bot import build_bot

        scheduler = build_scheduler(ctx.search_service, tick)
        scheduler.start()
        log.info("Scheduler started (checks every %s min); starting Telegram bot", tick)
        try:
            build_bot(ctx).run_polling(drop_pending_updates=True)
        finally:
            scheduler.shutdown(wait=False)
    else:
        if not ctx.notifier.configured:
            log.warning("Telegram is not configured: running scheduler only (no notifications/commands).")
        scheduler = build_scheduler(ctx.search_service, tick, blocking=True)
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            pass
    return 0


def cmd_dashboard(args) -> int:
    env = os.environ | {"PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    script = ROOT / "app" / "dashboard" / "Overview.py"
    cmd = [sys.executable, "-m", "streamlit", "run", str(script), "--server.port", str(args.port),
           "--browser.gatherUsageStats", "false"]
    return subprocess.call(cmd, env=env, cwd=ROOT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flight-radar", description="Personal flight price monitor")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init", help="create database and seed routes")
    p.add_argument("--sync-routes", action="store_true", help="add routes newly listed in config.yaml")
    p.set_defaults(func=cmd_init)
    p = sub.add_parser("search", help="run a manual search now")
    p.add_argument("--route", type=int, help="only this route id (see `status`/dashboard)")
    p.set_defaults(func=cmd_search)
    sub.add_parser("status", help="print status and best offers").set_defaults(func=cmd_status)
    sub.add_parser("check", help="check provider credentials").set_defaults(func=cmd_check)
    sub.add_parser("telegram-test", help="send a test Telegram message").set_defaults(func=cmd_telegram_test)
    p = sub.add_parser("run", help="run scheduler + Telegram bot")
    p.add_argument("--no-bot", action="store_true", help="scheduler only, no Telegram commands")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("dashboard", help="start the Streamlit dashboard")
    p.add_argument("--port", type=int, default=8501)
    p.set_defaults(func=cmd_dashboard)

    args = parser.parse_args(argv)
    setup_logging()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
