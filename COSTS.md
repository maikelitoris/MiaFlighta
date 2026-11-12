# Costs

**Current recurring cost: €0/month.**
No paid service, subscription, proxy, server or AI API is used.

| Component | Cost | Registration | Quota / limits | Could it ever cost money? |
|---|---|---|---|---|
| Python, SQLAlchemy, Pydantic, httpx, APScheduler, Streamlit, pandas, pytest, python-dotenv, PyYAML, tzdata | Free, open source | No | — | No |
| python-telegram-bot + Telegram Bot API | Free | Telegram account, then create a bot with @BotFather | About 30 messages/sec. We send a handful per day | No |
| SQLite database (`data/flight_radar.db`) | Free, local file | No | — | No |
| Ryanair fare finder (personal use) | Free | No | We self-limit to 1 request every 3 s, about 30 per run | No |
| Travelpayouts / Aviasales Data API | Free | Travelpayouts account, then copy the API token | About 600 requests/min. We use about 30 requests every 6 hours | Not for the Data API |
| Your computer (runs scheduler + dashboard) | Electricity only | — | Must stay on and running | — |
| Docker (optional) | Free | No | — | Docker Desktop is free for personal use |

## API usage budget

* 12 routes × about 2–3 months per 60-day window gives **about 30 requests per run**.
* At 4 runs per day, that is **about 120 requests per day**.
* Identical requests within `cache_ttl_minutes` come from the local cache instead.
* `max_requests_per_run` puts a hard cap on each run.

## Paid APIs

Prices and an estimate of what each would cost for our search volume are in [docs/PAID_APIS.md](docs/PAID_APIS.md). In short:

* **Ignav**: about $10–15/month for weekend-only searches.
* **SearchApi.io**: $40/month flat for calendar searches.
* Everything else costs more or isn't available to individuals.

## Things that would cost money (NOT used, need your approval first)

| Option | Why you might want it | Cost | Free alternative and its limits |
|---|---|---|---|
| Ignav API | Live (not cached) fares | 1,000 free requests, then about $2 per 1,000 | Travelpayouts cached data: free, but indicative and with coverage gaps |
| SerpApi (Google Flights) | Google Flights coverage | Free tier is too small (100–250/month). From about $25/month | — |
| Duffel live mode | Bookable NDC/GDS fares | Pay per order, plus fees when searches far exceed bookings | Duffel test mode is free but returns **test data only**, so it is useless for real prices |
| Cloud server / VPS | Searches while your PC is off | About €4–6/month | Keep your PC on, or run it on an old laptop or Raspberry Pi you already own |

The app never subscribes to anything and never makes paid API calls. Every provider in the code is free.
