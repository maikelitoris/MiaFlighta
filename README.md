# ✈️ Flight Radar

Flight Radar is a personal flight price monitor for **Portugal (Porto OPO, Lisbon LIS) → Kraków (KRK) / Katowice (KTW)**. It:

* watches the exact trips you pick (for example *Fri 29 Jan → Mon 1 Feb*) and searches them automatically every 12 hours;
* stores every price it observes in a local SQLite database;
* scores deals with configurable rules;
* sends Telegram alerts for good and excellent deals;
* shows offers and price history in a Streamlit dashboard.

It runs entirely on your own computer. **Recurring cost: €0** (see [COSTS.md](COSTS.md)).

> **Data sources (both free).**
>
> * **Ryanair fare finder**: live, airline-direct Ryanair fares, for personal, non-commercial use as Ryanair's terms allow. No key needed.
> * **Travelpayouts / Aviasales Data API**: **cached** fares for other airlines, so they may have changed. Needs a free token.
>
> Every price is labelled *indicative* until you open the booking page. Wizz Air is not covered live, because its terms forbid scraping. Amadeus Self-Service shut down in July 2026. See [docs/PROVIDERS.md](docs/PROVIDERS.md), and [docs/PAID_APIS.md](docs/PAID_APIS.md) for paid options and their costs.

## 1. Install (once)

You need Python 3.12 or newer.

```bash
git clone <this repo> && cd MiaFlighta
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # Windows: copy .env.example .env
```

### Get the free Travelpayouts token

1. Sign up at <https://www.travelpayouts.com>. It's free.
2. Join the **Aviasales** program inside the dashboard.
3. Copy your **API token** from your profile (the API section) into `.env` as `TRAVELPAYOUTS_TOKEN=...`.
4. Check it works:

   ```bash
   python main.py check
   python scripts/check_travelpayouts.py        # one real request; shows raw + normalized data
   python scripts/check_ryanair.py              # Ryanair works without any key
   ```

### Create the Telegram bot (free)

1. In Telegram, open **@BotFather** and send `/newbot`. Copy the token into `.env` as `TELEGRAM_BOT_TOKEN=...`.
2. Run `python main.py run`, open your bot in Telegram and send `/start`. The bot replies with your chat id.
3. Put the chat id in `.env` as `TELEGRAM_CHAT_ID=...`, then restart.
4. Check it: `python main.py telegram-test` should deliver a test message.

Only your chat id can use the bot's commands.

## 2. Use it

```bash
python main.py init        # create data/flight_radar.db and the trips from config.yaml
python main.py search      # manual search now
python main.py status      # status + best current offers in the terminal
python main.py dashboard   # dashboard at http://localhost:8501
python main.py run         # automatic mode: scheduler + Telegram bot (leave it running)
```

**Automatic searches only happen while `python main.py run` is running and your computer is awake.** If the computer sleeps or the app is closed, nothing is searched. When it comes back, any overdue routes are searched at the next scheduler check (every 30 minutes).

Run the dashboard in a second terminal next to `run`.

### Keep it running all the time

`python scripts/keep_running.py` runs `main.py run` and restarts it whenever it stops (a crash, or no internet at startup). It waits longer between restarts if the app keeps failing straight away.

**Windows: start automatically at log-in.** Run this once from the project folder, in a normal PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1
```

* It creates a **FlightRadar** task in Task Scheduler for your user. The task starts `keep_running.py` hidden at every log-in, and Windows restarts the task too if it ever ends.
* Nothing is downloaded or installed.
* `scripts\windows\uninstall_autostart.ps1` removes it.
* Logs are in `data\logs\`: `flight_radar.log`, `keep_running.log` and `crash.log`.

**macOS / Linux:** add `@reboot cd /path/to/project && .venv/bin/python scripts/keep_running.py` with `crontab -e`, or use a launchd agent or systemd user service.

**The computer must be on and awake.** Searches can't run while it's off or asleep: set *sleep when plugged in* to *Never*. After a sleep, missed searches run at the next check, and a weekly report that was due is sent then. The weekly report also counts completed searches, so you'll notice if the PC was off.

### Telegram messages

| Message | When |
|---|---|
| **Price update** | After every scheduled search (every 12 h): the current best price of every watched trip, **deal or not**, the change since the last check, and the lowest price seen so far |
| **Weekly report** | Sunday 19:00 by default (`config.yaml → reports`): the week's lowest, current and highest price per trip, the trend, and the best pick right now. It says when there is too little history to judge |
| **Deal alert** | When an offer scores *good* or *excellent* (see Deal scoring) |

Both regular messages can be turned off, previewed or sent immediately on the dashboard's Trips page.

### Telegram commands

| Command | Action |
|---|---|
| `/status` | Health, last search, provider state |
| `/deals` | Best current offers |
| `/trips` | Latest price for every watched trip |
| `/report` | Weekly report now |
| `/routes` | Monitored routes with their ids |
| `/budget` | Show the budget. `/budget 120` sets the global budget; `/budget 3 90` sets route #3; `/budget 3 off` resets it |
| `/search` | Run a search now |
| `/help` | Help |

### Dashboard pages

* **Overview**: cheapest offers, best deals, a *Run search now* button, provider health, latest runs.
* **Trips**: the easy way to choose dates.
  * Pick a **going** and a **coming back** date, or a quick pattern (*Fri → Sun*, *Fri → Mon*, *Thu → Mon*…).
  * **Repeat weekly** adds the same weekdays for the following weeks in one click.
  * Choose the airports on each side and an optional max price.
  * Each watched trip shows the best price seen, a booking link, a *Watch* on/off switch and *Remove*.
  * *Schedule and request limits* sets how often trips are searched and shows how many requests each run will make against each provider's cap. It warns before you add more dates than one run can cover.
* **Flight Search**: filter stored offers by airport, going dates, coming-back dates, nights away, price, airline and stops.
* **Price History**: daily lowest, average and median prices, cheapest price by departure date, and price changes per itinerary.
* **Settings**: budget, frequency, Telegram on/off, providers (including resuming a paused provider), routes (edit, add, remove, date ranges, weekdays, stay length), and airports.

The dashboard shows only data that was actually collected. It never shows sample data.

## 3. Configuration

[`config.yaml`](config.yaml) holds the defaults. They are copied into the database on first `init`; after that, change things in the dashboard or with `/budget`.

* **Trips**: `trips:` lists exact date pairs to watch, e.g. `{outbound: 2027-01-29, back: 2027-02-01}`. Each is searched from every `routes.origins` airport to every `routes.destinations` airport (or the trip's own `origins`/`destinations`). Past trips are skipped. Usually you add trips on the dashboard's Trips page instead.
* **Rolling routes**: `routes.seed_rolling_routes: true` also creates "the next N days" searches. It is off by default, because watching every day costs many more requests than watching a few trips.
* **Routes**: origins × destinations × trip types, plus days ahead (1–60), stay length (3–10 days), weekdays (for example Fri/Sat out, Sun/Mon back), passengers, budget and frequency. After adding routes to the YAML, run `python main.py init --sync-routes`.
* **Katowice**: `transfer_costs.KTW` (default €15 per direction, an estimate you should adjust). It is added to KTW offers when scoring and checking the budget, because Katowice is an *alternative*, not the same as Kraków.
* **Deal scoring**: `scoring` (defaults below). Price thresholds apply to the effective price **per passenger**. Return trips are judged on the **total return price**, with their own thresholds.

| Rule | Default points |
|---|---|
| Effective price below €50 one-way / €100 return | +40 |
| Effective price below €70 one-way / €140 return | +25 |
| At least 30% below the median of comparable journeys | +30 |
| Direct (both directions) | +10 |
| Departure(s) between 07:00 and 21:00 local time | +10 |
| Checked baggage included | +10 |

Categories: **70 or more = excellent**, **50–69 = good**, below 50 = normal.

How the history comparison works:

* "Comparable" means the same route, trip type, currency and passenger count, the same baggage status, and a stay within ±2 days.
* At least 8 comparable itineraries from the last 60 days, observed on at least 3 different days, are needed. Until then, **no historical claim is made**, and the alert says so.

How alerts avoid repeats:

* An offer is alerted once.
* It is alerted again only if it gets at least 5% cheaper or its category improves.
* Failed sends are retried on the next run.

### Is it a good price?

Ryanair does not publish past prices, and no free source of historical Ryanair fares exists. Google Flights and Skyscanner price graphs have no free API. The app therefore compares only with prices it has actually collected, in two ways:

* **Other dates, from the first search on.** Each Ryanair request returns the cheapest fare for every day of a month. The app builds every other return trip of the same length from those calendars and ranks yours. For example: *"Average price: 5 of 13 other 3-night trips are cheaper (median €129.70). Cheaper: Sat 30 Jan → Tue 2 Feb €98.98"*. It labels a trip *good* when it's in the cheapest quarter, *expensive* when it's above most others, and says so when there are too few dates to compare.
* **The same trip over time.** Every 12-hour search adds a price observation. From the second day on, the Telegram update and the Trips page show the low, the high and the trend since the first price.

Every result is a **return trip**. It shows the going flight and the coming-back flight together, with their times and each leg's price, plus one booking link for both. On the Trips page, *Price calendar* shows a bar chart of daily fares around your going and coming-back dates.

### Request limits

| Provider | Official limit | What the app does | Your trips |
|---|---|---|---|
| Ryanair fare finder | None published. The endpoints are undocumented and Ryanair can change them or start blocking at any time. Personal, non-commercial use only | One request every 3 s and at most 80 requests per run. Stops and pauses Ryanair for 24 h at the first CAPTCHA, challenge or 403, without trying to get around it | One request covers a whole month of fares in one direction. Trips in the same months share requests: the 29 Jan weekend from OPO+LIS to KRK+KTW is at most 13 requests per run (9 in practice, because Ryanair doesn't fly to Katowice from there), ~18–26 a day at 12 h. More weekends in Jan–Feb cost almost nothing extra |
| Travelpayouts | About 600 requests/min | At most 100 requests per run, cached for 60 min | 1 request per airport pair and month. Skipped when no token is set |

When one run would need more requests than a cap allows, the remaining searches in that run are skipped and logged. The Trips page shows the estimate before you add dates.

## 4. How it works

```
main.py (CLI)
app/
  core/          config, polite HTTP client (rate limit, backoff, cache, bot-challenge stop), time utils
  models/        SQLAlchemy tables + Pydantic FlightOfferData / SearchQuery
  database/      engine, seeding, SQLite response cache
  providers/     FlightProvider interface + Travelpayouts adapter + registry
  services/      search planner, search orchestrator, offer store (dedupe + history),
                 deal engine, alerts, provider health, settings, reports
  notifications/ Telegram message formatting/sending + command bot
  scheduler/     APScheduler tick job
  dashboard/     Streamlit (Overview + pages/)
tests/           pytest, mocked HTTP only, fixtures labelled TEST DATA
docs/PROVIDERS.md   provider research
```

### Database tables

| Table | Holds |
|---|---|
| `airports` | Airport codes, names and time zones |
| `search_configurations` | Your routes and their settings |
| `flight_offers` | One row per itinerary, keyed by a fingerprint. Holds the latest price |
| `price_history` | **Every** observation, append-only |
| `alerts` | Deal alerts and their notification status |
| `search_runs` | One row per route search: start and end times, results, errors |
| `provider_states` | Provider health and pauses |
| `app_settings` | Settings changed in the dashboard or Telegram |
| `response_cache` | Cached API responses |

### Data handling

* **Times.** Flight times are stored as local airport times plus the UTC offset. Arrival times are derived from the flight duration and flagged as estimates.
* **Duplicates.** Offers are de-duplicated with a provider-independent fingerprint: route, airline, flight number, local departure and return times, and passengers. The same flight from two providers becomes one offer, and the history records which provider reported each price.
* **Cache hits.** Results served from the cache are not recorded as new price observations.
* **Blocked access.** If a provider returns a CAPTCHA, bot challenge or access-denied response, it is paused, the problem is logged, and nothing is bypassed. The other providers keep working.

Logs are written to `data/logs/flight_radar.log`.

## 5. Tests

```bash
python -m pytest
```

The tests use mocked HTTP only. They never call paid or live APIs. Fixtures are explicitly labelled **TEST DATA**.

## 6. Docker (optional)

```bash
docker compose up -d     # scheduler/bot + dashboard on http://127.0.0.1:8501, data stays in ./data
```

## Roadmap (phase 6)

* More providers. Any paid option needs your approval first.
* A smarter flexible-date optimiser, for example the cheapest weekend combinations across all three Portuguese airports.
* Richer price analytics.
* Estimates of Porto/Lisbon/Faro ground costs from your home.
