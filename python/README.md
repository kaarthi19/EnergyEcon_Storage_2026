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
    --strategy regulation            # or: full (once the full strategy is added)
    [--template /path/to/BESS_Competition_Template.xlsx] [--month 2026-11] [--config overrides.toml]

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
6. It writes `<work-dir>/<out-stem>.manifest.json`. The manifest holds the input file names
   and sizes, the config, the validation result, a summary of the information audit, and two
   fingerprints: the SHA-256 of the workbook and an environment-independent SHA-256 of the
   bid table.

The exit code is 0 on success, 2 if validation or the read-back fails, and 1 on usage
errors. Output and work dirs inside the data dir are refused, and the data dir is never
written to.

Runs are deterministic. Nothing is random, and the workbook's created/modified times and zip
entry dates are fixed (`Config.fixed_timestamp`), so the same inputs give the same bytes.

### Fallback submission: `--strategy regulation`

Each period offers 100 MW of Regulation in one band at 0.00. It makes no Energy, Primary or
Contingency offers: those rows are written with every band blank, as the template's own
example does for period 2.

Why this is the fallback:

- Regulation cleared above Contingency and Primary in every training period.
- Activation is 0 MWh, so SOC stays at 50 % and cannot breach a limit under any efficiency
  convention.
- It reads no market data, so it cannot break an information rule.
- An offer at 0.00 clears whether EMA re-clears the market with our offers or prices them
  at fixed clearing prices.

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
- Always-clearing offers are priced as follows: discharge at `discharge_floor_price` = 0.00,
  charge at `charge_cap_price` = 3000.00, and AS at `as_floor_price` = 0.00.

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
| `cli.py` | `python -m bessbid generate \| validate \| inspect \| backtest` |

### Extension points

A strategy is any object with a `name`, a `needs_data` flag, and
`generate(month, infoset, cfg) -> list[PeriodBid]` returning one bid per period. Register it
under a name:

```python
from bessbid.strategy import register_strategy
register_strategy("full", "bessbid.strategy_full:FullStrategy")   # already pre-registered
```

`--strategy full` loads `bessbid/strategy_full.py:FullStrategy` lazily. Until that module
exists, the CLI reports it as unavailable.

Planned modules:
- `calendar.py`: outage and event cycles.
- `netdemand.py`
- `stackmodel.py`
- `forecast.py`
- `planner.py`: SOC plan under every efficiency convention in
  `Config.efficiency_conventions`, keeping `soc_margin_mwh`.
- `soc.py`

These modules should take data only from an `InfoSet` and build bids only with the
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

Tests marked `slow` (all 5904 periods; Jul-Oct goldens) take about 10 s; deselect them with
`-m "not slow"`.

`BESSBID_WORK_DIR` reuses the parquet caches. Without it, the tests build the caches in a
temporary folder.
