"""Page 2 — Flight search over stored offers (no live API call)."""

from datetime import date, timedelta

import streamlit as st
from common import DISCLAIMER, LINK_COLUMNS, offers_frame, page_setup
from sqlalchemy import select

from app.core.airlines import airline_name
from app.core.timeutils import utcnow
from app.models.db_models import Airport, FlightOffer
from app.services.deal_engine import assess_offer

ctx = page_setup("Flight Search")
st.caption("Return trips only: each result is a going flight and a coming-back flight priced together. "
           "Filters the offers already collected. " + DISCLAIMER)

with ctx.session_factory() as s:
    airports = {a.iata: a for a in s.scalars(select(Airport))}
    origins = sorted({o for (o,) in s.execute(select(FlightOffer.origin).distinct())}) or sorted(airports)
    dests = sorted({d for (d,) in s.execute(select(FlightOffer.destination).distinct())}) or sorted(airports)
    airlines = sorted({a for (a,) in s.execute(select(FlightOffer.airline).distinct()) if a})

fmt = lambda c: f"{airports[c].city} ({c})" if c in airports else c  # noqa: E731
col1, col2 = st.columns(2)
sel_origins = col1.multiselect("Departure airport", origins, default=origins, format_func=fmt)
sel_dests = col2.multiselect("Destination airport", dests, default=dests, format_func=fmt)
trip = "return"  # always going + coming back, priced together
d1, d2, d3 = st.columns(3)
dates = d1.date_input("Going between", (date.today(), date.today() + timedelta(days=90)), format="DD/MM/YYYY")
ret_dates = d2.date_input("Coming back between", (date.today(), date.today() + timedelta(days=120)),
                          format="DD/MM/YYYY")
nights = d3.slider("Nights away", 0, 30, (0, 30))
col4, col5 = st.columns(2)
max_price = col4.number_input(f"Max price ({ctx.config.currency}, total)", min_value=0.0, value=0.0, step=10.0,
                              help="0 = no limit")
stops = col5.radio("Stops", ["any", "direct only", "connecting only"], horizontal=True)
col7, col8 = st.columns(2)
sel_airlines = col7.multiselect("Airline", airlines, format_func=airline_name, help="Empty = all")
recent_only = col8.checkbox(f"Only offers seen in the last {ctx.config.dashboard.current_offer_hours:.0f}h", value=True)

with ctx.session_factory() as s:
    q = select(FlightOffer).where(FlightOffer.departure_at > utcnow())
    if sel_origins:
        q = q.where(FlightOffer.origin.in_(sel_origins))
    if sel_dests:
        q = q.where(FlightOffer.destination.in_(sel_dests))
    if trip != "both":
        q = q.where(FlightOffer.trip_type == trip)
    if isinstance(dates, (tuple, list)) and len(dates) == 2:
        q = q.where(FlightOffer.departure_at >= dates[0], FlightOffer.departure_at < dates[1] + timedelta(days=1))
    if max_price:
        q = q.where(FlightOffer.price <= max_price)
    if sel_airlines:
        q = q.where(FlightOffer.airline.in_(sel_airlines))
    if recent_only:
        q = q.where(FlightOffer.collected_at >= utcnow() - timedelta(hours=ctx.config.dashboard.current_offer_hours))
    offers = list(s.scalars(q.order_by(FlightOffer.price).limit(1000)))
    if trip != "one_way":
        def _return_ok(o) -> bool:
            if o.trip_type != "return":
                return True
            if o.return_departure_at is None:
                return False
            r = o.return_departure_at.date()
            if isinstance(ret_dates, (tuple, list)) and len(ret_dates) == 2 and not (ret_dates[0] <= r <= ret_dates[1]):
                return False
            return nights[0] <= (r - o.departure_at.date()).days <= nights[1]
        offers = [o for o in offers if _return_ok(o)]
    if stops == "direct only":
        offers = [o for o in offers if o.is_direct]
    elif stops == "connecting only":
        offers = [o for o in offers if o.is_direct is False]
    assessments = {o.id: assess_offer(s, o, ctx.config) for o in offers[:300]}

st.write(f"**{len(offers)}** offers")
if offers:
    st.dataframe(offers_frame(offers[:300], assessments), column_config=LINK_COLUMNS, hide_index=True)
else:
    st.info("No stored offers match these filters.")
