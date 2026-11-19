"""Shared helpers for dashboard pages."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from app.context import AppContext, build_context  # noqa: E402
from app.core.airlines import airline_name  # noqa: E402
from app.core.logging_setup import setup_logging  # noqa: E402
from app.models.db_models import FlightOffer  # noqa: E402
from app.services.deal_engine import DealAssessment  # noqa: E402

DISCLAIMER = (
    "Prices come from the enabled providers only (Ryanair's own fare finder and/or cached Aviasales data via "
    "Travelpayouts). "
    "They are **indicative** and may have changed — always verify on the booking site. "
    "Not all airlines are covered."
)


@st.cache_resource
def get_context() -> AppContext:
    setup_logging()
    return build_context()


def page_setup(title: str) -> AppContext:
    st.set_page_config(page_title=f"Flight Radar · {title}", page_icon="✈️", layout="wide")
    st.title(f"✈️ {title}")
    return get_context()


def stops_label(stops: int | None) -> str:
    if stops is None:
        return "?"
    return "direct" if stops == 0 else f"{stops} stop{'s' if stops > 1 else ''}"


def offers_frame(offers: list[FlightOffer], assessments: dict[int, DealAssessment] | None = None) -> pd.DataFrame:
    rows = []
    for o in offers:
        a = (assessments or {}).get(o.id)
        row = {
            "Route": f"{o.origin}→{o.destination}",
            "Type": "return" if o.trip_type == "return" else "one-way",
            "Price": o.price,
            "Currency": o.currency,
            "Departure": o.departure_at,
            "Return": o.return_departure_at,
            "Stay (days)": o.stay_days,
            "Airline": airline_name(o.airline),
            "Flight": o.flight_number or "",
            "Stops": stops_label(o.stops) + (f" / {stops_label(o.return_stops)}" if o.trip_type == "return" else ""),
            "Baggage": "included" if o.baggage_included else ("no" if o.baggage_included is False else "unknown"),
            "Price type": o.price_type,
            "Provider": o.provider,
            "Last seen (UTC)": o.collected_at,
            "Link": o.booking_url,
        }
        if a is not None:
            row = {"Deal": a.category, "Score": a.score, "Effective pp": a.effective_price_pp} | row
        rows.append(row)
    return pd.DataFrame(rows)


LINK_COLUMNS = {
    "Link": st.column_config.LinkColumn("Link", display_text="open"),
    "Price": st.column_config.NumberColumn("Price", format="%.2f"),
    "Effective pp": st.column_config.NumberColumn("Effective pp", format="%.2f",
                                                  help="Per passenger, incl. estimated ground transfer"),
    "Departure": st.column_config.DatetimeColumn("Departure", format="ddd DD MMM YYYY HH:mm"),
    "Return": st.column_config.DatetimeColumn("Return", format="ddd DD MMM YYYY HH:mm"),
}
