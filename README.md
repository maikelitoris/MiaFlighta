# ✈️ Flight Radar

Flight Radar is a personal flight price monitor for **Portugal (OPO, LIS, FAO) → Kraków (KRK) / Katowice (KTW)**. It:

* searches automatically every 12 hours;
* sends a Telegram price summary every 12 hours, plus an instant alert for good deals;
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
python main.py init        # create data/flight_radar.db and the 12 default routes
python main.py search      # manual search now
python main.py status      # status + best current offers in the terminal
python main.py dashboard   # dashboard at http://localhost:8501
python main.py run         # automatic mode: scheduler + Telegram bot (leave it running)
python main.py digest      # send the 12-hour Telegram summary right now (--print to only show it)
./scripts/start.sh         # run + dashboard together (WSL/Linux/macOS)
```

### Telegram every 12 hours

While `python main.py run` is running you get:

* a **summary every 12 hours** (`digest.every_hours` in `config.yaml`, 0 = off) with the cheapest current offer per route within your budget — even when nothing is a "deal";
* an **instant alert** whenever a good or excellent deal appears.

The last summary time is stored in the database, so restarting doesn't send duplicates. If the PC was asleep, the overdue summary goes out at the next check.

### Starting it automatically on Windows (WSL)

1. Find the project path inside WSL (for example `/home/<you>/MiaFlighta`).
2. Open **Task Scheduler → Create Task**. Trigger: *At log on*. Action: *Start a program*
   * Program: `wsl.exe`
   * Arguments: `-d Ubuntu-24.04 -- bash -lc "/home/<you>/MiaFlighta/scripts/start.sh"`
3. In Windows *Power & sleep*, set the PC to never sleep while plugged in (searches pause while it sleeps).

**Automatic searches only happen while `python main.py run` is running and your computer is awake.** If the computer sleeps or the app is closed, nothing is searched. When it comes back, any overdue routes are searched at the next scheduler check (every 30 minutes).

Run the dashboard in a second terminal next to `run`. To start it automatically at login, use Task Scheduler (Windows), a Login Item or launchd (macOS), or a systemd user service / `@reboot` cron entry (Linux).

### Telegram commands

| Command | Action |
|---|---|
| `/status` | Health, last search, provider state |
| `/deals` | Best current offers |
| `/routes` | Monitored routes with their ids |
| `/budget` | Show the budget. `/budget 120` sets the global budget; `/budget 3 90` sets route #3; `/budget 3 off` resets it |
| `/search` | Run a search now |
| `/summary` | The 12-hour price summary, now |
| `/help` | Help |

### Dashboard pages

* **Trip dates**: pick when you go and when you come back on a calendar — exact date ranges or a rolling window, stay length, weekdays, and presets like *Weekend (Fri/Sat → Sun/Mon)*. A live calendar shows the selected days, and **Apply** updates the routes. The *Price calendar* tab colours each outbound day by its cheapest price and shows a going × coming-back price grid.

* **Overview**: cheapest offers, best deals, a *Run search now* button, provider health, latest runs.
* **Flight Search**: filter stored offers by airport, dates, price, airline and stops.
* **Price History**: daily lowest, average and median prices, cheapest price by departure date, and price changes per itinerary.
* **Settings**: budget, frequency, Telegram on/off, providers (including resuming a paused provider), routes (edit, add, remove, date ranges, weekdays, stay length), and airports.

The dashboard shows only data that was actually collected. It never shows sample data.

## 3. Configuration

[`config.yaml`](config.yaml) holds the defaults. They are copied into the database on first `init`; after that, change things in the dashboard or with `/budget`.

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
