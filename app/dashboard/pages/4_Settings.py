"""Page 4 — Settings: budgets, routes, airports, frequency, Telegram, providers."""

from datetime import date

import pandas as pd
import streamlit as st
from common import page_setup
from sqlalchemy import func, select

from app.models.db_models import Airport, FlightOffer, ProviderState, SearchConfiguration
from app.services import provider_health, settings_service

ctx = page_setup("Settings")
cfg = ctx.config
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# ------------------------------------------------------------------ budget & frequency
st.subheader("Budget and frequency")
with ctx.session_factory() as s:
    budget = settings_service.get_budget(s, cfg)
    freqs = [c.frequency_hours for c in s.scalars(select(SearchConfiguration))]
c1, c2 = st.columns(2)
with c1.form("budget"):
    new_budget = st.number_input(f"Global max price per person ({cfg.currency})", min_value=1.0, value=float(budget),
                                 step=5.0, help="Total trip price per passenger incl. ground transfer estimate. "
                                                "Routes can override it below.")
    if st.form_submit_button("Save budget"):
        with ctx.session_factory() as s:
            settings_service.set_budget(s, new_budget)
        st.success("Budget saved.")
with c2.form("freq"):
    new_freq = st.number_input("Search every N hours (all routes)", min_value=1.0, max_value=168.0,
                               value=float(freqs[0] if freqs else 6), step=1.0)
    if st.form_submit_button("Apply to all routes"):
        with ctx.session_factory() as s:
            settings_service.set_all_frequencies(s, new_freq)
        st.success("Frequency updated.")
st.caption("Automatic searches only run while `python main.py run` is running on this computer.")

# ------------------------------------------------------------------ notifications & providers
st.subheader("Telegram notifications")
with ctx.session_factory() as s:
    tg_on = settings_service.telegram_enabled(s)
if not ctx.notifier.configured:
    st.warning("Telegram is not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in `.env` and restart.")
new_tg = st.toggle("Send deal alerts to Telegram", value=tg_on)
if new_tg != tg_on:
    with ctx.session_factory() as s:
        settings_service.set_setting(s, settings_service.TELEGRAM_ENABLED, new_tg)
    st.rerun()
if ctx.notifier.configured and st.button("Send test message"):
    r = ctx.notifier.send("✅ <b>Flight Radar test message</b> (not a flight offer)")
    (st.success if r.status == "sent" else st.error)(f"Telegram: {r.status} {r.error or ''}")

st.subheader("Data providers")
with ctx.session_factory() as s:
    states = {p.provider: p for p in s.scalars(select(ProviderState))}
    for name, provider in ctx.providers.items():
        state = states.get(name)
        col_a, col_b, col_c = st.columns([2, 2, 1])
        col_a.markdown(f"**{name}** — {'configured' if provider.is_configured() else '⚠️ missing token'}  \n"
                       f"<small>{provider.coverage_note}</small>", unsafe_allow_html=True)
        enabled = settings_service.provider_enabled(s, cfg, name)
        new_enabled = col_b.toggle("Enabled", value=enabled, key=f"prov_{name}",
                                   disabled=not cfg.provider(name).enabled)
        if new_enabled != enabled:
            settings_service.set_provider_enabled(s, name, new_enabled)
            st.rerun()
        if state and state.paused_until:
            col_b.error(f"Paused until {state.paused_until:%d %b %H:%M} UTC: {state.last_error}")
            if col_c.button("Resume", key=f"resume_{name}",
                            help="Only resume after checking why it was blocked. No bypass is attempted."):
                provider_health.resume(s, name)
                st.rerun()

# ------------------------------------------------------------------ routes
st.subheader("Routes (search configurations)")
with ctx.session_factory() as s:
    configs = list(s.scalars(select(SearchConfiguration).order_by(SearchConfiguration.id)))
    airports = list(s.scalars(select(Airport).order_by(Airport.country, Airport.city)))
codes = [a.iata for a in airports]


def wd(text: str) -> list[str]:
    return [WEEKDAYS[i] for i in sorted(SearchConfiguration.parse_weekdays(text))]


df = pd.DataFrame([{
    "id": c.id, "enabled": c.enabled, "origin": c.origin, "destination": c.destination, "trip_type": c.trip_type,
    "date_from": c.date_from, "date_to": c.date_to, "days_ahead_min": c.days_ahead_min,
    "days_ahead_max": c.days_ahead_max, "return_date_from": c.return_date_from,
    "return_date_to": c.return_date_to, "min_stay_days": c.min_stay_days, "max_stay_days": c.max_stay_days,
    "departure_weekdays": ",".join(wd(c.departure_weekdays)), "return_weekdays": ",".join(wd(c.return_weekdays)),
    "passengers": c.passengers, "max_price": c.max_price, "frequency_hours": c.frequency_hours,
} for c in configs])
st.caption("Tip: the **Trip dates** page is the easy way to pick outbound and return dates. "
           "Edit cells and press Save. Absolute dates (date_from/date_to) override the rolling 'days ahead' window. "
           "Weekdays: comma separated, e.g. `Fri,Sat` (empty = any). max_price empty = global budget.")
edited = st.data_editor(
    df, hide_index=True, disabled=["id", "origin", "destination", "trip_type"], key="routes_editor",
    column_config={
        "date_from": st.column_config.DateColumn(), "date_to": st.column_config.DateColumn(),
        "return_date_from": st.column_config.DateColumn(), "return_date_to": st.column_config.DateColumn(),
        "max_price": st.column_config.NumberColumn(min_value=1),
        "passengers": st.column_config.NumberColumn(min_value=1, max_value=9),
        "frequency_hours": st.column_config.NumberColumn(min_value=1),
    },
)


def parse_wd(text) -> str:
    if not isinstance(text, str) or not text.strip():
        return ""
    names = [t.strip().title()[:3] for t in text.split(",") if t.strip()]
    bad = [n for n in names if n not in WEEKDAYS]
    if bad:
        raise ValueError(f"unknown weekday(s): {', '.join(bad)}")
    return ",".join(str(WEEKDAYS.index(n)) for n in names)


def none_if_nan(v):
    return None if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NaT else v


if st.button("Save routes"):
    try:
        with ctx.session_factory() as s:
            for row in edited.to_dict("records"):
                c = s.get(SearchConfiguration, int(row["id"]))
                c.enabled = bool(row["enabled"])
                c.date_from = none_if_nan(row["date_from"])
                c.date_to = none_if_nan(row["date_to"])
                c.days_ahead_min = int(row["days_ahead_min"])
                c.days_ahead_max = int(row["days_ahead_max"])
                if c.trip_type == "return":
                    c.return_date_from = none_if_nan(row["return_date_from"])
                    c.return_date_to = none_if_nan(row["return_date_to"])
                c.min_stay_days = none_if_nan(row["min_stay_days"]) and int(row["min_stay_days"])
                c.max_stay_days = none_if_nan(row["max_stay_days"]) and int(row["max_stay_days"])
                c.departure_weekdays = parse_wd(row["departure_weekdays"])
                c.return_weekdays = parse_wd(row["return_weekdays"])
                c.passengers = int(row["passengers"])
                c.max_price = none_if_nan(row["max_price"])
                c.frequency_hours = float(row["frequency_hours"])
            s.commit()
        st.success("Routes saved.")
    except (ValueError, TypeError) as exc:
        st.error(f"Not saved: {exc}")

with st.expander("➕ Add route"):
    with st.form("add_route"):
        a1, a2, a3 = st.columns(3)
        origin = a1.selectbox("From", codes)
        dest = a2.selectbox("To", codes, index=min(len(codes) - 1, 1))
        trip = a3.selectbox("Trip type", ["return", "one_way"])
        b1, b2, b3, b4 = st.columns(4)
        ahead_min = b1.number_input("Days ahead from", 0, 365, cfg.routes.days_ahead_min)
        ahead_max = b2.number_input("Days ahead to", 1, 365, cfg.routes.days_ahead_max)
        stay_min = b3.number_input("Min stay (return)", 0, 60, cfg.routes.min_stay_days or 3)
        stay_max = b4.number_input("Max stay (return)", 0, 90, cfg.routes.max_stay_days or 10)
        d1, d2 = st.columns(2)
        dep_days = d1.multiselect("Departure weekdays (empty = any)", WEEKDAYS)
        ret_days = d2.multiselect("Return weekdays (empty = any)", WEEKDAYS)
        if st.form_submit_button("Add route"):
            if origin == dest:
                st.error("Origin and destination must differ.")
            else:
                with ctx.session_factory() as s:
                    s.add(SearchConfiguration(
                        origin=origin, destination=dest, trip_type=trip,
                        days_ahead_min=ahead_min, days_ahead_max=ahead_max,
                        min_stay_days=stay_min if trip == "return" else None,
                        max_stay_days=stay_max if trip == "return" else None,
                        departure_weekdays=",".join(str(WEEKDAYS.index(d)) for d in dep_days),
                        return_weekdays=",".join(str(WEEKDAYS.index(d)) for d in ret_days) if trip == "return" else "",
                        passengers=cfg.routes.passengers, frequency_hours=cfg.routes.frequency_hours, enabled=True,
                    ))
                    s.commit()
                st.success("Route added.")
                st.rerun()

with st.expander("🗑️ Remove route"):
    if configs:
        rid = st.selectbox("Route", [c.id for c in configs],
                           format_func=lambda i: next(c.label for c in configs if c.id == i) + f" (#{i})")
        st.caption("Collected offers and price history are kept. Tip: disabling a route is usually enough.")
        if st.button("Delete route"):
            with ctx.session_factory() as s:
                s.delete(s.get(SearchConfiguration, rid))
                s.commit()
            st.rerun()

# ------------------------------------------------------------------ airports
st.subheader("Airports")
st.dataframe(pd.DataFrame([{"IATA": a.iata, "Name": a.name, "City": a.city, "Country": a.country,
                            "Timezone": a.timezone} for a in airports]), hide_index=True)
with st.expander("➕ Add airport"):
    with st.form("add_airport"):
        e1, e2, e3 = st.columns(3)
        iata = e1.text_input("IATA code", max_chars=3).strip().upper()
        name = e2.text_input("Airport name")
        city = e3.text_input("City")
        f1, f2 = st.columns(2)
        country = f1.text_input("Country")
        tz = f2.text_input("Timezone (IANA)", value="Europe/Warsaw")
        if st.form_submit_button("Add airport"):
            from zoneinfo import ZoneInfo

            try:
                ZoneInfo(tz)
                if len(iata) != 3 or not iata.isalpha() or not (name and city and country):
                    raise ValueError("fill all fields with a 3-letter IATA code")
                with ctx.session_factory() as s:
                    if s.get(Airport, iata):
                        raise ValueError(f"{iata} already exists")
                    s.add(Airport(iata=iata, name=name, city=city, country=country, timezone=tz))
                    s.commit()
                st.success(f"Added {iata}. Now add routes that use it.")
                st.rerun()
            except Exception as exc:
                st.error(f"Not added: {exc}")
with st.expander("🗑️ Remove airport"):
    with ctx.session_factory() as s:
        used = {c for (c,) in s.execute(select(SearchConfiguration.origin))} | \
               {c for (c,) in s.execute(select(SearchConfiguration.destination))}
        with_offers = {c for (c,) in s.execute(select(FlightOffer.origin).group_by(FlightOffer.origin)
                                                 .having(func.count() > 0))} | \
                      {c for (c,) in s.execute(select(FlightOffer.destination).distinct())}
    removable = [c for c in codes if c not in used and c not in with_offers]
    if removable:
        code = st.selectbox("Airport", removable)
        if st.button("Delete airport"):
            with ctx.session_factory() as s:
                s.delete(s.get(Airport, code))
                s.commit()
            st.rerun()
    else:
        st.caption("Airports used by routes or stored offers cannot be removed (disable/delete those routes first).")

st.caption(f"Ground transfer estimates (config.yaml → transfer_costs): " +
           (", ".join(f"{k}: {v.amount:g} {cfg.currency} per direction" for k, v in cfg.transfer_costs.items()) or "none")
           + f" · Today: {date.today():%d %b %Y}")
