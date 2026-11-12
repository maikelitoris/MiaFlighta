"""Page — Trip dates: pick outbound and return dates on a calendar, apply them to
your routes, and see the cheapest stored price for every outbound day."""

from __future__ import annotations

import calendar
import html
from datetime import date, timedelta

import pandas as pd
import streamlit as st
from common import page_setup
from sqlalchemy import select

from app.models.db_models import Airport, SearchConfiguration
from app.services import reports, settings_service
from app.services.deal_engine import assess_offer

ctx = page_setup("Trip dates")
cfg = ctx.config
today = date.today()
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MAX_AHEAD = 365

st.markdown(
    """
<style>
.cal-wrap {display:flex; flex-wrap:wrap; align-items:flex-start; gap:18px; margin:4px 0 8px}
.cal {border-collapse:separate; border-spacing:3px; font-size:13px}
.cal caption {font-weight:600; text-align:left; padding:0 0 4px 2px}
.cal th {font-weight:500; opacity:.6; padding:2px 0; width:44px; text-align:center}
.cal td {width:44px; height:40px; text-align:center; vertical-align:middle; border-radius:8px;
         line-height:1.15; border:1px solid rgba(128,128,128,.18)}
.cal td.out, .cal td.ret, .cal td.both {border-radius:6px}
.cal td.empty {border:none}
.cal td.past {opacity:.3}
.cal td.out {background:rgba(37,99,235,.80); color:#fff; border-color:transparent}
.cal td.ret {background:rgba(234,88,12,.80); color:#fff; border-color:transparent}
.cal td.both {background:linear-gradient(135deg, rgba(37,99,235,.85) 50%, rgba(234,88,12,.85) 50%);
              color:#fff; border-color:transparent}
.cal td small {display:block; font-size:10px; opacity:.95}
.cal td.p1 {background:rgba(22,163,74,.85); color:#fff}
.cal td.p2 {background:rgba(132,204,22,.70)}
.cal td.p3 {background:rgba(234,179,8,.55)}
.cal td.p4 {background:rgba(249,115,22,.55)}
.cal td.p5 {background:rgba(239,68,68,.55)}
.legend span {display:inline-block; padding:2px 10px; border-radius:6px; margin-right:6px; font-size:13px; color:#fff}
</style>
""",
    unsafe_allow_html=True,
)


def month_iter(start: date, end: date):
    m = start.replace(day=1)
    while m <= end:
        yield m
        m = (m.replace(day=28) + timedelta(days=4)).replace(day=1)


def calendar_html(start: date, end: date, cell) -> str:
    """cell(day) -> (css_class, inner_html)."""
    parts = ['<div class="cal-wrap">']
    for m in month_iter(start, end):
        rows = [f'<table class="cal"><caption>{m:%B %Y}</caption><tr>'
                + "".join(f"<th>{d[:2]}</th>" for d in WEEKDAYS) + "</tr>"]
        for week in calendar.Calendar(firstweekday=0).monthdatescalendar(m.year, m.month):
            tds = []
            for d in week:
                if d.month != m.month:
                    tds.append('<td class="empty"></td>')
                    continue
                cls, inner = cell(d)
                tds.append(f'<td class="{cls}">{inner}</td>')
            rows.append("<tr>" + "".join(tds) + "</tr>")
        rows.append("</table>")
        parts.append("".join(rows))
    parts.append("</div>")
    return "".join(parts)


def wd_codes(names: list[str]) -> str:
    return ",".join(str(WEEKDAYS.index(n)) for n in names)


def wd_names(text: str | None) -> list[str]:
    return [WEEKDAYS[i] for i in sorted(SearchConfiguration.parse_weekdays(text))]


# ------------------------------------------------------------------ current values
with ctx.session_factory() as s:
    configs = list(s.scalars(select(SearchConfiguration).order_by(SearchConfiguration.id)))
    airports = list(s.scalars(select(Airport).order_by(Airport.country, Airport.city)))
names = {a.iata: f"{a.city} ({a.iata})" for a in airports}
pt = [a.iata for a in airports if a.country.lower() == "portugal"] or [a.iata for a in airports]
others = [a.iata for a in airports if a.iata not in pt]
ref = next((c for c in configs if c.enabled and c.trip_type == "return"), None) or \
      next((c for c in configs if c.trip_type == "return"), None)

TAB_PICK, TAB_PRICES = st.tabs(["📅 Choose dates", "💶 Price calendar"])

# ================================================================== choose dates
with TAB_PICK:
    st.caption("Pick when you can fly out and when you can come back. Press **Apply** to make the "
               "automatic searches use these dates.")

    st.markdown("#### Where")
    w1, w2, w3 = st.columns([2, 2, 1.3])
    enabled_pairs = {(c.origin, c.destination) for c in configs if c.enabled}
    origins = w1.multiselect("From", pt + others, default=sorted({o for o, _ in enabled_pairs if o in pt}) or pt,
                             format_func=lambda c: names.get(c, c))
    dests = w2.multiselect("To", others + pt, default=sorted({d for _, d in enabled_pairs if d in others}) or others,
                           format_func=lambda c: names.get(c, c))
    with w3:
        want_return = st.checkbox("Return trips", value=True)
        want_oneway = st.checkbox("One-way trips", value=any(c.trip_type == "one_way" and c.enabled for c in configs))

    # Widget defaults come from the first return route; presets overwrite them.
    st.session_state.setdefault("dep_wd", wd_names(ref.departure_weekdays) if ref else [])
    st.session_state.setdefault("ret_wd", wd_names(ref.return_weekdays) if ref else [])
    st.session_state.setdefault("stay", (ref.min_stay_days or 3, ref.max_stay_days or 10) if ref else (3, 10))

    # ---------------------------------------------------- presets
    st.markdown("#### When")
    presets = {
        "Any day": ([], [], (3, 10)),
        "Weekend (Fri/Sat → Sun/Mon)": (["Fri", "Sat"], ["Sun", "Mon"], (1, 3)),
        "Long weekend (Thu/Fri → Sun/Mon)": (["Thu", "Fri"], ["Sun", "Mon"], (2, 4)),
        "About a week": ([], [], (5, 9)),
    }
    preset = st.segmented_control("Quick start", list(presets), default=None, key="preset")
    if preset and st.session_state.get("_applied_preset") != preset:
        dep_p, ret_p, stay_p = presets[preset]
        st.session_state.update(dep_wd=dep_p, ret_wd=ret_p, stay=stay_p, _applied_preset=preset)
        st.rerun()

    # ---------------------------------------------------- outbound
    o1, o2 = st.columns(2)
    with o1:
        st.markdown("**🛫 Going**")
        out_mode = st.radio("Outbound dates", ["Exact dates", "Rolling window"], horizontal=True,
                            index=0 if (ref and ref.date_from) else 1, label_visibility="collapsed",
                            help="Exact dates stay fixed. A rolling window moves forward every day.")
        if out_mode == "Exact dates":
            default_out = (ref.date_from, ref.date_to) if ref and ref.date_from and ref.date_to else \
                          (today + timedelta(days=7), today + timedelta(days=37))
            out_range = st.date_input("Leave between", value=default_out, min_value=today,
                                      max_value=today + timedelta(days=MAX_AHEAD), format="DD/MM/YYYY")
            if isinstance(out_range, (tuple, list)) and len(out_range) == 2:
                out_start, out_end = out_range
            else:
                st.info("Click a second day to finish the range.")
                out_start = out_end = out_range[0] if isinstance(out_range, (tuple, list)) else out_range
            ahead = None
        else:
            ahead = st.slider("Leave within the next … days", 0, 120,
                              value=(ref.days_ahead_min, ref.days_ahead_max) if ref else (1, 60))
            out_start, out_end = today + timedelta(days=ahead[0]), today + timedelta(days=ahead[1])
        dep_wd = st.pills("Only on these days (none = any day)", WEEKDAYS, selection_mode="multi", key="dep_wd")

    # ---------------------------------------------------- return
    with o2:
        st.markdown("**🛬 Coming back**")
        if not want_return:
            st.caption("Return trips are off.")
            ret_mode, stay, ret_start, ret_end, ret_wd = "Stay length", None, None, None, []
        else:
            ret_mode = st.radio("Return dates", ["Stay length", "Exact dates", "Both"], horizontal=True,
                                index=(2 if ref.min_stay_days else 1) if (ref and ref.return_date_from) else 0,
                                label_visibility="collapsed",
                                help="Stay length = nights away. Exact dates = a fixed return window. "
                                     "Both = must match both rules.")
            stay = None
            ret_start = ret_end = None
            if ret_mode in ("Stay length", "Both"):
                stay = st.slider("Stay between … days", 1, 30, key="stay")
            if ret_mode in ("Exact dates", "Both"):
                default_ret = (ref.return_date_from, ref.return_date_to) \
                    if ref and ref.return_date_from and ref.return_date_to else \
                    (out_start + timedelta(days=3), out_end + timedelta(days=10))
                default_ret = (max(default_ret[0], out_start), max(default_ret[1], out_start))
                ret_range = st.date_input("Come back between", value=default_ret, min_value=out_start,
                                          max_value=today + timedelta(days=MAX_AHEAD + 30), format="DD/MM/YYYY")
                if isinstance(ret_range, (tuple, list)) and len(ret_range) == 2:
                    ret_start, ret_end = ret_range
                else:
                    st.info("Click a second day to finish the range.")
                    ret_start = ret_end = ret_range[0] if isinstance(ret_range, (tuple, list)) else ret_range
            ret_wd = st.pills("Only on these days (none = any day)", WEEKDAYS, selection_mode="multi",
                              key="ret_wd")

    dep_wd = dep_wd or []
    ret_wd = ret_wd or []

    # ---------------------------------------------------- preview
    out_days = [out_start + timedelta(days=i) for i in range((out_end - out_start).days + 1)
                if not dep_wd or WEEKDAYS[(out_start + timedelta(days=i)).weekday()] in dep_wd]
    out_set = set(out_days)
    pairs: list[tuple[date, date]] = []
    if want_return and out_days:
        lo = stay[0] if stay else 1
        hi = stay[1] if stay else ((ret_end - out_start).days if ret_end else 30)
        for d in out_days:
            for n in range(lo, hi + 1):
                r = d + timedelta(days=n)
                if ret_start and r < ret_start or ret_end and r > ret_end:
                    continue
                if ret_wd and WEEKDAYS[r.weekday()] not in ret_wd:
                    continue
                pairs.append((d, r))
    ret_set = {r for _, r in pairs}
    usable_out = {d for d, _ in pairs} if want_return and not want_oneway else out_set

    st.markdown("#### Your selection")
    m1, m2, m3 = st.columns(3)
    m1.metric("Outbound days", len(usable_out))
    m2.metric("Return days", len(ret_set) if want_return else "—")
    m3.metric("Date combinations", len(pairs) if want_return else len(out_set))
    st.markdown('<div class="legend"><span style="background:rgba(37,99,235,.85)">going</span>'
                '<span style="background:rgba(234,88,12,.85)">coming back</span></div>', unsafe_allow_html=True)

    def sel_cell(d: date):
        cls = "past" if d < today else ""
        o, r = d in usable_out, d in ret_set
        if o and r:
            cls = "both"
        elif o:
            cls = "out"
        elif r:
            cls = "ret"
        return cls, str(d.day)

    last_day = max([out_end] + list(ret_set))
    st.markdown(calendar_html(min(out_start, today), last_day, sel_cell), unsafe_allow_html=True)

    problems = []
    if not origins or not dests:
        problems.append("Choose at least one departure and one destination airport.")
    if not (want_return or want_oneway):
        problems.append("Tick return trips, one-way trips or both.")
    if out_end < out_start:
        problems.append("The outbound range ends before it starts.")
    if want_return and not pairs:
        problems.append("No return date fits these rules. Widen the stay, the return dates or the weekdays.")
    if (out_end - today).days > MAX_AHEAD:
        problems.append(f"Fares are usually only published about a year ahead; keep within {MAX_AHEAD} days.")
    for p in problems:
        st.warning(p)

    pause_others = st.checkbox("Pause routes that are not part of this selection", value=True,
                               help="Paused routes keep their history; you can turn them on again in Settings.")
    if st.button("✅ Apply these dates", type="primary", disabled=bool(problems)):
        wanted = [(o, d, t) for o in origins for d in dests if o != d
                  for t, on in (("return", want_return), ("one_way", want_oneway)) if on]
        with ctx.session_factory() as s:
            existing = {(c.origin, c.destination, c.trip_type): c for c in s.scalars(select(SearchConfiguration))}
            freq = next((c.frequency_hours for c in existing.values()), cfg.routes.frequency_hours)
            for key in wanted:
                c = existing.get(key)
                if c is None:
                    c = SearchConfiguration(origin=key[0], destination=key[1], trip_type=key[2],
                                            passengers=cfg.routes.passengers, frequency_hours=freq)
                    s.add(c)
                if ahead is None:
                    c.date_from, c.date_to = out_start, out_end
                    c.days_ahead_min, c.days_ahead_max = 0, max(1, (out_end - today).days)
                else:
                    c.date_from = c.date_to = None
                    c.days_ahead_min, c.days_ahead_max = ahead[0], max(1, ahead[1])
                c.departure_weekdays = wd_codes(dep_wd)
                if key[2] == "return":
                    c.min_stay_days, c.max_stay_days = (stay if stay else (None, None))
                    c.return_date_from, c.return_date_to = ret_start, ret_end
                    c.return_weekdays = wd_codes(ret_wd)
                else:
                    c.min_stay_days = c.max_stay_days = c.return_date_from = c.return_date_to = None
                    c.return_weekdays = ""
                c.enabled = True
                c.last_run_at = None  # search the new dates at the next scheduler check
            if pause_others:
                for k, c in existing.items():
                    if k not in wanted:
                        c.enabled = False
            s.commit()
        st.success(f"Saved for {len(wanted)} route(s). The next automatic check (within "
                   f"{cfg.scheduler.tick_minutes:g} min while `python main.py run` is running) searches them, "
                   "or press *Run search now* on the Overview page.")

# ================================================================== price calendar
with TAB_PRICES:
    with ctx.session_factory() as s:
        offers = reports.current_offers(s, cfg)
        budget = settings_service.get_budget(s, cfg)
        rows = []
        for o in offers:
            a = assess_offer(s, o, cfg)
            rows.append({"route": f"{o.origin}→{o.destination}", "origin": o.origin, "destination": o.destination,
                         "type": o.trip_type, "out": o.departure_at.date(),
                         "ret": o.return_departure_at.date() if o.return_departure_at else None,
                         "price": a.effective_price_pp, "currency": o.currency, "deal": a.category,
                         "link": o.booking_url})
    if not rows:
        st.info("No prices collected yet. Once searches have run, this shows the cheapest price for every day.")
    else:
        df = pd.DataFrame(rows)
        f1, f2, f3 = st.columns([1, 2, 2])
        kind = f1.radio("Trip", ["return", "one_way"], horizontal=True,
                        format_func=lambda k: "Return" if k == "return" else "One-way")
        routes = sorted(df.loc[df.type == kind, "route"].unique())
        chosen = f2.multiselect("Routes", routes, default=routes)
        cap = f3.slider("Max price per person", 10, int(max(df.price.max(), budget)) + 10,
                        int(min(budget, df.price.max()) + 1) if len(df) else int(budget))
        view = df[(df.type == kind) & df.route.isin(chosen) & (df.price <= cap)]
        if view.empty:
            st.warning("Nothing matches these filters.")
        else:
            by_day = view.sort_values("price").groupby("out").first()
            lo, hi = by_day.price.min(), by_day.price.max()

            def price_cell(d: date):
                if d < today:
                    return "past", str(d.day)
                if d not in by_day.index:
                    return "", str(d.day)
                p = by_day.loc[d, "price"]
                band = 1 if hi == lo else 1 + min(4, int((p - lo) / (hi - lo + 1e-9) * 5))
                return f"p{band}", f"{d.day}<small>{p:.0f}</small>"

            st.caption("Cheapest price per person on each **outbound** day (return trips: total return price, "
                       "including the Katowice transfer estimate). Green = cheapest.")
            st.markdown(calendar_html(min(by_day.index), max(by_day.index), price_cell), unsafe_allow_html=True)

            if kind == "return":
                st.markdown("##### Going × coming back")
                st.caption("Cheapest price for each combination. Rows = going, columns = coming back.")
                grid = view.sort_values("price").groupby(["out", "ret"]).price.first().unstack().astype(float)
                grid.index = [d.strftime("%a %d %b") for d in grid.index]
                grid.columns = [d.strftime("%a %d %b") for d in grid.columns]
                gl, gh = view.price.min(), view.price.max()

                def shade(v):
                    if pd.isna(v):
                        return ""
                    t = 0 if gh == gl else (v - gl) / (gh - gl)
                    r, g = int(22 + t * (239 - 22)), int(163 - t * (163 - 68))
                    return f"background-color: rgba({r},{g},60,.55)"

                css = grid.map(shade)
                shown = grid.map(lambda v: "" if pd.isna(v) else f"{v:.0f}")  # blank, not "None"
                st.dataframe(shown.style.apply(lambda _: css, axis=None))

            st.markdown("##### Cheapest dates")
            top = view.sort_values("price").head(15)
            st.dataframe(pd.DataFrame({
                "Route": top.route,
                "Going": [d.strftime("%a %d %b") for d in top.out],
                "Coming back": [d.strftime("%a %d %b") if d else "" for d in top.ret],
                "Price pp": top.price.round(2), "Deal": top.deal, "Link": top.link,
            }), hide_index=True, column_config={"Link": st.column_config.LinkColumn(display_text="open")})
            st.caption("Indicative prices from the enabled providers only — always check the booking site.")
