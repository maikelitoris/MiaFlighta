# Flight data providers: research notes

Researched November 2026. Terms and prices change, so re-check before relying on any of this.
Nothing here is assumed to be free just because it has a developer portal.

## Summary

| Source | Free? | Sign-up | API key | Real or test prices | Coverage | Limits | OK for this project? |
|---|---|---|---|---|---|---|---|
| **Travelpayouts / Aviasales Data API** | ✅ Yes | Travelpayouts account | Token | **Real but cached** (prices Aviasales users found in roughly the last 2–7 days). Indicative only | Whatever Aviasales sells and its users searched recently. Gaps on quiet routes and dates | About 600 req/min on `prices_for_dates` | ✅ **Implemented** (primary provider) |
| Amadeus for Developers (Self-Service) | ❌ Gone | — | — | — | — | — | ❌ **Shut down 17 July 2026.** Self-service keys were disabled; only the Enterprise offering (sales contact) remains |
| Duffel | Test mode free. Live is pay-as-you-go | Business account + KYC for live | Yes | Test mode uses sandbox and test airlines ("Duffel Airways"), so **not real prices**. Live gives real NDC/GDS fares | Many full-service carriers. Low-cost carriers vary | Reportedly $3 per order, plus **$0.005 per search above a 1500:1 search-to-book ratio**. A monitor that never books would pay for searches | ❌ Not implemented: costs money in live mode, and test data is fake |
| Skyscanner Travel API | Partner only | Application | Yes | Live | Broad | Partner terms. Caching reportedly restricted | ❌ Requires an established business (around 100k monthly visitors) |
| Kiwi.com Tequila | Closed | Invitation or partnership only since 2024 | — | — | — | — | ❌ Not available to individuals |
| Aviasales Flight Search API (real-time) | — | Travelpayouts | — | Live | — | Needs a project with **50,000+ monthly active users** | ❌ Not eligible |
| **Ryanair fare finder** (the JSON endpoints ryanair.com itself uses) | ✅ Yes | No | No | **Real, live, airline-direct**: lowest fare per day, one adult, basic fare without bags | Ryanair only | We use ~30 requests per run at 1 request every 3 s | ✅ **Implemented, for personal use only.** Ryanair's terms allow use of the site, data and APIs for *private, non-commercial purposes*, and ban screen-scraping *for commercial purposes*. Ryanair litigates against commercial scrapers, so do not use or republish this commercially. Undocumented endpoints may change. Verified live on 12 Nov 2026 |
| Wizz Air website | — | — | — | — | — | — | ❌ No public developer API. B2B distribution is via Kyte for travel sellers. Scraping not permitted. Will not be automated |
| TAP / LOT / others | — | — | — | — | — | — | No public self-serve fare APIs found |
| Google Flights | — | — | — | — | — | — | ❌ No API. Automated access is against Google's terms |
| SerpApi (Google Flights results) | Free tier, reportedly 100–250 searches/month | Account | Yes | Live (scraped by SerpApi) | Google Flights coverage | Free tier is very small. Paid from about $25/month | ⏸️ Possible future option. **Would need your approval**, and the free tier is too small for 12 routes × 4 runs/day |
| Ignav | 1,000 free requests, then $2 per 1,000 | Account, no card | Yes | Live consumer fares | Broad (search + booking links) | — | ⏸️ Possible future option. **Would cost money** after the free requests; at our volume, roughly a few €/month. Needs your approval |

## Why the MVP uses Travelpayouts

It is the only source found that meets all of these at once:

* costs €0;
* lets an individual sign up on their own;
* is legitimate (an official, documented API, not scraping);
* returns real airline prices, not test data.

Its limits, and how the app handles each:

* **Prices are cached, not live.** Every offer is stored as `price_type = indicative`. Telegram messages and the dashboard say clearly that the price may have changed.
* **Coverage is incomplete.** If nobody searched a route or date on Aviasales recently, there is no data for it. The app never claims all airlines were checked.
* **Ryanair and Wizz Air**, the main low-cost carriers between Portugal and southern Poland, may be only partly visible through agency data. This is a known blind spot, and only the airlines' own sites are complete.
* **No baggage data.** Baggage is stored as *unknown* and never assumed.
* **Prices are per adult.** For several passengers the app multiplies the price, which is an approximation.

## Web scraping

The Ryanair provider (`app/providers/ryanair.py`) is the one scraping approach that fits the project rules:

* **What it calls.** It requests the same public JSON the ryanair.com fare calendar uses. One request returns a month of daily lowest fares, which is much lighter than driving a browser.
* **Honest client.** It sends an honest `FlightRadar/…` User-Agent and does not pretend to be a browser.
* **Low volume.** It caches responses and waits 3 seconds between requests.
* **Stops on blocks.** It stops for 24 hours on any CAPTCHA, challenge or denial.
* **Diagnostic.** `python scripts/check_ryanair.py` checks it.

Excluded on purpose:

* **Wizz Air**: its website terms prohibit screen-scraping, and its booking API sits behind Kasada bot protection. Getting through would mean evading bot detection, which this project does not do.
* **Google Flights / Skyscanner / Kayak websites**: their terms prohibit automated access. Use a paid API vendor instead (see [PAID_APIS.md](PAID_APIS.md)).

**Playwright is not needed.** The Ryanair endpoints return JSON directly. Running a headless browser would only add load and mostly serves to look like a human, which we explicitly avoid.

The shared HTTP client already has the safety behaviour the brief asks for:

* it detects CAPTCHA, bot-challenge and access-denied responses;
* it stops that provider for `blocked_pause_hours` and logs an error;
* it does not retry and does not try to bypass the block.

A future Playwright provider must reuse this, and should only be added for a site whose terms allow it.

## Adding a provider

1. Create `app/providers/<name>.py` subclassing `FlightProvider` (`search_flights`, `get_offer_details`, `health_check`, `get_provider_name`).
2. Return `FlightOfferData` objects with the correct `price_type`.
3. Raise `app.core.errors` exceptions on failure.
4. Register it in `app/providers/registry.py` and add a `providers:` entry in `config.yaml`.
5. Add tests with mocked HTTP (`httpx.MockTransport`) and fixtures labelled TEST DATA.

## Sources

* Amadeus self-service shutdown:
  * [Amadeus statement](https://amadeus.com/en/industry-messaging/statement-regarding-amadeus-for-developers-portal)
  * [PhocusWire](https://www.phocuswire.com/amadeus-shut-down-self-service-apis-portal-developers)
  * [Skift](https://skift.com/2026/09/03/amadeus-brings-travel-tech-into-claude/)
* Travelpayouts:
  * [Data API](https://support.travelpayouts.com/hc/en-us/articles/203956163)
  * [Rate limits](https://support.travelpayouts.com/hc/en-us/articles/4402565416594)
  * [Data API access requirements](https://support.travelpayouts.com/hc/en-us/articles/203956083-Requirements-for-Aviasales-data-API-access)
  * [Search API (50k MAU)](https://support.travelpayouts.com/hc/en-us/articles/210995808-How-to-get-access-to-the-Aviasales-Search-API)
* Duffel:
  * [Pricing](https://duffel.com/pricing)
  * [Test mode](https://duffel.com/docs/api/overview/test-mode)
* Skyscanner:
  * [Acceptance criteria](https://skyscannerpartnersupport.zendesk.com/hc/en-us/articles/10881149122717-What-is-the-acceptance-criteria-for-the-Travel-API)
* Kiwi Tequila:
  * [Ignav notes](https://ignav.com/docs/kiwi-tequila-alternative)
* Ryanair:
  * [Terms of use](https://www.ryanair.com/gb/en/lp/legal/terms-of-use)
  * [Endpoint reference: ryanair-py](https://pypi.org/project/ryanair-py)
  * [High Court ruling](https://corporate.ryanair.com/news/ryanair-welcomes-high-court-ruling-confirming-screenscraping-is-unlawful/)
* Wizz Air:
  * [Website terms (DE)](https://wizzair.com/cms/api/docs/default-source/downloadable-documents/website_terms_and_conditions_de_4a3bca35-bc8c-4a18-a2c3-816d1252fe9b.pdf)
  * [Kyte distribution](https://www.altexsoft.com/travel-industry-news/wizz-air-opens-api-access-to-travel-sellers-through-kyte/)
* SerpApi:
  * [Pricing overview](https://www.costbench.com/software/web-scraping/serpapi/free-plan/)
* Ignav:
  * [Pricing](https://ignav.com/pricing)
