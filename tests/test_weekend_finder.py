"""Weekend finder: shared destinations, totals, visits, messages. All fares are TEST DATA."""

from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import func, select

from app.core.config import FinderTraveler, WeekendFinderConfig
from app.models.db_models import WeekendFare
from app.providers.ryanair import RyanairProvider
from app.services import weekend_finder as wf
from app.services.provider_health import is_paused

TODAY = date(2027, 1, 20)

# TEST DATA: {origin: {destination: (name, country, out price, in price)}}
PRICES = {
    "OPO": {"BGY": ("Milan Bergamo", "Italy", 30.0, 34.0), "AGP": ("Malaga", "Spain", 20.0, 25.0),
            "KRK": ("Krakow", "Poland", 50.0, 55.0), "DUB": ("Dublin", "Ireland", 10.0, 10.0)},
    "LIS": {"AGP": ("Malaga", "Spain", 15.0, 20.0), "BGY": ("Milan Bergamo", "Italy", 60.0, 60.0)},
    "KRK": {"BGY": ("Milan Bergamo", "Italy", 20.0, 20.0), "AGP": ("Malaga", "Spain", 40.0, 45.0),
            "OPO": ("Porto", "Portugal", 30.0, 35.0), "STN": ("London Stansted", "United Kingdom", 9.0, 9.0)},
}


def rt_payload(origin, out_day, back_day, surcharge=0.0):
    fares = []
    for dest, (name, country, p_out, p_in) in PRICES[origin].items():
        p_out, p_in = p_out + surcharge, p_in + surcharge
        fares.append({
            "outbound": {"departureAirport": {"iataCode": origin},
                         "arrivalAirport": {"iataCode": dest, "name": name, "countryName": country,
                                            "city": {"name": name.split()[0]}},
                         "departureDate": f"{out_day}T18:10:00", "arrivalDate": f"{out_day}T21:00:00",
                         "price": {"value": p_out, "currencyCode": "EUR"}, "flightNumber": "FR1"},
            "inbound": {"departureAirport": {"iataCode": dest}, "arrivalAirport": {"iataCode": origin},
                        "departureDate": f"{back_day}T17:15:00", "arrivalDate": f"{back_day}T19:00:00",
                        "price": {"value": p_in, "currencyCode": "EUR"}, "flightNumber": "FR2"},
            "summary": {"price": {"value": round(p_out + p_in, 2), "currencyCode": "EUR"}, "tripDurationDays": 3},
        })
    return {"fares": fares}


class FakeFinder:
    def __init__(self, surcharge=0.0, block=False):
        self.surcharge, self.block, self.calls = surcharge, block, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.ryanair.com":
            return httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"})
        if self.block:
            return httpx.Response(403, text="<html>Access denied - captcha</html>", headers={"content-type": "text/html"})
        p = request.url.params
        self.calls.append((p["departureAirportIataCode"], p["outboundDepartureDateFrom"]))
        return httpx.Response(200, json=rt_payload(p["departureAirportIataCode"], p["outboundDepartureDateFrom"],
                                                   p["inboundDepartureDateFrom"], self.surcharge))


def provider(fake):
    return RyanairProvider(transport=httpx.MockTransport(fake), sleep=lambda s: None, min_interval_seconds=0)


def finder_config(config, **kw):
    kw = {"start": date(2027, 2, 5), "weeks": 2} | kw
    cfg = WeekendFinderConfig(enabled=True, travelers=[
        FinderTraveler(name="You", origins=["OPO", "LIS"]), FinderTraveler(name="Mia", origins=["KRK"])], **kw)
    return config.model_copy(update={"weekend_finder": cfg})


def test_weekends_are_fridays_to_mondays_after_start():
    cfg = WeekendFinderConfig(start=date(2027, 2, 3), weeks=3)  # a Wednesday
    assert wf.weekends(cfg, TODAY) == [(date(2027, 2, 5), date(2027, 2, 8)), (date(2027, 2, 12), date(2027, 2, 15)),
                                       (date(2027, 2, 19), date(2027, 2, 22))]
    assert wf.weekends(cfg, date(2027, 2, 10)) == [(date(2027, 2, 12), date(2027, 2, 15)),
                                                   (date(2027, 2, 19), date(2027, 2, 22))]  # past ones dropped


def test_run_stores_one_request_per_airport_per_weekend(config, session):
    cfg = finder_config(config)
    fake = FakeFinder()
    r = wf.run_finder(session, cfg, provider(fake), today=TODAY)
    assert (r.weekends, r.errors) == (2, [])
    assert len(fake.calls) == 6  # 2 weekends x OPO, LIS, KRK
    assert session.scalar(select(func.count(WeekendFare.id))) == 2 * (4 + 2 + 4)


def test_shared_destinations_ranked_by_total(config, session):
    cfg = finder_config(config)
    wf.run_finder(session, cfg, provider(FakeFinder()), today=TODAY)
    meetups, visits = wf.combine(session, cfg)
    first = [m for m in meetups if m.out_day == date(2027, 2, 5)]
    # Malaga: you 35 (from LIS, cheaper than OPO 45) + Mia 85 = 120; Bergamo: you 64 + Mia 40 = 104
    assert [(m.destination, m.total) for m in first] == [("Milan", 104.0), ("AGP", 120.0)]
    agp = first[1]
    assert {(leg.traveler, leg.fare.origin, leg.fare.price) for leg in agp.legs} == {("You", "LIS", 35.0),
                                                                                       ("Mia", "KRK", 85.0)}
    # Dublin (only you) and London (only Mia) are not shared; home airports are visits, not meetups
    assert not {"DUB", "STN", "KRK", "OPO"} & {m.destination for m in meetups}
    v = {(x.visitor, x.fare.destination, x.fare.price) for x in visits if x.out_day == date(2027, 2, 5)}
    assert v == {("You", "KRK", 105.0), ("Mia", "OPO", 65.0)}


def test_excluded_destinations_are_skipped(config, session):
    cfg = finder_config(config, exclude_destinations=["BGY"])
    wf.run_finder(session, cfg, provider(FakeFinder()), today=TODAY)
    meetups, _ = wf.combine(session, cfg)
    assert {m.destination for m in meetups} == {"AGP"}  # BGY excluded before Milan grouping


def test_messages_preview_and_overview(config, session):
    cfg = finder_config(config)
    p = provider(FakeFinder())
    wf.run_finder(session, cfg, p, today=TODAY)
    top, months, overview = wf.finder_messages(session, cfg, p)
    assert "🗓️ Best weekend each month · Feb 2027 → Feb 2027" in months
    assert "<b>Feb 2027</b>: Milan 5–8 Feb · <b>€104.00</b> (You €64.00 + Mia €40.00)" in months
    assert top.startswith("<b>🌍 Best weekend: 5–8 Feb Milan · €104.00 together</b>")
    assert "You €64.00 Porto→BGY" in top and "Mia €40.00 Kraków→BGY" in top
    assert "book</a>" in top and "Visiting each other" in top and "Mia → Porto: €65.00" in top
    assert "<b>5–8 Feb</b>: Milan €104.00 · Malaga €120.00" in overview
    assert len(top) <= 4096 and len(overview) <= 4096


def test_new_low_is_flagged_against_earlier_runs(config, session):
    cfg = finder_config(config)
    wf.run_finder(session, cfg, provider(FakeFinder(surcharge=10)), today=TODAY,
                  now=datetime(2027, 1, 19, 6, 0))
    wf.finish_run(session, cfg)
    wf.run_finder(session, cfg, provider(FakeFinder()), today=TODAY, now=datetime(2027, 1, 19, 18, 0))
    meetups, _ = wf.combine(session, cfg)
    bgy = next(m for m in meetups if m.destination == "Milan" and m.out_day == date(2027, 2, 5))
    assert bgy.previous_low == 144.0 and bgy.total == 104.0  # 4 legs x +10 before
    top = wf.finder_messages(session, cfg)[0]
    assert "🔻 new low (was €144.00)" in top


def test_request_budget_is_respected(config, session):
    cfg = finder_config(config, max_requests_per_run=4)
    fake = FakeFinder()
    r = wf.run_finder(session, cfg, provider(fake), today=TODAY)
    assert len(fake.calls) <= 4 and any("budget" in e for e in r.errors)


def test_block_pauses_ryanair_and_stops(config, session):
    cfg = finder_config(config)
    r = wf.run_finder(session, cfg, provider(FakeFinder(block=True)), today=TODAY)
    assert r.errors and is_paused(session, "ryanair")
    again = wf.run_finder(session, cfg, provider(FakeFinder()), today=TODAY)
    assert again.skipped_reason and "paused" in again.skipped_reason


def test_disabled_finder_does_nothing(config, session):
    r = wf.run_finder(session, config, provider(FakeFinder()), today=TODAY)
    assert r.skipped_reason == "weekend finder is off"


def test_due_logic(session):
    now = datetime(2027, 1, 19, 12, 0)
    assert wf.is_due(session, 12, now)
    from app.services import settings_service
    settings_service.set_setting(session, wf.LAST_RUN_SETTING, (now - timedelta(hours=3)).isoformat())
    assert not wf.is_due(session, 12, now)
    assert wf.is_due(session, 2, now)


def test_scheduler_runs_finder_and_sends_two_messages(config, session_factory):
    from app.scheduler.scheduler import run_weekend_finder
    from app.services.search_service import SearchService
    from tests.test_deals_and_alerts import FakeNotifier

    cfg = finder_config(config)
    cfg = cfg.model_copy(update={"weekend_finder": cfg.weekend_finder.model_copy(update={"start": date.today() + timedelta(days=3)})})
    n = FakeNotifier()
    svc = SearchService(cfg, session_factory, {"ryanair": provider(FakeFinder())}, n)
    r = run_weekend_finder(svc, force=True)
    assert r.fares > 0 and len(n.messages) == 3 and "🌍" in n.messages[0] and "🗓️" in n.messages[1]
    assert run_weekend_finder(svc) is None  # not due again right away


def test_same_city_different_airports_counts_as_meeting(config, session):
    cfg = finder_config(config)
    PRICES["OPO"]["STN"] = ("London Stansted", "United Kingdom", 25.0, 25.0)
    PRICES["KRK"]["LTN"] = ("London Luton", "United Kingdom", 20.0, 20.0)
    try:
        wf.run_finder(session, cfg, provider(FakeFinder()), today=TODAY)
        meetups, _ = wf.combine(session, cfg)
        london = next(m for m in meetups if m.destination == "London" and m.out_day == date(2027, 2, 5))
        # Mia's cheapest London airport is Stansted (18) here, not Luton (40)
        assert london.total == 50.0 + 18.0 and london.city == "London"
    finally:
        del PRICES["OPO"]["STN"], PRICES["KRK"]["LTN"]


def test_country_filter_and_one_sided_note(config, session):
    cfg = finder_config(config, countries=["United Kingdom", "Spain", "Georgia"])
    PRICES["OPO"]["STN"] = ("London Stansted", "United Kingdom", 25.0, 25.0)
    try:
        p = provider(FakeFinder())
        wf.run_finder(session, cfg, p, today=TODAY)
        meetups, _ = wf.combine(session, cfg)
        assert {m.country for m in meetups} <= {"United Kingdom", "Spain"}  # no Italy
        assert {m.destination for m in meetups} >= {"London", "AGP"}
        top, months, overview = wf.finder_messages(session, cfg, p)
        assert "No Ryanair flights from your airports:</b> Georgia" in overview
    finally:
        del PRICES["OPO"]["STN"]
    sided = wf.one_sided(session, cfg.model_copy(update={"weekend_finder": cfg.weekend_finder.model_copy(
        update={"countries": ["Ireland"]})}))
    assert sided == {"Ireland": {"You": {"Dublin"}}}  # only you fly to Dublin in the TEST data


def test_finish_run_keeps_only_latest_snapshot(config, session):
    cfg = finder_config(config)
    wf.run_finder(session, cfg, provider(FakeFinder(surcharge=5)), today=TODAY, now=datetime(2027, 1, 19, 6, 0))
    wf.finish_run(session, cfg)
    wf.run_finder(session, cfg, provider(FakeFinder()), today=TODAY, now=datetime(2027, 1, 19, 18, 0))
    wf.finish_run(session, cfg)
    times = set(session.scalars(select(WeekendFare.observed_at)))
    assert times == {datetime(2027, 1, 19, 18, 0)}


def test_long_overview_is_split_into_telegram_sized_messages(config, session):
    cfg = finder_config(config, weeks=53, max_requests_per_run=250)
    p = provider(FakeFinder())
    wf.run_finder(session, cfg, p, today=TODAY)
    messages = wf.finder_messages(session, cfg, p)
    assert len(messages) >= 2 and all(len(m) <= 4096 for m in messages)
    assert sum(m.count("<b>") for m in messages[2:]) >= 53  # every weekend listed
    assert messages[1].count("<b>") >= 13  # one line per month (Feb 2027 ... Feb 2028) + title


def test_slow_interval_wins_over_cloud_force(config, session_factory):
    from app.scheduler.scheduler import run_weekend_finder
    from app.services.search_service import SearchService
    from tests.test_deals_and_alerts import FakeNotifier

    cfg = finder_config(config, start=date.today() + timedelta(days=3), every_hours=20)
    svc = SearchService(cfg, session_factory, {"ryanair": provider(FakeFinder())}, FakeNotifier())
    assert run_weekend_finder(svc, force=True) is not None   # first run of the day
    assert run_weekend_finder(svc, force=True) is None       # 12 h later the cloud forces: still once a day


def test_manual_run_ignores_once_a_day_interval(config, session_factory):
    from app.scheduler.scheduler import run_weekend_finder
    from app.services.search_service import SearchService
    from tests.test_deals_and_alerts import FakeNotifier

    cfg = finder_config(config, start=date.today() + timedelta(days=3), every_hours=20)
    svc = SearchService(cfg, session_factory, {"ryanair": provider(FakeFinder())}, FakeNotifier())
    assert run_weekend_finder(svc, force=True) is not None
    assert run_weekend_finder(svc, force=True, ignore_interval=True) is not None  # "Run workflow" button
