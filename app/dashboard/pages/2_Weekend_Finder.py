"""Page — Weekend finder: where can you both fly for the lowest combined price?"""

import pandas as pd
import streamlit as st
from common import page_setup

from app.services import weekend_finder as wf

ctx = page_setup("Weekend finder")
cfg = ctx.config.weekend_finder
names = " & ".join(wf._who(t.name) for t in cfg.travelers) or "nobody"
st.caption(f"Every {wf.WEEKDAY[cfg.outbound_weekday]} → +{cfg.nights} nights weekend, all Ryanair destinations "
           f"shared by {names}. Prices per person, return, basic fare; confirm on ryanair.com.")

if not cfg.enabled:
    st.info("The weekend finder is off. Turn it on in config.yaml → weekend_finder.")
    st.stop()

with ctx.session_factory() as s:
    meetups, visits = wf.combine(s, ctx.config)
    rows = wf.latest_snapshot(s)

if st.button("🔎 Search all weekends now", help=f"About {len(cfg.travelers) and sum(len(t.origins) for t in cfg.travelers) * cfg.weeks} "
                                                "Ryanair requests, one every 3 s"):
    with st.spinner("Searching every weekend… this takes a couple of minutes"):
        with ctx.session_factory() as s:
            r = wf.run_finder(s, ctx.config, ctx.providers.get("ryanair"))
    (st.warning if r.errors or r.skipped_reason else st.success)(
        r.skipped_reason or f"{r.weekends} weekends, {r.requests} requests, {r.fares} fares")
    st.rerun()

if not rows:
    st.info("No results yet. Press the button above, or wait for the next scheduled run.")
    st.stop()

st.caption(f"Last searched {rows[0].observed_at:%d %b %H:%M} UTC")
weekend_labels = sorted({(m.out_day, m.back_day) for m in meetups})
c1, c2, c3 = st.columns(3)
pick = c1.multiselect("Weekends", weekend_labels, format_func=lambda w: wf._weekend(*w), placeholder="All")
max_total = c2.number_input(f"Max together ({ctx.config.currency})", min_value=0.0, value=0.0, step=10.0,
                            help="0 = no limit")
query = c3.text_input("Destination contains", placeholder="e.g. Milan, Spain")

data = []
for m in meetups:
    if pick and (m.out_day, m.back_day) not in pick:
        continue
    if max_total and m.total > max_total:
        continue
    if query and query.lower() not in f"{m.city} {m.country} {m.destination}".lower():
        continue
    row = {"_day": m.out_day, "Weekend": wf._weekend(m.out_day, m.back_day), "Destination": f"{m.city} ({m.destination})",
           "Country": m.country, "Together": m.total}
    for leg in m.legs:
        who = wf._who(leg.traveler)
        row[who] = leg.fare.price
        row[f"{who} from"] = leg.fare.origin
        row[f"{who} times"] = f"{leg.fare.outbound_departure:%a %H:%M} → {leg.fare.inbound_departure:%a %H:%M}"
    data.append(row)

st.subheader(f"🌍 Meeting somewhere · {len(data)} options")
if data:
    st.dataframe(pd.DataFrame(data).drop(columns="_day"), hide_index=True, column_config={
        "Together": st.column_config.NumberColumn(format="%.2f"),
        **{wf._who(t.name): st.column_config.NumberColumn(format="%.2f") for t in cfg.travelers}})
    best = (pd.DataFrame(data).groupby(["_day", "Weekend"])["Together"].min().reset_index()
            .sort_values("_day").drop(columns="_day"))  # in date order
    st.caption("Cheapest shared destination per weekend")
    st.bar_chart(best, x="Weekend", y="Together", sort=False, height=220)
else:
    st.info("No shared destination matches these filters.")

if visits:
    st.subheader("🏠 Visiting each other")
    st.dataframe(pd.DataFrame([{
        "Weekend": wf._weekend(v.out_day, v.back_day), "Who": wf._who(v.visitor),
        "To": f"{v.fare.city} ({v.fare.destination})", "Price": v.fare.price,
        "Times": f"{v.fare.outbound_departure:%a %H:%M} → {v.fare.inbound_departure:%a %H:%M}"} for v in visits]),
        hide_index=True, column_config={"Price": st.column_config.NumberColumn(format="%.2f")})
