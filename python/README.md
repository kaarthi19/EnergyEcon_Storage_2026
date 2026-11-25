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
| `cli.py` | `python -m bessbid generate \| validate \| inspect` |

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
- `engine.py`: per-period clearing LP with highspy.
- `backtest.py`
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

`BESSBID_WORK_DIR` reuses the parquet caches. Without it, the tests build the caches in a
temporary folder.
