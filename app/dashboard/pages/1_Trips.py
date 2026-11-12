"""Page — Trips: pick exact outbound/return dates to watch, see the best price per trip
and how many provider requests the watch list costs."""

from datetime import date, timedelta

import streamlit as st
from common import page_setup
from sqlalchemy import select

from app.core.airlines import airline_name
from app.models.db_models import Airport, SearchConfiguration
from app.services import trips as trip_service

ctx = page_setup("Trips")
cfg = ctx.config
today = date.today()

st.caption("Each trip is an exact outbound and return date, searched from every departure airport to every "
           "destination you pick. Prices come from the enabled providers only and are indicative: "
           "always confirm on the airline's site.")

with ctx.session_factory() as s:
    airports = {a.iata: a for a in s.scalars(select(Airport).order_by(Airport.country, Airport.city))}
    all_configs = list(s.scalars(select(SearchConfiguration)))
    trips = trip_service.list_trips(s)


def fmt_airport(code: str) -> str:
    a = airports.get(code)
    return f"{a.city} ({code})" if a else code


def fmt_day(d: date) -> str:
    return f"{trip_service.WEEKDAY_NAMES[d.weekday()]} {d:%d %b %Y}"


# ---------------------------------------------------------------- date picking state
# Presets: (outbound weekday, nights). Picking one snaps the outbound date to the next
# matching weekday and sets the return date; editing a date keeps the trip length.
PRESETS = {
    "Fri → Sun": (4, 2),
    "Fri → Mon": (4, 3),
    "Thu → Sun": (3, 3),
    "Thu → Mon": (3, 4),
    "Sat → Mon": (5, 2),
    "Custom": None,
}

if "trip_out" not in st.session_state:
    first = date(2027, 1, 29) if date(2027, 1, 29) >= today else today + timedelta(days=(4 - today.weekday()) % 7)
    st.session_state.trip_out = first
    st.session_state.trip_back = first + timedelta(days=3)
    st.session_state.trip_nights = 3
    st.session_state.trip_preset = "Fri → Mon"


def _on_preset() -> None:
    preset = PRESETS.get(st.session_state.trip_preset)
    if preset is None:
        return
    weekday, nights = preset
    out = max(st.session_state.trip_out, today)
    out += timedelta(days=(weekday - out.weekday()) % 7)
    st.session_state.trip_out = out
    st.session_state.trip_back = out + timedelta(days=nights)
    st.session_state.trip_nights = nights


def _on_out() -> None:
    # Moving the outbound date keeps the trip length.
    st.session_state.trip_back = st.session_state.trip_out + timedelta(days=st.session_state.trip_nights)
    preset = PRESETS.get(st.session_state.trip_preset)
    if preset and preset[0] != st.session_state.trip_out.weekday():
        st.session_state.trip_preset = "Custom"


def _on_back() -> None:
    nights = (st.session_state.trip_back - st.session_state.trip_out).days
    if nights < 0:  # return picked before outbound: move the outbound instead
        st.session_state.trip_out = st.session_state.trip_back
        nights = 0
    st.session_state.trip_nights = nights
    preset = PRESETS.get(st.session_state.trip_preset)
    if preset and preset[1] != nights:
        st.session_state.trip_preset = "Custom"


# ---------------------------------------------------------------- add dates
with st.container(border=True):
    st.subheader("➕ Add dates")
    st.segmented_control("Quick pick", list(PRESETS), key="trip_preset", on_change=_on_preset,
                         help="Snaps to the next matching weekday. Edit the dates freely afterwards.")
    c1, c2, c3 = st.columns([2, 2, 1])
    c1.date_input("Going (outbound)", key="trip_out", min_value=today, format="DD/MM/YYYY", on_change=_on_out)
    c2.date_input("Coming back (return)", key="trip_back", min_value=today, format="DD/MM/YYYY",
                  on_change=_on_back)
    weeks = c3.number_input("Repeat weekly", min_value=1, max_value=12, value=1,
                            help="Also add the same weekdays for the following weeks (e.g. 4 = this and the "
                                 "next 3 weekends).")
    a1, a2, a3 = st.columns([2, 2, 1])
    codes = list(airports)
    origins = a1.multiselect("From", codes, default=[c for c in cfg.routes.origins if c in airports],
                             format_func=fmt_airport)
    dests = a2.multiselect("To", codes, default=[c for c in cfg.routes.destinations if c in airports],
                           format_func=fmt_airport, help="Katowice adds an estimated bus cost to Kraków when "
                                                         "scoring deals.")
    max_price = a3.number_input(f"Max price ({cfg.currency})", min_value=0.0, value=0.0, step=10.0,
                                help="Per person, whole return trip. 0 = global budget (Settings).")

    out, back = st.session_state.trip_out, st.session_state.trip_back
    pairs = trip_service.repeat_weekly(out, back, int(weeks))
    existing = {(t.outbound, t.back) for t in trips}
    problems = []
    try:
        trip_service.validate_dates(out, back, today)
    except ValueError as exc:
        problems.append(str(exc))
    if not origins or not dests:
        problems.append("pick at least one airport on each side")

    if problems:
        st.warning("Can't add yet: " + "; ".join(problems))
    else:
        nights = (back - out).days
        lines = [f"- {fmt_day(o)} → {fmt_day(b)} · {nights} night{'s' if nights != 1 else ''}"
                 + (" *(already watched)*" if (o, b) in existing else "") for o, b in pairs]
        st.markdown("**Will add:**\n" + "\n".join(lines)
                    + f"\n\n{', '.join(map(fmt_airport, origins))} → {', '.join(map(fmt_airport, dests))} "
                      f"= {len(origins) * len(dests)} route(s) per trip")

    # What would the watch list cost after adding these dates?
    pending = [trip_service.new_trip_config(cfg, o, d, po, pb)
               for po, pb in pairs for o in origins for d in dests
               if o != d and (po, pb) not in existing] if not problems else []
    after = trip_service.estimate_usage(all_configs + pending, cfg, ctx.providers, today=today)
    over = [u for u in after if u.over_cap]
    if over:
        st.error("Too many dates for one run: " + "; ".join(
            f"{u.provider} would need ~{u.requests_per_run} requests (cap {u.cap_per_run})" for u in over)
            + ". The extra searches would be skipped each run. Remove some dates or airports.")

    all_known = all(pair in existing for pair in pairs)
    if st.button("Add to watch list", type="primary", disabled=bool(problems) or all_known,
                 help="These dates are already watched" if all_known else None):
        added = 0
        with ctx.session_factory() as s:
            for po, pb in pairs:
                added += trip_service.add_trip(s, cfg, po, pb, origins, dests,
                                               max_price=max_price or None, today=today)
        st.session_state.trip_flash = (f"Added {len(pairs)} trip(s) ({added} new route searches). They run at the "
                                       "next scheduler tick, or press 'Search now'.")
        st.rerun()
    if flash := st.session_state.pop("trip_flash", None):
        st.success(flash)

# ---------------------------------------------------------------- watch list
st.subheader("👀 Watched trips")
upcoming = [t for t in trips if not t.is_past(today)]
past = [t for t in trips if t.is_past(today)]

if not trips:
    st.info("No trips yet. Add dates above.")

b1, b2 = st.columns([1, 3])
enabled_ids = [i for t in upcoming if t.enabled for i in t.config_ids]
if b1.button("🔎 Search now", disabled=not enabled_ids, help="Searches all enabled upcoming trips immediately"):
    with st.spinner("Searching… (Ryanair is queried politely, one request every few seconds)"):
        summary = ctx.search_service.run(trigger="manual", config_ids=enabled_ids, force=True)
    (st.warning if summary.errors else st.success)(summary.text())
    for e in summary.errors:
        st.caption(f"⚠️ {e}")

for t in upcoming:
    with ctx.session_factory() as s:
        offers = trip_service.cheapest_offers(s, t)
    best = offers[0] if offers else None
    with st.container(border=True):
        left, mid, right = st.columns([3, 3, 1])
        left.markdown(f"**{t.label}**  \n"
                      f"{', '.join(map(fmt_airport, t.origins))} → {', '.join(map(fmt_airport, t.destinations))}"
                      + (f"  \nMax {t.max_price:g} {cfg.currency} pp" if t.max_price else ""))
        if best:
            mid.markdown(
                f"Best seen: **{best.price:.2f} {best.currency}** {best.origin}→{best.destination} "
                f"({airline_name(best.airline)})  \n"
                f"Out {best.departure_at:%H:%M} · back {best.return_departure_at:%H:%M} · "
                f"seen {best.collected_at:%d %b %H:%M} UTC  \n"
                + (f"[Open booking page]({best.booking_url})" if best.booking_url else ""))
            if len(offers) > 1:
                with mid.expander(f"All {len(offers)} options"):
                    for o in offers:
                        st.markdown(f"- {o.origin}→{o.destination}: **{o.price:.2f} {o.currency}** "
                                    f"({o.provider}, seen {o.collected_at:%d %b %H:%M} UTC)")
        else:
            mid.markdown("No price yet" + (f" · last searched {t.last_run_at:%d %b %H:%M} UTC"
                                           if t.last_run_at else " · not searched yet"))
            if t.last_run_at:
                mid.caption("No enabled provider has a fare for these exact dates and airports right now.")
        on = right.toggle("Watch", value=t.enabled, key=f"on_{t.key}")
        if on != t.enabled:
            with ctx.session_factory() as s:
                trip_service.set_trip_enabled(s, t, on)
            st.rerun()
        if right.button("Remove", key=f"del_{t.key}", help="Stops watching. Collected prices are kept."):
            with ctx.session_factory() as s:
                trip_service.delete_trip(s, t)
            st.rerun()

if past:
    with st.expander(f"Past trips ({len(past)})"):
        for t in past:
            st.markdown(f"- {t.label}")
        if st.button("Remove past trips"):
            with ctx.session_factory() as s:
                for t in past:
                    trip_service.delete_trip(s, t)
            st.rerun()

# ---------------------------------------------------------------- schedule & limits
st.subheader("⏱️ Schedule and request limits")
trip_configs = [c for c in all_configs if trip_service.is_trip_config(c)]
freq_now = min((c.frequency_hours for c in trip_configs), default=cfg.routes.frequency_hours)
f1, f2 = st.columns([1, 3])
with f1.form("trip_freq"):
    freq = st.number_input("Search trips every N hours", min_value=1.0, max_value=168.0, value=float(freq_now),
                           step=1.0)
    if st.form_submit_button("Save"):
        with ctx.session_factory() as s:
            for c in s.scalars(select(SearchConfiguration)):
                if trip_service.is_trip_config(c):
                    c.frequency_hours = freq
            s.commit()
        st.rerun()

usage = trip_service.estimate_usage(all_configs, cfg, ctx.providers, today=today)
with f2:
    for u in usage:
        configured = ctx.providers[u.provider].is_configured()
        st.markdown(f"**{u.provider}**: ~{u.requests_per_run} requests per run (cap {u.cap_per_run}) · "
                    f"~{u.requests_per_day:.0f} per day" + ("" if configured else " · *not configured, skipped*"))
        st.progress(min(1.0, u.cap_share))
        st.caption(u.note)
    st.caption("Automatic searches only happen while `python main.py run` is running on your computer. "
               "Ryanair publishes no official quota: the app keeps itself to one request every 3 s and stops "
               "for 24 h at the first block or CAPTCHA.")

rolling = [c for c in all_configs if c.enabled and not trip_service.is_trip_config(c)]
if rolling:
    st.warning(f"{len(rolling)} rolling route(s) (\"next N days\") are also enabled and add requests to every "
               "run. Turn them off if you only want the trips above.")
    if st.button("Turn off rolling routes"):
        with ctx.session_factory() as s:
            trip_service.disable_rolling_routes(s)
        st.rerun()
