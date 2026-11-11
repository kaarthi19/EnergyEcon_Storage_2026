# External Data Plan

Shortlist of external datasets that could improve the price forecasts, from a web search and access check run on 11 Nov 2026. Nothing here is used by the algorithm yet. Anything adopted must be added to `EXTERNAL_DATA_AND_SOFTWARE.md`.

## Market facts found along the way that affect the strategy

These came out of the search and matter more than any single dataset. Each needs confirming on EMA's data.

- **The battery is not small in the regulation market.** The regulation requirement averaged about 99 MW in 2026 (113 MW in 2024, 108 MW in 2025). A 100 MW regulation offer is the size of the whole requirement, so a price-taker assumption cannot hold for regulation. Primary reserve was about 313-340 MW and contingency about 565-584 MW in early Nov 2026.
- **Two 100 MW batteries already compete.** Sembcorp's two 100 MW units have been registered as energy storage facilities since 19 Sep 2025.
- **Ancillary prices are capped under the Temporary Price Cap (TPC).** The caps are multiples of the CCGT long-run marginal cost, reset every half month. The regulation cap was about 48-55 $/MWh.
- **Typical price levels (2024 averages, EMC market report):** energy 163 $/MWh, regulation 25.7, contingency reserve 19.7, primary reserve 2.9.
- **State of charge is in the market clearing engine.** RC386 (state of charge in the market clearing engine) and RC390 (market clearing and offers for energy storage) took effect on 12 Aug 2025 together with RC383. We have read only RC383. RC386 and RC390 may add offer fields or clearing constraints that this package does not yet model.
- **No public archive of pre-dispatch runs exists.** EMA's pre-dispatch data is the only source of price and demand forecasts as issued.
- **Spikes cluster around midday** (10:30-14:00), driven by forced outages and, since 2024, solar shortfalls. Contingency reserve prices move with energy scarcity.

## Shortlist

EMA releases its data on 13 Nov and the deadline is 30 Nov, 12:00 SGT.

**Reading the tags.** "AC" means an access check fetched the item successfully. "Scout only" means the claim is unverified. Wherever this note says "cut-off" or "decision time t", the rule is the same: a feature value may be used only if `available_time <= t`. Here t is the forecast issue time for the target period, and it can never be later than EMA's data cut-off.

**Basic split.** Open-licence sources can be bundled with attribution. EMC and OEM sources cannot be bundled, and their terms also forbid derivative works and automated collection. For those, ship a fetch script (a handful of GETs), ask EMA for permission, and make every model run with or without them.

## 1. Ranked shortlist

| # | Dataset | Value: energy / ancillary | Access (AC) | Vintage | History | Licence / bundle | Effort | Verdict |
|---|---|---|---|---|---|---|---|---|
| 1 | SG public holidays (MOM via data.gov.sg) | Med / Med | Open, AC | Calendar, known ahead | 2020-01-01 to 2027-12-25 | SG Open Data Licence v1.0. **Bundle** with attribution | 0.5 d | Integrate |
| 2 | TPC parameters (EMC value=25) | High / **High** (hard caps; regulation cap about 48-55) | Open, AC | Calendar. Window published ahead (16-Nov window was visible on 11-Nov) | 27-Jun-2023 to now | EMC T&C: no redistribution, no derivative works. **Script plus permission** | 1 d | Integrate |
| 3 | Structural-event calendar, hand-coded (rule-change effective dates, unit entries and exits, BESS entry, carbon tax) | Med / **High** (regime breaks) | EMC rule registers AC. Registry diffs AC | Effective dates known ahead | 2017 to 2026 | Facts with citations. Derivative-work status under EMC T&C is grey: **ask** | 1 d | Integrate |
| 4 | EMC Registered Facility Capacity snapshots (value=8): numeric MW features | Low / **High** (about 212 MW of BESS competitors from 19-Sep-2025; largest-unit size) | Open, AC | Dated snapshot. Whether it is true point-in-time is **not verified** | At least 2018 to now | EMC T&C. **Script plus permission** | 1 d | Integrate (fall back to #3 dates if not permitted) |
| 5 | Open-Meteo Previous Runs (ECMWF IFS 0.25, GFS) | Med-High / Low-Med (net-load and solar dips drive regulation) | Open, AC | Archived forecasts at fixed lead times (≥N×24 h). Exact run and latency **not verified** | Temperature from about Feb 2024. ECMWF shortwave from about mid-Mar 2024. GFS shortwave from Feb 2024 | Data CC BY 4.0. Free API is non-commercial. **Pre-download and bundle** with attribution | 1.5 d | Integrate |
| 6 | NEA 24-hr forecast, 4-day outlook and 2-hr nowcast (data.gov.sg v2) | Med / Low-Med | Open, AC | **As issued**, with issue timestamps | About 16-Mar-2016 to now (gaps, e.g. 2020-01-01) | SG Open Data Licence. **Bundle** | 1.5 d | Integrate (licence-cleanest weather source; covers any window) |
| 7 | OEM quarterly vesting data: BVP and per-period TCQ profile (weekday vs weekend/PH) | Med / Low | Open, AC | Known before each quarter. Some files revised later; originals not retrievable | 2008 to Q4 2026 (Q4 2024 not on the listing) | OEM terms: personal, non-commercial, no redistribution. **Script plus permission** | 1 d | Integrate if time |
| 8 | EMC Public Monthly Trading Report: monthly reserve and regulation **requirements** (MW) | Low / Med | Open PDF, AC | Ex-post monthly; publication date not verified | Jun-2023 to Sep-2026 | EMC Disclaimer: no reproduction | 1 d (PDF parse) | Optional. Skip if EMA gives requirements |
| 9 | Brent spot, FRED/ALFRED (DCOILBRENTEU) | Low-Med / Low | Open, AC | Actuals. **ALFRED vintages** available keyless | 1987 to now | Public domain. **Bundle** | 0.5 d | Optional (the TPC CCGT LRMC is a better fuel proxy) |
| 10 | NEA station observations (rain, temperature), lagged | Low / Low | Open, AC | Actuals only | Rain from 2017. Temperature sparse before 2017 | SG Open Data Licence. Bundle | 1.5 d (heavy pagination) | Optional |
| 11 | EMC final USEP, demand, solar, MRP, MFP (value=1/3/4) | Validation only | Open, AC | Finals only. DEMAND/SOLAR are the dispatch-time period forecast | 02-Jan-2016 to D+6 business days | EMC T&C. Do not bundle | 0.5 d | Cross-check EMA data. Extend history only if EMA allows |

Priority order for the two weeks: 1, 2, 3 and 4 in days 1-3; 5 and 6 in days 3-6; then ablation. 7 to 10 only if the ablation shows a gain. For ancillary prices, the biggest wins will probably still come from EMA's own pre-dispatch fields plus features built from EMA data: a TPC-active flag, MAP versus MAPT, a 48-period rolling mean, t-96 lags, and regime dummies.

## 2. Integration details for the shortlisted datasets

**Common join key:** `(date_sgt, period)`, where `period` runs 1-48 and `start_sgt = date 00:00 SGT + (period-1)×30 min`. Keep every timestamp in UTC internally; SGT is UTC+8 with no DST. Every external row carries an `available_utc` column. The feature builder does an as-of join: for decision time t and target period p, take the latest row with `available_utc <= t` whose validity covers p.

### #1 Public holidays
- **Download:** `https://data.gov.sg/api/action/datastore_search?resource_id=d_8ef23381f9417e4d4254ee8b4dcdb176&limit=500` returns JSON with fields date, day, holiday; there are 104 rows.
- **Join:** on `date_sgt`.
- **Features:**
  - `is_ph`, and `is_ph_observed` (separate rows exist, e.g. 2026-08-10 "National Day (Observed)").
  - A bridge-day flag (a weekday between a PH and a weekend). This is derived by us and is not in the source.
  - `day_type = weekday / sat / sun_ph`, replacing the current weekend flag.
  - Normalise the curly apostrophe in names.
- **Leakage:** none, because gazetted dates are known ahead. Coverage starts only on 2020-01-01.

### #2 TPC parameters
- **Download (one GET):** `https://www.nems.emcsg.com/api/sitecore/DataSync/DataDownload?value=25&fromDate=2023-06-27&toDate=<cutoff+30d>&tpcValue=1` returns CSV with START DATE, START PERIOD, END DATE, END PERIOD, NAME, VALUE.
  - Range requests are needed: a single-date request returns only the covering window.
  - END DATE 01-Jan-3000 means an open-ended window.
- **Cleaning:**
  - Drop the 27-30 Jun 2023 placeholder window (values at market-cap level).
  - Handle the overlapping row 16-Mar-2024 to 01-Apr-2024.
  - The *PERIODS rows (TRIGGER ON/OFF PERIODS, MINIMUM TRIGGER PERIODS) cover long windows.
- **Join:** expand each window to `(date, period)`.
- **Features:**
  - Caps for energy, regulation, primary and contingency, and `ccgt_lrmc` as a fuel proxy.
  - The cap/LRMC multiplier regime. At least three regimes alternate between fortnights: (3.0, 0.2, 2.167, 2.833), (2.5, 0.1667, 1.806, 2.361), and from 16-Nov-2026 (2.0, 0.133, 1.444, 1.889).
  - Trigger thresholds and periods. Combine with EMA's MAP/MAPT to get `tpc_headroom = threshold − MAP`.
  - Clip each product's forecast at its cap.
- **Leakage:** for a target in window W, use W's values only if `t >= W.start − 1 day`; otherwise use the current window. Lead time was observed for one window only (at least 5 days); the general posting date is not verified.

### #3 Event calendar (hand-coded CSV with a source URL per row)
- **Seed file:** the existing draft at `hackathon/external/sg_market_events_draft.csv` (28 rows).
- **Registers (AC, dates confirmed):**
  - RCP-considered: `https://www.home.emcsg.com/api/Search/PublicationResultsLoadMore?q=&sort=date-desc&filter1=-1&filter2=-1&filter3=-1&year=0&pageSize=10&page=N&itemId=10a3ffcf-6f14-4155-a35d-b015597f466b&relLimit=0`
  - EMA-directed: the same URL with `itemId=c47b4623-7bd7-43a4-aaa0-21aac80af002`
- **Rows to include:**

  | Category | Events (effective dates) |
  |---|---|
  | Rule changes and market reforms | TPC and new vesting 01-Jul-2023; RC385 01-Feb-2024; RC371 solar in MCE 21-Feb-2024; RC383/386/387/390 ESS package 12-Aug-2025; RC394 27-Nov-2025; RC396 06-Feb-2026; RC395 RVS 11-Mar-2026; RC391 reference prices 23-Jun-2026 |
  | Requirements | Regulation requirement cut to 113 MW 01-Feb-2024 (EMC 2024 report) |
  | Policy | Carbon tax S$25 from 2024-01-01 and S$45 from 2026-01-01 (NCCS page, scout only) |
  | Unit entries and exits | Seraya ST retirement in Q1 2024; Meranti OCGTs Feb to Jul 2025; Keppel Sakra 600 MW reserve-capable 29-May-2026; Sembcorp CCP4 600 MW reserve-capable 23-Oct-2026 |
  | Battery storage entry | Sembcorp ESS 2×100 MW 19-Sep-2025 |
  | Gas supply shock | Indonesian gas cut from about 24-Oct-2026 for 18 days (press, approximate) |

- **Features:** step dummies, or "days since" for each regime. Use them to split or weight the training window, and to interact with the ancillary-product intercepts.
- **Leakage:** effective dates were announced ahead, so they are fine. Do not use unannounced future events: anything effective after the cut-off must not appear in features.

### #4 Registered facility capacity
- **Download:** `https://www.nems.emcsg.com/api/sitecore/DataSync/DataDownload?value=8&fromDate=D&toDate=D&tpcValue=1`, once per month-start in the window (about 24 GETs).
- **Join:** as-of `date_sgt`.
- **Features:**
  - Total MAX REGULATION CAPACITY, MAX PRIMARY and MAX CONTINGENCY RESERVE CAPACITY (MW).
  - ESS MW with regulation capability.
  - Largest unit MAX GENERATION CAPACITY, as a contingency-requirement proxy (inference).
  - Count of 600 MW CCGTs.
- **Leakage:** use the snapshot dated ≤ t − 1 day. The snapshot may be the current register filtered by date rather than a true historical vintage, so the safer choice is step changes taken from #3 dates.

### #5 Open-Meteo Previous Runs (pre-download once, bundle)
- **Download URL:** `https://previous-runs-api.open-meteo.com/v1/forecast?latitude=1.35&longitude=103.82&hourly=temperature_2m_previous_day1,temperature_2m_previous_day2,temperature_2m_previous_day3,relative_humidity_2m_previous_day1,relative_humidity_2m_previous_day2,relative_humidity_2m_previous_day3,shortwave_radiation_previous_day1,shortwave_radiation_previous_day2,shortwave_radiation_previous_day3,cloud_cover_previous_day1,cloud_cover_previous_day2,cloud_cover_previous_day3&models=ecmwf_ifs025,gfs_seamless&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&timezone=GMT`
  - `_previous_day1` was verified, and `_previous_day2` for temperature only. `_previous_day3` and the other variables come from the scout's N=1..7 statement.
  - Pull in monthly chunks and retry on connection resets.
  - ECMWF snaps to 1.5N 103.75E, which is in Johor. A more southern latitude may fix this (not tested). The GFS point is 1.347N 103.828E.
- **Join:** UTC hour → SGT → half-hour. Interpolate temperature, RH and cloud linearly. For shortwave, both half-hours take the hourly value. Open-Meteo's shortwave averaging convention was not verified.
- **Features:**
  - Forecast temperature and the temperature anomaly versus the hour-of-day mean (cooling load).
  - Shortwave radiation × installed PV as a solar proxy, and cloud cover, both mainly for periods 15-38.
  - Day-over-day change.
- **Leakage:** with conservative latency, a `_previous_dayN` value for valid time v was issued at or before v − N×24 h and published about 6 h later. It is usable at t if `v − N×24h + 6h <= t`. That means:
  - day1 for horizons up to 18 h (36 periods);
  - day2 up to 42 h (84 periods);
  - day3 up to 66 h.

  This assumes "N days before" means init ≥ N×24 h before v, which is **not verified**. If it turns out to be "approximately", add one run (6 h) of slack.
- **Disclose:** "Weather data by Open-Meteo.com" (CC BY 4.0), with ECMWF and NOAA upstream.

### #6 NEA forecasts (data.gov.sg v2, bundle)
- **Download:** one call per day per product.
  - `https://api-open.data.gov.sg/v2/real-time/api/twenty-four-hr-forecast?date=YYYY-MM-DD`
  - `https://api-open.data.gov.sg/v2/real-time/api/four-day-outlook?date=YYYY-MM-DD`
  - `https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast?date=YYYY-MM-DD`, paginated with `&paginationToken=`, about 2-3 pages a day.
  - The keyless limit is 6 calls per 10 s (verified), so throttle to 1 call per 2 s. Two years is about 1-2 h.
- **Join:** each issue becomes a row with `available_utc`. For each target period, find the 6-hour `periods[].timePeriod` that covers it.
- **Features:**
  - From the 24-hr forecast: temperature high and low, RH high and low, and wind range.
  - Per 6-hour block: share of the 5 regions with showers or thunder codes (e.g. "TL").
  - Beyond the 24-hr validity: the 4-day outlook's day values.
  - For h ≤ 4 periods: share of the 47 nowcast areas with rain.
- **Leakage:** `available_utc = issue time + 15 min`. Two checks disagree on which field holds the issue time:
  - One says `timestamp` is the issue time for all records, and `updatedTimestamp` is a back-fill date in 2024 records and about 7-11 min after issue in current records.
  - The other found a real issue time in `updatedTimestamp` only from 20-Jul-2024 onward, and back-fill stamps before that.

  Use `timestamp` where it is present and validate it against `validPeriod.start`. If they disagree before Jul 2024, use `validPeriod.start` as the issue time.
- **Attribution:** use the Singapore Open Data Licence v1.0 notice.

### #7 Vesting data
- **Download:** take each quarter's file URL from the listing page `https://www.openelectricitymarket.sg/business/resources/vesting-contracts/vesting-data`; directories differ by year. Example: `https://www.openelectricitymarket.sg/dam/oem/rm/vesting-data/VCData4thQuarter2026Final--Website-.xlsx`.
- **Join:** quarter × month × day type (weekday or weekend/PH, using #1) × period.
- **Features:** `tcq_period_kwh`, which is a same-period profile, plus BVP and PRP.
- **Leakage:**
  - A quarter is usable from the Last-Modified of its first version. Observed: Q4 2026 on 25-Sep, Q3 2026 on 25-Jun.
  - Revised files (e.g. Q1 2024 modified 09-Oct-2024; Q2 2026 on 03-Jun-2026) may hold values that did not exist at the time.
  - Use them only if `t >= quarter start − 3 days`, and disclose the revision risk.

### #8 Monthly requirements (optional)
- **Download:** `https://www.nems.emcsg.com/-/media/market-trading-reports/monthly/YYYY/Public-Monthly-Trading-Report---Mon-YYYY.pdf`, then `pdftotext -layout`, Figure 12.
- **Features:** last-published monthly regulation, primary and contingency requirement.
- **Leakage:** month m is usable from the end of month m+1. Publication timing is not verified, so a 2-month lag is the safe choice.

### #9 Brent (optional)
- **Download:**
  - `https://fred.stlouisfed.org/graph/fredgraph.csv?id=DCOILBRENTEU`
  - For point-in-time values: `https://alfred.stlouisfed.org/graph/alfredgraph.csv?id=DCOILBRENTEU&vintage_date=YYYY-MM-DD`
- **Features:** 30-day and 90-day mean, and the 3-6 month lagged mean to mimic oil indexation.
- **Leakage:** use the ALFRED vintage at date(t) − 1, or lag at least 9 calendar days.

### #10 NEA station observations (optional)
- **Download:** `https://api-open.data.gov.sg/v2/real-time/api/rainfall?date=` and `/air-temperature?date=`. That is about 58 pages per day per variable, so pull only rainfall at a few stations if at all.
- **Features:** island-wide rain over the last 30 and 60 min, and temperature anomaly.
- **Leakage:** reading time + 10 min ≤ t. Delivery latency is not verified.

## 3. Skip

| Source | Reason |
|---|---|
| EMC 72-period table (`/api/DataSync/TableChart?value=12/13`) | Live only; the date parameter is ignored; no archive. Polling to build one breaches the EMC T&C. Use EMA's pre-dispatch data instead |
| EMC Data Subscription packages A-D (offer stack, outage plans, advisory notices) | Paid and login-only. Conflicts with the "no undisclosed proprietary services" rule |
| EMC Public Daily Trading Report PDFs | Archive is irregular: the AC found Nov-2024 onward with many 302s, and a scout saw a rolling window only. Requirements are daily figures only. Duplicates the value=1 CSV |
| EMC metered generation (value=16), nodal (value=2), WEP (value=5), VCRP (value=6) | D+10 to D+15 lag, so lagged context only. Nodal data should come from EMA if settlement is nodal |
| ema.gov.sg pages (half-hourly demand, plant availability, SES, TPC page, solar PV reports) | Incapsula bot check (AC confirmed for demand and TPC pages). Ask EMA instead |
| Open-Meteo Single Runs | ECMWF runs before May 2026 are Cycle 49R1 hindcasts, not operational issues; other models only from 02-Apr-2026. Previous Runs covers the same need |
| Open-Meteo Historical/ERA5 | Reanalysis; 5-6 day latency; not a forecast. Could at most be used to train a weather→load map |
| ECMWF open data / GFS on AWS | Licence-clean, but needs GRIB processing. Keep as the fallback if Open-Meteo's non-commercial terms are ruled out |
| NASA POWER, Himawari L2, Solcast, SERIS | Months of solar latency / heavy processing / paid or academic sign-up / no public download |
| MSS weather.gov.sg daily CSVs | Terms forbid commercial use, redistribution and scraping |
| data.gov.sg EMA datasets (peak demand, PV capacity, fuel mix), SingStat | Frozen at 2021, or monthly/annual |
| SP tariff, MAS FX, FRED USD/SGD, IMF LNG via FRED, Henry Hub, GPR index | Redundant with the TPC LRMC, weak for half-hourly or ancillary prices, or licence unclear (IMF, MAS) |
| World Bank Pink Sheet | Monthly and lagged; redundant with the LRMC. Use only if a fuel regime term is wanted (CC BY implied, not fetched) |
| MOE school terms | Small effect; HTML only. Hand-code later if time allows |
| Third-party mirrors (GitHub SGelectric, GPL-3.0; Mendeley 2017-18) | Wrong period, or a licence that clashes with the IP assignment to EMA |

## 4. Open questions only EMA (or its release) can answer

1. What is the data cut-off, what is the training window, and is the evaluation window after the cut-off? May history from before EMA's window be used?
2. May EMC/OEM public data (TPC, registry, vesting, finals) be used, and may it be fetched by script at run time or bundled? Will EMA obtain EMC permission, or supply TPC and vesting parameters itself?
3. Will the algorithm have internet access when EMA runs it? If not, everything must be bundled, and EMC/OEM-derived features need permission.
4. What does the pre-dispatch information contain?
   - Ancillary price forecasts (regulation, primary, contingency)?
   - Demand and solar forecasts?
   - Half-hourly reserve and regulation requirements?
   - The risk-setting or largest unit?
   - Outages or available capacity, and the supply cushion?
5. When was each pre-dispatch run issued, and at what lead times? Is it one row per run, i.e. are vintages retained?
6. What is the decision timeline per period (the offer gate closure, which defines t)? Is the bid fixed for day D at the cut-off, or rolling?
7. Is the battery settled at a node or at USEP? Which reserve group (CONRES/PRIRES A-E) applies?
8. Are TPC caps and reference prices applied in the simulation? Does the simulator enforce RC383/RC386 state-of-charge and duration constraints?
9. Is the battery a price-taker in the simulation, or do our offers move prices (the 100 MW regulation offer against a requirement of about 99 MW)?
10. Is Julia acceptable, or must the code be R or Python?
11. Does EMA's use of the free Open-Meteo API count as commercial? Is pre-downloaded CC BY data acceptable? What attribution format does EMA want for disclosed sources?
12. Does "same period as EMA-provided data" allow longer lag windows that reach before EMA's window, such as 28-day means at the window start?

## 5. Claims resting on unverified scout statements or inference

- **Open-Meteo:**
  - The exact run behind each `_previous_dayN` value and its publication latency.
  - The availability of `_previous_day3`, and of previous-day variants other than temperature and day1.
  - The shortwave averaging convention.
  - Whether a different latitude avoids the Johor grid point.
  - Whether hackathon use is "commercial".
- **TPC:** the publication lead time for past windows (only one window was observed, at ≥5 days). Units (S$/MWh) are inferred because the file does not state them.
- **NEA forecasts:**
  - Which field holds the issue time before Jul 2024 (the two checks conflict).
  - That the "+15 min" publication lag applies to older records.
  - The indemnity clause in the licence.
- **Registry:**
  - Whether past-date snapshots are true point-in-time vintages.
  - The aggregate GW totals (about 11.3 to 13.4 GW).
  - That the Sembcorp ESS operated outside the market before Sep 2025.
- **Requirements:** that contingency-requirement rises are caused by the 600 MW CCGT arrivals (inference). Whether daily-report requirements are ex-ante or ex-post.
- **EMC value=1:** that DEMAND/SOLAR are the real-time dispatch vintage. The AC showed they match the Daily Trading Report "Forecasted" columns; the exact run is unverified. One literature check called DEMAND "realised", which contradicts EMC's guide.
- **Other EMC files:**
  - The MFP REFERENCE PRICE is populated from about Jul-2026. Its link to RC391 (23-Jun-2026) is inferred, and the exact start date was not pinned down.
  - The MRP start date of 02-Jan-2016 was tested for value=1 only.
  - The by-year ZIP was HEAD-checked only.
- **Vesting:** whether Q4 2024 exists under another name. That Last-Modified is a valid proxy for first publication.
- **Licences:**
  - The World Bank's CC BY is implied by a link only.
  - NOAA/GFS public domain, OEM and SP terms beyond those quoted, and NCCS and MOE terms were not read.
  - Whether hand-coded facts from EMC pages count as "derivative works" (a legal grey area).
- **Calendar facts from scouts only:**
  - Carbon tax rates and dates.
  - The ~S$8/MWh carbon cost estimate (our own calculation).
  - Gas-cut dates (press reports; start date inferred).
  - The regulation requirement change on 1-Feb-2024, which comes from the EMC 2024 report (spot-checked text).
- **Literature claims:** the arXiv 2602.12782 and 2507.07477 findings, and the NUS SDP finding that temperature adds nothing once demand is included. These were read but not reproduced.

## Files

- `hackathon/external/sg_public_holidays.csv`: item 1, downloaded 11 Nov 2026 from data.gov.sg (Ministry of Manpower, Singapore Open Data Licence v1.0).
- `hackathon/external/sg_market_events_draft.csv`: draft of item 3, hand-coded dates with a source URL per row. Several rows are inferred or from press reports; check each before use.

The samples fetched during the access checks were kept in a temporary working directory and are not in the repository.
