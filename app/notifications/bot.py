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
from app.services import reports

log = logging.getLogger(__name__)

HELP = (
    "<b>Flight Radar commands</b>\n"
    "/status – system and provider health\n"
    "/deals – best current offers\n"
    "/routes – monitored routes\n"
    "/budget – show budget · /budget 120 – set global · /budget 3 90 – set route #3\n"
    "/search – run a search now\n"
    "/summary – the 12-hour price summary, now\n"
    "/help – this message\n\n"
    "<i>Prices come from cached provider data and are indicative. Always verify on the booking site.</i>"
)


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
        await reply(update, "✈️ Flight Radar is running. You'll get a price summary every 12 hours and an instant message when a good deal appears.\n\n" + HELP)

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

    @authorized
    async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        from app.services.digest import build_digest

        await reply(update, await asyncio.to_thread(with_session, build_digest))

    app.add_handler(CommandHandler("summary", summary_cmd))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("deals", deals))
    app.add_handler(CommandHandler("routes", routes))
    app.add_handler(CommandHandler("budget", budget))
    app.add_handler(CommandHandler("search", search))
    return app
