# Paid flight APIs: prices and estimated cost for Flight Radar

Researched November 2026, mostly from vendor pages and third-party price trackers. Several vendors don't publish complete prices, so **check the vendor's pricing page before paying**. Nothing here is implemented, and nothing will be without your approval.

## What a paid API would add

The free setup already gives you:

| Source | What you get |
|---|---|
| **Ryanair fare finder** | Live, airline-direct Ryanair fares |
| **Travelpayouts** | Cached fares for other airlines |

A paid API would add **live** prices for Wizz Air, TAP, LOT, easyJet and connecting itineraries, mostly through Google Flights data.

## How many searches we would need

There are 3 origins × 2 destinations = 6 airport pairs. Searching both directions over a 60-day window, the volume depends on the API style and how often we search:

| Search style | Per run | 4 runs/day (monthly) | 1 run/day (monthly) |
|---|---|---|---|
| A. Every date, one-way both directions (return trips built from the two one-ways) | 720 | 86,400 | 21,600 |
| B. Weekends only (Fri/Sat out, Sun/Mon back), one-way both directions | ~204 | ~24,500 | ~6,100 |
| C. Calendar APIs: one request returns many dates; about 12 one-way + 48 return (8 trip lengths) | ~60 | ~7,200 | ~1,800 |

## Price per provider and estimated monthly cost

| Provider | Data | Pricing (as found) | A, 1 run/day | B, 1 run/day | C, 4 runs/day | C, 1 run/day |
|---|---|---|---|---|---|---|
| **Ignav** | Live consumer fares + booking links | 1,000 free requests (one-time), then **$2 per 1,000**. No monthly minimum, no card for the free tier | ~$43 | **~$12** | n/a (no calendar found) | n/a |
| **SearchApi.io** (Google Flights + Google Flights Calendar engines) | Google Flights, live | 100 free requests (one-time). **$40/mo for 10k**, $100/mo for 35k, $250/mo for 100k (~$4 per 1,000) | $100 | $40 | **$40** | **$40** (minimum plan) |
| **SerpApi** (Google Flights + Calendar) | Google Flights, live | Free 100–250/mo. $25/mo for 1k, $75/mo for 5k, $150/mo for 15k, $275/mo (higher tier). Sources disagree | $275+ | $150 | $150 | $75 |
| **Apify Google Flights actors** (third-party scrapers) | Google Flights, live | ~$1.20–$4 per 1,000 searches depending on the actor, plus Apify's $5/mo free credit | ~$21–81 | ~$2–20 | — | — |
| **FlightAPI.io** | Live (aggregated) | Pricing not verifiable; a reseller lists ~$0.003/request | ~$65 (unverified) | ~$18 (unverified) | — | — |
| **Duffel** (live mode) | Bookable NDC/GDS fares | Searches are free up to a 1500:1 search-to-booking ratio, then $0.005/search. A monitor that never books pays for every search. Needs business KYC; low-cost coverage is limited | ~$108 | ~$31 | — | — |
| Amadeus Enterprise | GDS | Contract only (self-service closed July 2026) | — | — | — | — |
| Skyscanner / Kiwi Tequila | — | Partner or invitation only | — | — | — | — |

Notes:

* The Google Flights APIs (SearchApi, SerpApi, Apify) get their data by scraping Google; Google offers no official Flights API. Any terms-of-service risk is the vendor's, but they can break or change without notice.
* "Calendar" engines return one price per date for a given trip, so they suit "find the cheapest dates" monitoring and are by far the most request-efficient.

## Recommendation

1. **Stay at €0 for now.** Ryanair (live, direct) plus Travelpayouts (cached, other airlines) covers the main low-cost routes. Watch the results for 1–2 weeks.
2. **If you want live Wizz Air, TAP and LOT prices too**, the cheapest sensible options are:
   * **Ignav, weekends only, 1 run/day: about $10–15/month**, pay-as-you-go, and the first 1,000 requests are free (about 5 days of testing);
   * **SearchApi.io with the Google Flights Calendar engine: $40/month flat**, enough for calendar searches 4× a day on every route.
3. Not worth it for this project: SerpApi (more expensive for the same data), Duffel (costs money just to search and needs a business account), Amadeus, Skyscanner and Kiwi (not available).

Tell me which one (if any) you'd like and I'll implement it as another provider, with a hard monthly request cap so it can never exceed the budget you set.

## Sources

* Ignav:
  * [Pricing](https://ignav.com/pricing)
* SearchApi:
  * [Pricing](https://searchapi.io/pricing)
  * [Capterra listing](https://www.capterra.com/p/10009863/SearchApi/)
  * [Calendar API](https://www.searchapi.io/docs/google-flights-calendar-api)
* SerpApi:
  * [Calendar API](https://serpapi.com/google-flights-calendar-api)
  * [Price tracker (Costbench)](https://www.costbench.com/software/ai-search-apis/serpapi-search/)
  * [Activepieces guide](https://www.activepieces.com/blog/serp-api-pricing-plans-and-costs-2026-guide.md)
* Benchmark of four Google Flights APIs:
  * [FlightPowers](https://flightpowers.com/guides/flight-api-benchmark-2026)
* Apify:
  * [Google Flights Search API actor](https://apify.com/gauravlabs/google-flights-search-api)
* Duffel:
  * [Pricing](https://duffel.com/pricing)
* FlightAPI.io:
  * [Reseller listing](https://getalby.com/ai-tools/flights)
