"""Page 1 — Overview: cheapest flights, best deals, latest runs, provider health."""

import pandas as pd
import streamlit as st
from common import DISCLAIMER, LINK_COLUMNS, offers_frame, page_setup
from sqlalchemy import select

from app.core.timeutils import utcnow
from app.models.db_models import ProviderState, SearchConfiguration, SearchRun
from app.services import reports
from app.services.deal_engine import assess_offer

ctx = page_setup("Flight Radar — Overview")
st.caption(DISCLAIMER)

with ctx.session_factory() as s:
    offers = reports.current_offers(s, ctx.config)
    assessments = {o.id: assess_offer(s, o, ctx.config) for o in offers}
    last = reports.last_successful_run(s)
    runs = list(s.scalars(select(SearchRun).order_by(SearchRun.started_at.desc()).limit(25)))
    labels = {c.id: c.label for c in s.scalars(select(SearchConfiguration))}
    states = list(s.scalars(select(ProviderState)))

c1, c2, c3, c4 = st.columns(4)
c1.metric("Current offers", len(offers), help=f"Seen in the last {ctx.config.dashboard.current_offer_hours:.0f}h, future departures")
c2.metric("Cheapest", f"{min(o.price for o in offers):.0f} {ctx.config.currency}" if offers else "—")
c3.metric("Good+ deals", sum(a.category != "normal" for a in assessments.values()))
c4.metric("Last successful search", last.completed_at.strftime("%d %b %H:%M UTC") if last else "never")

if st.button("🔎 Run search now", help="Searches all enabled routes immediately"):
    with st.spinner("Searching… (polite rate limits apply, this can take a minute)"):
        summary = ctx.search_service.run(trigger="manual", force=True)
    (st.warning if summary.errors else st.success)(summary.text())
    for e in summary.errors:
        st.error(e)

if not offers:
    st.info("No current offers yet. Add your Travelpayouts token to `.env` and run a search "
            "(button above or `python main.py search`). Nothing is shown until real data is collected.")
else:
    st.subheader("Best current deals")
    best = sorted(offers, key=lambda o: (-assessments[o.id].score, assessments[o.id].effective_price_pp))[:10]
    st.dataframe(offers_frame(best, assessments), column_config=LINK_COLUMNS, hide_index=True)

    st.subheader("Cheapest per route")
    cheapest = {}
    for o in offers:  # already sorted by price
        cheapest.setdefault((o.origin, o.destination, o.trip_type), o)
    st.dataframe(offers_frame(list(cheapest.values()), assessments), column_config=LINK_COLUMNS, hide_index=True)

st.subheader("Provider health")
if states:
    now = utcnow()
    st.dataframe(pd.DataFrame([{
        "Provider": p.provider, "Status": p.status,
        "Paused until (UTC)": p.paused_until if p.paused_until and p.paused_until > now else None,
        "Last success (UTC)": p.last_success_at, "Last failure (UTC)": p.last_failure_at,
        "Consecutive failures": p.consecutive_failures, "Last error": p.last_error,
    } for p in states]), hide_index=True)
else:
    st.write("No searches have run yet.")

st.subheader("Latest search runs")
if runs:
    st.dataframe(pd.DataFrame([{
        "Started (UTC)": r.started_at, "Route": labels.get(r.search_configuration_id, "—"), "Provider": r.provider,
        "Trigger": r.trigger, "Status": r.status, "Results": r.results_count, "New": r.new_offers,
        "Requests": r.requests_made, "Cache hits": r.cache_hits, "Errors": r.errors,
    } for r in runs]), hide_index=True)
else:
    st.write("No search runs recorded.")
