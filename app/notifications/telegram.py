"""Telegram notifications via the official Bot API (python-telegram-bot)."""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass
from typing import Protocol

from app.core.airlines import airline_name
from app.models.db_models import Airport, FlightOffer
from app.services.deal_engine import DealAssessment

log = logging.getLogger(__name__)

PROVIDER_LABELS = {"travelpayouts": "Travelpayouts / Aviasales Data API (cached prices)"}
TELEGRAM_MAX_LEN = 4096


@dataclass
class SendResult:
    status: str  # sent | failed | not_configured
    error: str | None = None


class Notifier(Protocol):
    def send(self, text: str) -> SendResult: ...


def money(amount: float, currency: str) -> str:
    symbol = {"EUR": "€", "PLN": "zł ", "USD": "$", "GBP": "£"}.get(currency.upper())
    return f"{symbol}{amount:,.2f}" if symbol else f"{amount:,.2f} {currency}"


def fmt_dt(dt) -> str:
    return dt.strftime("%a %d %b %Y, %H:%M") if dt else "unknown"


def stops_text(stops: int | None) -> str:
    if stops is None:
        return "unknown"
    return "Direct" if stops == 0 else f"{stops} stop{'s' if stops > 1 else ''}"


def place(airport: Airport | None, code: str) -> str:
    return f"{airport.city} ({code})" if airport else code


def format_deal_message(
    offer: FlightOffer,
    assessment: DealAssessment,
    origin: Airport | None = None,
    destination: Airport | None = None,
    transfer_note: str | None = None,
) -> str:
    e = html.escape
    title = "EXCELLENT FLIGHT DEAL" if assessment.category == "excellent" else "GOOD FLIGHT DEAL"
    kind = "return" if offer.trip_type == "return" else "one-way"
    pax = f"{offer.passengers} passenger{'s' if offer.passengers > 1 else ''}"
    lines = [
        f"✈️ <b>{title}</b>",
        "",
        f"<b>{e(place(origin, offer.origin))} → {e(place(destination, offer.destination))}</b>",
        "",
        f"Price: <b>{money(offer.price, offer.currency)}</b> {kind} total ({pax})",
    ]
    if offer.price_type == "indicative":
        lines.append("⚠️ <i>Indicative cached price — may have changed. Verify before booking.</i>")
    if assessment.transfer_cost_pp:
        note = f" ({e(transfer_note)})" if transfer_note else ""
        lines.append(
            f"+ est. ground transfer: {money(assessment.transfer_cost_pp, offer.currency)} pp{note}"
        )
        lines.append(f"Effective price: {money(assessment.effective_price_pp, offer.currency)} pp")
    flight = airline_name(offer.airline)
    if offer.flight_number:
        flight += f" · flight {e(offer.flight_number)}"
    lines += [
        f"Airline: {e(flight)}",
        f"Stops: {stops_text(offer.stops)}"
        + (f" / return: {stops_text(offer.return_stops)}" if offer.trip_type == "return" else ""),
        f"Baggage: {e(offer.baggage_info or ('included' if offer.baggage_included else 'unknown'))}",
        "",
        f"Departure: {fmt_dt(offer.departure_at)} (local)",
    ]
    if offer.return_departure_at:
        lines.append(f"Return: {fmt_dt(offer.return_departure_at)} (local)")
    lines.append("")
    b = assessment.baseline
    if b is not None:
        lines.append(f"Comparable median ({b.samples} offers, {b.lookback_days}d): {money(b.median, offer.currency)}")
        lines.append(f"Historical average: {money(b.average, offer.currency)}")
        if assessment.discount_pct is not None:
            lines.append(f"Price difference: {-assessment.discount_pct:+.0f}%")
    else:
        lines.append(
            f"History: not enough comparable data yet ({assessment.comparable_samples} offers) — "
            "no historical comparison made."
        )
    lines += [
        "",
        f"Deal score: <b>{assessment.score}/100</b>",
        f"<i>{e('; '.join(assessment.reasons) or 'no bonus rules matched')}</i>",
        "",
        f"Source: {e(PROVIDER_LABELS.get(offer.provider, offer.provider))}",
        f"Last verified: {offer.collected_at.strftime('%d %b %Y %H:%M')} UTC",
    ]
    if offer.booking_url:
        lines.append(f'<a href="{e(offer.booking_url, quote=True)}">Open offer</a>')
    return "\n".join(lines)[:TELEGRAM_MAX_LEN]


class TelegramNotifier:
    """Sends messages to one chat. Safe to call from non-async code/threads."""

    def __init__(self, bot_token: str | None, chat_id: str | None) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def _send_async(self, text: str) -> None:
        from telegram import Bot, LinkPreviewOptions
        from telegram.constants import ParseMode

        async with Bot(self.bot_token) as bot:
            await bot.send_message(
                chat_id=self.chat_id, text=text, parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )

    def send(self, text: str) -> SendResult:
        if not self.configured:
            return SendResult("not_configured", "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        try:
            asyncio.run(self._send_async(text))
        except Exception as exc:  # network errors, invalid chat id, etc.
            log.error("Telegram send failed: %s", exc)
            return SendResult("failed", str(exc)[:500])
        return SendResult("sent")
