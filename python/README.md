# bessbid: bid generator for EMA's BESS bidding hackathon

`bessbid` turns EMA's competition data into a November 2026 bid schedule in EMA's Excel
template. The BESS is 100 MW / 100 MWh, SOC 5-95 %, 85 % round trip, starting at 50 % SOC.
The products are Energy, Primary Reserve, Contingency Reserve and Regulation, bid in
30-minute periods.

This folder is the Python implementation. It reads the data from a folder you pass on the
command line and writes caches and outputs to a work folder you choose. **No competition
data, and nothing derived from it, is stored in this repository** (hackathon clause 21);
`python/.gitignore` blocks csv/parquet/xlsx files as a safeguard.

## Install

Python 3.10 or newer.

```bash
cd python
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` pins two tested sets of versions: one for Python >= 3.12 and one for 3.10
and 3.11. With these pins the output workbook is byte-identical across both sets.

## Run

```bash
# November bids -> EMA template -> validated workbook
python -m bessbid generate \
    --data-dir "/path/to/ema bidding comp" \
    --out /path/to/work/submission.xlsx \
    --work-dir /path/to/work \
    [--strategy full]                # default; 'regulation' is the fallback
    [--plan-out /path/to/work/november_plan.csv] \
    [--template /path/to/BESS_Competition_Template.xlsx] [--month 2026-11] [--config overrides.toml]

# walk-forward pseudo-November evaluation (Aug, Sep, Oct) against the baselines
python -m bessbid evaluate --data-dir "/path/to/ema bidding comp" --work-dir /path/to/work

# check any workbook against every template rule
python -m bessbid validate /path/to/work/submission.xlsx --month 2026-11

# find and load the data, print a summary (stack shift fix, gaps, publication lags)
python -m bessbid inspect --data-dir "/path/to/ema bidding comp" --work-dir /path/to/work
```

`generate` performs these steps:

1. It finds the files by name pattern anywhere under `--data-dir`. Nested folders and names
   such as `(Part 1) ...` are fine. The template is also found there unless `--template` is
   given.
2. It runs the strategy. Every market-data read goes through the time-gated `InfoSet`.
3. It writes the bids into a copy of the template.
4. It re-reads the file with the independent validator and checks it against every template
   rule.
5. It reads the file back and confirms it matches the bids exactly.
6. For the full strategy it writes the per-period plan (`--plan-out`, default
   `<work-dir>/<out-stem>.plan.csv`): energy MW, Regulation MW, the SOC path under each
   efficiency convention, and the day-ahead, month-ahead and lead-2 h forecasts behind it.
7. It writes `<work-dir>/<out-stem>.manifest.json`. The manifest holds the input file names
   and sizes, the config, the validation result, a summary of the information audit, and two
   fingerprints: the SHA-256 of the workbook and an environment-independent SHA-256 of the
   bid table.

The exit code is 0 on success, 2 if validation or the read-back fails, and 1 on usage
errors. Output and work dirs inside the data dir are refused, and the data dir is never
written to.

The full strategy takes about 3 minutes for November (about 340,000 LP solves).
Runs are deterministic. Nothing is random, and the workbook's created/modified times and zip
entry dates are fixed (`Config.fixed_timestamp`), so the same inputs give the same bytes.

### Fallback submission: `--strategy regulation`

Each period offers 100 MW of Regulation in one band at 0.01 (not 0.00, which the template's
number format shows as "-"). It makes no Energy, Primary or Contingency offers: those rows
are written with every band blank, as the template's own example does for period 2.

Why this is the fallback:

- Regulation cleared above Contingency and Primary in every training period.
- Activation is 0 MWh, so SOC stays at 50 % and cannot breach a limit under any efficiency
  convention.
- It reads no market data, so it cannot break an information rule.
- An offer at 0.01 clears whether EMA re-clears the market with our offers or prices them
  at fixed clearing prices.

### Main submission: `--strategy full`

Regulation is the base product. On top of it, a planned energy overlay that always clears.

**Layer 0: Regulation.** Every period offers `100 - |energy MW|` of Regulation in one band
at the AS floor price, so the strict capacity rule holds with equality. There are no Primary
or Contingency offers (see the investigations below).

**Layer 1: energy overlay.** Energy is offered only as fixed quantities that always clear:
discharge at the floor price and charge at the cap price (3000). The cleared MW therefore
equal the offered MW whether EMA re-clears the market with our offers (price-maker) or uses
fixed prices (price-taker), and our code knows the SOC in every period without feedback
from EMA. The quantities come from a dynamic programme over SOC (`planner.py`):

- Values include our own price impact. For each period, the forecast stack is cleared with
  our offer inside the LP at 13 signed energy levels (-100 to +100 MW, Regulation filling
  the rest). The prices are averaged over the 9 net-demand error nodes of the forecaster,
  which turns a single merit-order step into an expected price curve. The value of an
  action x is 0.5 h x (x E(x) + (100 - |x|) R(x)).
- Size limits: charge at most 50 MW; discharge at most 50 MW, or 100 MW inside calendar
  event windows.
- Bans: no charging inside 11-day or 19-day event windows, or when the day-ahead forecast
  capacity margin is below 300 MW.
- SOC: the programme's state is the *binding* SOC `Y = s0 + eta_c_max x N`, where `N` is
  the grid-side net energy stored. For any convention, SOC = s0 + eta_c x N, so keeping Y
  in [5 % + margin, 95 % - margin] keeps the SOC inside the limits under **every**
  efficiency convention (all loss on charge, sqrt split, or all loss on discharge). The
  margin is `soc_margin_mwh` = 2 MWh. Actions are exact SOC steps on a 0.5 MWh grid (charge
  in 2 MW steps, discharge in 1.7 MW steps), so the bid quantities reproduce the planned
  path exactly.
- Terminal SOC: the month ends with SOC >= `terminal_soc_frac` (default 0.5) under every
  convention.

**Layer 2: events.** The same programme plans the 19-day stress event. A month-ahead value
function (one decision at the gate of the month's first period) sees the calendar's event
days. It is the continuation value at the end of each daily plan, so the battery arrives
full before the event, discharges up to 100 MW in the periods with the highest forecast
value, and recharges after the event window.

**Guards at each period's gate.** At T - 65 min the lead-2 h forecast for T cancels a
planned charge if it shows a margin under 300 MW, a reserve shortfall or an energy price
above 400. The daily policy is defined for every SOC, so the rest of the day continues
optimally from the new SOC. In Aug-Oct and November the guard never fired.

**Information timing.**
- The forecaster is fitted at 00:00 the day before the month (Oct 31 for November), with
  net-demand history capped there.
- Day D's plan is decided at 00:00 of D-1, using day-ahead forecasts.
- The guard for period T uses T's own lead-2 h vintage at T - 65 min.
- Earlier periods' bids always clear and are our own decisions, so the SOC at T is known
  at every earlier decision time.

Every forecast and value call records an audit entry; `generate` ends with
`assert_no_leak()`. A test perturbs every row not legal at T's gate and checks that bids for
periods up to T come out identical.

**Layer 3 (not used).** `soc.SocIntervalTracker` keeps a worst-case SOC interval for
price-responsive bands that may or may not clear. Such a band only fits if every later
committed action stays feasible in the worst case and the terminal SOC still holds. The
planned path ends at the terminal minimum and empties the battery during events, so the
room is about zero. Re-optimising each period on lead-2 h values (`refine='lead2'`), which
is the information such a band would use, lost money in every month (table below).

#### Walk-forward results (pseudo-Novembers)

Each training month is planned exactly as November would be: fitted at 00:00 the day
before the month, and every bid limited to data legal at its own gate. The result is then
settled by `backtest.simulate`. Revenue does not depend on the efficiency convention,
because the energy offers always clear.

| Month | Regulation only, maker | Full, maker | Uplift, maker | Uplift, taker | Perfect-foresight uplift (cap 50 MW, maker) |
|---|---|---|---|---|---|
| Aug | 1,205,144 | 1,318,037 | +112,894 | +126,994 | +155,998 |
| Sep | 1,209,195 | 1,329,444 | +120,249 | +127,992 | +158,798 |
| Oct | 2,126,795 | 2,246,396 | +119,601 | +142,603 | +235,193 |
| **per 30 days** | 1,480,804 | 1,595,829 | **+115,025** | **+129,649** | +179,344 |

- **Positive in every month** under both modes.
- The full strategy captures 64 % of the perfect-foresight uplift (maker).
- **Revenue by product, Aug-Oct, maker:** energy +616,258; Regulation 4,277,619
  (-263,514 against Regulation only, because the energy MW displace Regulation MW).
- **SOC range:** [7.0, 93.0] MWh under the all-loss-on-discharge convention,
  [10.4, 89.6] under sqrt, and [13.4, 86.6] under all-loss-on-charge.
- **0 breaches** under all three conventions in both modes. Every month ends at >= 50 %.
- **Runtime:** about 170 s per month to plan.
- `python -m bessbid evaluate` reproduces this table; `evaluate.perfect_foresight_plan`
  builds the hindsight bound.

**Investigations** (maker and taker, Aug-Oct; differences in SGD over the three months, or
per month where stated):

- *Regulation offer price.* 0.00 or 0.01 is best. Any price up to about 11 gives the same
  revenue (the lowest training regulation price was 11.3). Offering 100 MW at 12 loses
  22-34k per month and at 15 loses about 330k per month, because cheap generator Regulation
  offers then displace ours. Splitting 5 MW into a second band at 15 or 25 loses 17k or
  57k per month.
- *Primary/Contingency.* Moving part of the 100 MW from Regulation to C or P, even only in
  the raised-contingency windows with hindsight timing, loses in every month and mode:
  R80+C20 -5k to -15k per month, C100 -26k to -74k, R70+P30 -21k to -112k. Regulation is
  priced above C and P in every training period, and our price impact does not reverse that.
- *Energy size vs price impact.*
  - Capping both directions at 30 / 40 / 50 / 75 / 100 MW gives maker uplifts within ±4k of
    each other. 50 MW is the balanced choice: 40 MW is +4k maker and -4k taker.
  - Planning without our own price impact loses 10.5k (maker).
  - Planning on a single point forecast instead of the 9 net-demand error nodes loses 20k
    (maker).
- *Other options tested and rejected:*
  - a throughput hurdle per MWh: +3k to +5k at 5-10 SGD/MWh (mixed by month, noise level),
    -39k at 20 SGD/MWh;
  - allowing charging inside event windows: -2.5k (+14k in Aug, -16k in Sep-Oct);
  - lead-2 h re-optimisation in all periods: -21k; only inside event windows: -3.3k;
  - deciding the daily plan at the gate of D's first period instead of 00:00 of D-1: -0.3k.
- *Terminal SOC.* Ending the month at 5 % instead of 50 % would add about 10k per month.
  It is a config option (`terminal_soc_frac`); 50 % is kept until EMA says otherwise.

## Rules the code enforces

**Capacity (strict reading).** In every period, sum |energy band MW| + sum of all AS band MW
<= 100. The alternative reading, `max_side` (max(charge, discharge) + AS), can be selected in
the config, but `strict` is the default.

**Offers.** These rules come from the template and RC383 s3.1.2:
- Energy has up to 10 bands and each AS product up to 5 (bands 6-10 stay blank).
- Quantities are per band, not cumulative.
- Charging is negative MW and discharging positive MW.
- Prices are in whole cents and strictly ascending, and every charge price is below every
  discharge price.
- MW is rounded *toward zero* to 0.1 MW (`mw_resolution`), so rounding never adds capacity.
- Always-clearing offers are priced as follows: discharge at `discharge_floor_price` = 0.01,
  charge at `charge_cap_price` = 3000.00, and AS at `as_floor_price` = 0.01.

**Information rules.** These are enforced by `InfoSet`, which raises `LeakError` and audits
every read:

| Data | Usable for a period starting at T when | Accessors |
|---|---|---|
| Pre-dispatch | `issue_time <= T - 65 min`. Strict default: only T's own rows, lead 2 h, else lead 3 h | `predispatch_for(T)`, `predispatch_for_many(Ts)`, `predispatch_known_at(t)` |
| Offer stacks | `assumed_publication_time_sgt <= T - 65 min` (= ts + 28 d 8 h, so stacks lag >= 29 days) | `stacks_for(T)`, `stacks_known_at(t)` |
| Training clearing | the period has ended: `ts + 30 min <= T - 65 min` | `clearing_for(T)`, `clearing_known_at(t)` |

Compute every per-period feature with that period's own cutoff (`infoset.cutoff(T)`), never
from a month-wide frame. `predispatch_known_at` hides training actuals until their period
has ended. `infoset.assert_no_leak()` and `infoset.audit_frame()` provide the proof for the
manifest.

**Data fixes (`io.py`).**
- In the first 28 days of the Part-1 stack file, `period_sgt` is 8 h later than the trading
  period named in the `period` column. The true time is rebuilt from the trading date and
  `period`.
- Duplicates are dropped only after that fix, and the key (ts, facility, product, band) must
  then be unique.
- Timestamps in mixed formats (`T` or space, with an offset) are parsed to naive SGT.
- `io.copied_dates()` flags stack dates that are an exact copy of the data 28 days later
  (June == July). Exclude those dates from fits (`Config.stack_fit_start`).

## Package layout

| Module | Contents |
|---|---|
| `config.py` | `Config` (frozen dataclass) and `load_config(path)`. TOML/JSON overrides may be flat or grouped in tables; unknown keys are errors |
| `periods.py` | `month_slots(month)`, `period_start(date, k)`, `period_of(ts)`. All times are naive SGT |
| `io.py` | `find_files`, `find_template`, `load_clearing`, `load_predispatch(kind)`, `load_stacks`, `load_all`, `stack_report`, `copied_dates`. Parquet cache in `<work>/cache`, keyed by file name, size and mtime |
| `infoset.py` | `InfoSet`, `AuditEntry`, `LeakError` |
| `bids.py` | `Band`, `BidRow`, `PeriodBid`; builders `regulation_only`, `energy_fixed`, `energy_curve`, `make_period_bid(clip_as=...)`; `to_table` / `from_table` |
| `validate.py` | `validate_table(df, cfg, month)`, `validate_xlsx(path, cfg, month)`, `Violation`, `format_report` |
| `writer.py` | `write_submission(bids, template, out, cfg, month)` and `read_back(path)` |
| `strategy.py` | the `Strategy` protocol, `register_strategy`, `get_strategy`, `check_complete` |
| `strategy_regulation.py` | `RegulationOnlyStrategy` |
| `engine.py` | EMA-replica clearing LP: `ClearingEngine`, `clear`, `clear_period`, `price_taker_clear`, `replicate`; `StackArrays`, `PeriodStack`, `BessOffer`, `ClearResult` |
| `backtest.py` | `simulate(bids, month, data, mode, convention, soc_policy, ...)`, `run_months`, `MarketData`, `soc_path` |
| `cli.py` | `python -m bessbid generate \| validate \| inspect \| backtest \| evaluate` |
| `calendar.py` | `learn_calendar(infoset, cut) -> EventCalendar` (outage cycles, contingency and surge windows learned from data); `event_flags`, `contingency_req`, `surge_mw`, `expected_outage` |
| `netdemand.py` | `NetDemandModel`: legal lead-2 h/3 h net demand (night solar zeroed), day-ahead profile + surge, error nodes |
| `stackmodel.py` | `StackModel`: offer-price factor model, legal 7-lag stack blend (`forecast_stack(T, decision_time, outage)`) |
| `forecast.py` | `Forecaster(data, cfg).fit(cut)`: `.lead2`, `.lead2_many`, `.day_ahead`, `.scenarios`; `walk_forward`, `evaluate`, `month_forecasts`; `python -m bessbid.forecast` |
| `_clearlp.py` | small reference clearing LP (cross-checked against `engine.py` in the tests) |
| `soc.py` | SOC under every efficiency convention: `soc_paths`, `binding_soc` (the convention-free state Y), `check_soc`, `y_bounds`, `y_terminal_min`, `SocIntervalTracker` (worst-case interval for offers that may not clear) |
| `planner.py` | `ValueModel` (forecast stack cleared with our own offer at a ladder of energy levels, averaged over net-demand error nodes), `make_action_grid` (exact SOC steps), `action_values`, `dp_backward` / `dp_forward` / `dp_plan` |
| `strategy_full.py` | `FullParams`, `plan_month(month, infoset, cfg, params) -> (bids, plan frame, info)`, `FullStrategy` (registered as `full`, the CLI default) |
| `evaluate.py` | `evaluate(ds, cfg, months)`: walk-forward pseudo-Novembers in maker/taker mode and every convention against Regulation-only and `perfect_foresight_plan` |

### Extension points

A strategy is any object with a `name`, a `needs_data` flag, and
`generate(month, infoset, cfg) -> list[PeriodBid]` returning one bid per period. Register it
under a name:

```python
from bessbid.strategy import register_strategy
register_strategy("full", "bessbid.strategy_full:FullStrategy")   # already pre-registered
```

`--strategy full` loads `bessbid/strategy_full.py:FullStrategy` lazily.

Strategies should take data only from an `InfoSet` and build bids only with the
`bids.py` builders, which enforce rounding and the capacity rule. The output must pass
`validate_table` before it is written; `write_submission` refuses invalid tables.

## Market engine and backtest

### `engine.py`: exact replica of EMA's clearing

One LP per 30-minute period, solved with HiGHS (`highspy`) directly:

- It minimises offer cost.
- Energy balance: generation + unserved + BESS discharge - BESS charge = `net_demand_mw`.
  The energy price is the dual of this row.
- Reserve rows: offered + shortfall >= requirement. Shortfalls are uncapped and priced at 750
  (P), 1000 (C) and 1200 (R). VoLL is 4500, an assumption, since energy was never unserved.
- Per unit: e + p + c + r <= `available_mw`. Reserve caps are the reserve `offered_mw`.
- Energy ramp-up: e(t) <= e(t-1) + 0.42 x `available_mw`(t). It is applied by clipping bands
  in merit order, which is the same LP as an explicit ramp row.

**BESS offers** enter as RC383 bands:
- discharge (MW > 0) is supply at its offer price;
- charge (MW < 0) is a demand bid, cleared when the price is below the offer;
- reserve bands are supply at their price.

The engine also applies two physical joint rows: discharge + P + C + R <= 100 and
charge + R <= 100 (`bess_pmax`). The bid builders separately enforce the stricter
offered-MW rule (|energy| + all AS <= 100). For our own bids, therefore, these rows never
bind.

**Price-taker awards.** `price_taker_clear(offer, prices, tie=)` awards the bands that are in
the money. With the default `tie='strict'` (RC383 wording), a band offered exactly at the
price does not clear; `'inclusive'` clears it. If the joint rows would be exceeded, MW go to
the largest margins first.

**Speed.** One model per (facilities, bands) shape is reused: each call changes bounds,
costs and row bounds and warm-starts the simplex. A solve takes about 0.4 ms. With
`warm_start=False` the results do not depend on solve history.

**Replication** (`replicate(stacks, clearing)`): all 5904 training periods, Jul 1 - Oct 31,
cold start Jul 1, ramp chained as EMA did. Runtime is 2.3 s.

| Price | MAE | Max error |
|---|---|---|
| E | 0.0003 | 0.0019 |
| P | 0.0003 | 0.0020 |
| C | 0.0003 | 0.0020 |
| R | 0.0004 | 0.0018 |

Unmet reserve MW agree to within 0.002. BESS insertion matches the Phase A engine to 1e-12.

### `backtest.py`: EMA-replica evaluator

```python
from bessbid import io
from bessbid.backtest import MarketData, simulate, run_months
md = MarketData.from_dataset(io.load_all(data_dir, work_dir), start="2026-07-01")
r = simulate(bid_table, "2026-10", md, mode="maker", convention="sqrt", soc_policy="clip")
r.frame     # one row per period: prices, awards, revenue by product, SOC (+ per convention)
r.summary   # totals, revenue per 30 days, SOC min/max/final, breaches, clips, rejections
```

**Modes.**
- `maker`: our offers inside the LP, with actual stacks, net demand and requirements. The
  ramp state is chained from a cold start on Jul 1. `ramp='baseline'` instead chains the
  no-BESS dispatch (the Phase A convention).
- `taker`: actual training prices, with `price_taker_clear` awards.

**SOC.** SOC starts at 50 MWh and moves by 0.5 h x (eta_c x charge - discharge / eta_d).
`convention` is `'sqrt'`, `'charge'`, `'discharge'` or an (eta_c, eta_d) pair. When a period
would leave 5-95 MWh, `soc_policy` decides what happens:
- `none`: report the breach;
- `clip`: trim the energy offer to what SOC allows, before clearing;
- `reject`: drop that period's energy offer and keep its reserves.

Every run also reports the SOC path of the realised dispatch under all three conventions.

**Settlement.**
- Energy: price x (discharge - charge) x 0.5 h, so charging costs money.
- Reserves: price x MW x 0.5 h.

**Golden numbers** (regulation-only, 100 MW at 0.00, sqrt convention):

| Window | Maker (re-cleared) | Taker (actual prices) |
|---|---|---|
| October | 2.0582 M per 30 days (2.1268 M for the 31 days) | 2.4062 M per 30 days |
| Jul-Oct | 1,415,911 per 30 days | 1,575,608 per 30 days |

These equal the Phase A values exactly. Pseudo-Novembers are Aug, Sep and Oct
(`PSEUDO_MONTHS`). A whole month runs in about 2.5 s as maker (of which about 1.8 s is the
ramp warm-up from Jul 1) and about 0.1 s as taker.

```bash
python -m bessbid backtest --data-dir D --work-dir W --strategy regulation \
    --month 2026-08 2026-09 2026-10 --mode both --convention all --soc-policy clip
python -m bessbid backtest --data-dir D --work-dir W --bids some_october_bids.xlsx --month 2026-10
```

The summary goes to stdout and to `<work>/backtest_<label>.json`.

## Price forecasts

Prices are forecast structurally, the way EMA clears them: a forecast offer stack, forecast
net demand and the event calendar go through the clearing LP (`engine.py`, cold-started so a
forecast never depends on earlier solves).

- **Calendar** (`calendar.py`). Learned at a cut from the known-outage state in
  pre-dispatch rows issued by then: outage onsets are explained greedily by periodic cycles
  (time of day, period in days, anchor); each cycle's duration is the candidate that best
  reproduces the observed states and its event MW is the median onset jump. The raised
  contingency requirement and the demand surge are windows relative to a cycle, learned from
  training clearing rows that ended by the cut. Nothing is hard-coded. On EMA's data the
  calendar learned from data up to Jul 28 already classifies every training and November
  known-outage state above 400 MW with 0 exceptions.
- **Net demand** (`netdemand.py`). Lead forecast: T's own lead-2 h vintage, else lead 3 h,
  `load - solar` with forecast solar set to zero in periods of the day where training solar
  was always zero. Day-ahead: mean of the same period over the last 4 days of the same day
  type, outside surge windows, plus the learned surge. Under the strict reading
  (`Config.strict_predispatch`) the history is training actual net demand; otherwise it is
  the lead-0.5 h vintages of past periods. The hour-of-day bias correction exists
  (`bias_correction=True`) but is off: it made lead-2 h error slightly worse.
- **Stack** (`stackmodel.py`). log price = base(unit, band, period of day) + factor; the
  factor is regressed on stack outage MW and weekend. The forecast for T blends the 7 most
  recent published stacks for T's period of day (publication per row, so lags of 29+ days),
  normalised to zero outage and weekday, rescaled to T's expected outage and day type, with
  outaged units restored. Stack periods that are exact copies of a published period 28 days
  later (EMA's June files) are left out of the fit.
- **Forecasts** (`forecast.py`). `lead2(T)`: decision `T - 65 min`; expected outage = the
  vintage's known outage moved forward with the calendar. `day_ahead(day)`: decision 00:00
  the day before (any decision time can be given). `scenarios(...)`: net-demand error nodes
  (and event MW x 0.87/1/1.13 day-ahead in event windows) through the LP, times whole-day
  empirical log-residual blocks of the same regime from a walk-forward residual library;
  gives means, quantiles, P(spike) and price paths. Every forecast checks its decision time
  against the fit cut and writes an audit entry.

Walk-forward accuracy, Aug-Oct, monthly refits at 00:00 the day before each month (MAE in
SGD/MWh; spike = price > 400):

| | Energy MAE | Primary | Contingency | Regulation | Energy spike precision / recall |
|---|---|---|---|---|---|
| lead 2 h | 18.8 | 1.27 | 1.80 | 2.27 | 0.74 / 0.97 |
| day-ahead, strict reading | 23.1 | 2.45 | 3.08 | 3.55 | 0.70 / 0.93 |
| day-ahead, broad reading | 22.1 | 1.95 | 2.53 | 3.00 | 0.72 / 0.96 |

Scenario ensembles (Sep-Oct, residual library from Aug 1): q10/q50/q90 coverage
0.06-0.10 / 0.42-0.48 / 0.82-0.87; P(energy spike) >= 0.5 has precision/recall 0.78/0.97
(lead 2 h) and 0.64/0.99 (day-ahead).

```bash
# a month of forecasts (lead 2 h, day-ahead, month-ahead, ensembles) into the work dir
python -m bessbid.forecast --data-dir D --work-dir W --month 2026-11
```

The output (`<work>/forecasts_<month>.parquet`, a daily CSV and a JSON summary) is derived
from EMA's data and stays in the work dir.

## Tests

```bash
cd python
python -m pytest                       # unit tests on tiny synthetic frames (~35 s)
BESSBID_DATA_DIR="/path/to/ema bidding comp" BESSBID_WORK_DIR=/path/to/work python -m pytest
```

Tests marked `data` run only when `BESSBID_DATA_DIR` is set. They check the following:
- file discovery;
- row counts and gap-free series;
- the shift fix (the kept copy reproduces clearing prices);
- the November information cut (stacks up to Oct 3 14:30 at Nov 1 00:00; lead 2 h for all
  1440 periods);
- a full `generate` run on EMA's real template.

Engine and backtest tests (`test_engine.py`, `test_backtest.py`) run on small synthetic
markets. They compare the engine with an independent scipy formulation (skipped if scipy
is absent) and also check the following:
- BESS monotonicity;
- the price-taker tie rules;
- the joint limits;
- SOC under the three conventions;
- the clip and reject policies;
- revenue signs.

The data tests in `test_engine_data.py` check:
- 200 sampled periods, including the Oct 12 16:30-17:30 scarcity;
- all 5904 periods;
- the regulation-only golden numbers;
- the CLI `backtest`.

Forecast tests (`test_forecast.py`) run on a synthetic market with two outage cycles, a
surge window and LP-cleared prices. They check that the calendar is learned exactly, the stack
lags are published and of the right period of day, copy detection, night-solar zeroing, and
forecast accuracy. They also include a no-leak property test: every stack, pre-dispatch and
clearing row not legal at the decision time is perturbed, and the lead-2 h, day-ahead and
scenario forecasts must come out bit-identical. A control test checks that perturbing legal
rows does change the forecast. `test_forecast_data.py` repeats the calendar and no-leak checks
on EMA's data and runs an October walk-forward.

Strategy tests: `test_soc_planner.py` checks the SOC formulas under every convention,
that the binding state Y is the extreme convention, the interval tracker, the exact action
grid, and the dynamic programme against brute force. `test_strategy_full.py` plans a month
on the synthetic market and checks:
- completeness, the capacity rule and always-clearing prices;
- the event bans and size caps;
- SOC under every convention with the margin and the terminal target;
- a replay in the backtest with 0 breaches in both modes;
- determinism;
- the information cut: bids for periods up to T are identical when every row not yet legal
  at T's gate is perturbed, for the default plan, the gate-time daily plan and the lead-2 h
  refinement.

`test_evaluate.py` checks the walk-forward harness and the perfect-foresight bound.
`test_strategy_data.py` (data, slow) runs the November `generate` end to end and an October
pseudo-November that must beat Regulation-only by more than 50k under both modes and all
conventions with 0 breaches.

Tests marked `slow` (all 5904 periods; Jul-Oct goldens) take about 10 s; deselect them with
`-m "not slow"`.

`BESSBID_WORK_DIR` reuses the parquet caches. Without it, the tests build the caches in a
temporary folder.
