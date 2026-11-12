"""Page 3 — Price history per route (all real observations, never interpolated)."""

import pandas as pd
import streamlit as st
from common import page_setup
from sqlalchemy import select

from app.models.db_models import FlightOffer, PriceHistory

ctx = page_setup("Price History")

with ctx.session_factory() as s:
    routes = sorted({(o, d, t) for o, d, t in s.execute(
        select(FlightOffer.origin, FlightOffer.destination, FlightOffer.trip_type).distinct())})

if not routes:
    st.info("No price observations yet. History builds up automatically with every search.")
    st.stop()

label = lambda r: f"{r[0]}→{r[1]} ({'return' if r[2] == 'return' else 'one-way'})"  # noqa: E731
route = st.selectbox("Route", routes, format_func=label)

with ctx.session_factory() as s:
    rows = s.execute(
        select(PriceHistory.observed_at, PriceHistory.observed_price, PriceHistory.currency, PriceHistory.provider,
               FlightOffer.id, FlightOffer.departure_at, FlightOffer.return_departure_at, FlightOffer.airline,
               FlightOffer.flight_number)
        .join(FlightOffer, FlightOffer.id == PriceHistory.offer_id)
        .where(FlightOffer.origin == route[0], FlightOffer.destination == route[1], FlightOffer.trip_type == route[2])
        .order_by(PriceHistory.observed_at)
    ).all()

df = pd.DataFrame(rows, columns=["observed_at", "price", "currency", "provider", "offer_id", "departure",
                                 "return", "airline", "flight"])
currencies = df["currency"].unique().tolist()
if len(currencies) > 1:
    cur = st.radio("Currency", currencies, horizontal=True)
    df = df[df["currency"] == cur]
cur = df["currency"].iloc[0]

c1, c2, c3, c4 = st.columns(4)
c1.metric("Observations", len(df))
c2.metric("Lowest observed", f"{df['price'].min():.2f} {cur}")
c3.metric("Average observed", f"{df['price'].mean():.2f} {cur}")
c4.metric("Itineraries", df["offer_id"].nunique())

st.subheader("Lowest and average observed price per day")
st.caption("Each point summarises all itineraries on this route observed that day.")
daily = df.assign(day=df["observed_at"].dt.floor("D")).groupby("day")["price"].agg(lowest="min", average="mean",
                                                                                     median="median")
if len(daily) < 2:
    st.info("Only one day of observations so far — the chart becomes useful after a few days of searches.")
st.line_chart(daily, y=["lowest", "average", "median"])

st.subheader("Cheapest price by departure date (latest observations)")
latest = df.sort_values("observed_at").groupby("offer_id").tail(1)
by_dep = latest.assign(departure_day=latest["departure"].dt.floor("D")).groupby("departure_day")["price"].min()
st.bar_chart(by_dep)

st.subheader("Price changes of individual itineraries")
changes = (df.groupby("offer_id")
           .agg(departure=("departure", "first"), ret=("return", "first"), airline=("airline", "first"),
                first_price=("price", "first"), last_price=("price", "last"), lowest=("price", "min"),
                observations=("price", "size"))
           .query("observations > 1"))
if changes.empty:
    st.write("No itinerary has been observed more than once yet.")
else:
    changes["change"] = changes["last_price"] - changes["first_price"]
    changes["change %"] = (changes["change"] / changes["first_price"] * 100).round(1)
    st.dataframe(changes.sort_values("change"), hide_index=True)
    pick = st.selectbox("Show history of itinerary", changes.index.tolist(),
                        format_func=lambda i: f"#{i} dep {changes.loc[i, 'departure']:%d %b %H:%M} "
                                              f"({changes.loc[i, 'airline'] or '?'})")
    st.line_chart(df[df["offer_id"] == pick].set_index("observed_at")["price"])
