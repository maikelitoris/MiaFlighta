"""Telegram command bot (long polling — no server or webhook needed).

Only the chat in TELEGRAM_CHAT_ID may use commands. Anyone else who sends /start
is told their chat id (useful for first-time setup) and nothing more.
"""

from __future__ import annotations

import asyncio
import logging
from functools import wraps

from telegram import LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from app.context import AppContext
from app.services import digest, reports

log = logging.getLogger(__name__)

HELP = (
    "<b>Flight Radar commands</b>\n"
    "/status – system and provider health\n"
    "/deals – best current offers\n"
    "/trips – latest price for every watched trip\n"
    "/report – weekly report (best prices of the last 7 days) now\n"
    "/routes – monitored routes\n"
    "/budget – show budget · /budget 120 – set global · /budget 3 90 – set route #3\n"
    "/search – run a search now\n"
    "/help – this message\n\n"
    "<i>Prices come from cached provider data and are indicative. Always verify on the booking site.</i>"
)


def _joined(session, config, build) -> list[str]:
    """Builds digest/report blocks and packs them into Telegram-sized messages."""
    messages: list[str] = []

    class Collect:
        def send(self, text):
            messages.append(text)
            return digest.SendResult("sent")

    digest.send_blocks(Collect(), build(session, config))
    return messages


def build_bot(ctx: AppContext) -> Application:
    token = ctx.config.secrets.telegram_bot_token
    allowed_chat = (ctx.config.secrets.telegram_chat_id or "").strip()
    app = Application.builder().token(token).build()

    async def reply(update: Update, text: str) -> None:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True)
        )

    def authorized(handler):
        @wraps(handler)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            chat_id = str(update.effective_chat.id)
            if chat_id != allowed_chat:
                log.warning("Ignoring command from unauthorized chat %s", chat_id)
                return
            return await handler(update, context)

        return wrapper

    def with_session(fn, *args):
        with ctx.session_factory() as session:
            return fn(session, ctx.config, *args)

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        if chat_id != allowed_chat:
            await reply(update, f"Hi! Your chat id is <code>{chat_id}</code>.\n"
                                "Put it in TELEGRAM_CHAT_ID in your .env file and restart Flight Radar.")
            return
        await reply(update, "✈️ Flight Radar is running. You'll get the latest trip prices after every search, "
                            "a weekly report, and an alert when a good deal appears.\n\n" + HELP)

    @authorized
    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await reply(update, HELP)

    @authorized
    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await reply(update, await asyncio.to_thread(with_session, reports.status_text))

    @authorized
    async def deals(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await reply(update, await asyncio.to_thread(with_session, reports.deals_text))

    @authorized
    async def routes(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await reply(update, await asyncio.to_thread(with_session, reports.routes_text))

    @authorized
    async def budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await reply(update, await asyncio.to_thread(with_session, reports.budget_command, list(context.args or [])))

    @authorized
    async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await reply(update, "🔎 Searching all enabled routes… this can take a minute.")
        summary = await asyncio.to_thread(ctx.search_service.run, trigger="manual", force=True)
        await reply(update, "Done: " + summary.text())

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("deals", deals))
    @authorized
    async def trips_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        for text in await asyncio.to_thread(with_session, _joined, digest.digest_blocks):
            await reply(update, text)

    @authorized
    async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
        for text in await asyncio.to_thread(with_session, _joined, digest.weekly_blocks):
            await reply(update, text)

    app.add_handler(CommandHandler("trips", trips_cmd))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("routes", routes))
    app.add_handler(CommandHandler("budget", budget))
    app.add_handler(CommandHandler("search", search))
    return app
