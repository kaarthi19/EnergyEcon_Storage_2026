# Rules the code enforces, and what `generate` does

**Capacity (strict reading).** In every period, sum |energy band MW| + sum of all AS band MW
<= 100. Two alternative readings can be selected in the config, but `strict` is the default:
`max_side` (max(charge, discharge) + all AS <= 100) and `physical` (the clearing engine's
BESS rows: discharge + all AS <= 100 and charge + Regulation <= 100, so Primary or
Contingency may sit on top of charging). Ready-made configs for each reading of EMA's answer
are in [`../config/scenarios/README.md`](../config/scenarios/README.md).

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
| Training clearing | the period ended before the decision time: `ts + 30 min < T - 65 min` | `clearing_for(T)`, `clearing_known_at(t)` |

Compute every per-period feature with that period's own cutoff (`infoset.cutoff(T)`), never
from a month-wide frame. `predispatch_known_at` hides training actuals until their period
ended before `t`. `infoset.assert_no_leak()` and `infoset.audit_frame()` provide the proof for the
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

## What `generate` does

`generate` performs these steps:

1. It finds the files by name pattern anywhere under `--data-dir`. Nested folders and names
   such as `(Part 1) ...` are fine. Words are matched whole ("constrained" is not "train"),
   and when a file name does not say train or test, the nearest folder name that does
   decides (e.g. `train/pre_dispatch_forecasts.csv` vs `test/pre_dispatch_forecasts.csv`).
   CSV header names are trimmed. The template is also found there unless `--template` is
   given. **The November pre-dispatch file is required:** if it is missing, or does not give
   every period of the month a legal vintage (its own lead-2 h or lead-3 h row, issued by
   T - 65 min), `generate` stops with exit code 1 and says which file or period is missing.
   The manifest records the coverage (`inputs.predispatch_test_coverage`: periods, covered,
   first missing).
2. It runs the strategy. Every market-data read goes through the time-gated `InfoSet`.
3. It writes the bids into a copy of the template.
4. It re-reads the file with the independent validator and checks it against every template
   rule.
5. It reads the file back and confirms it matches the bids exactly.
6. For the full strategy it writes the per-period plan (`--plan-out`, default
   `<work-dir>/<out-stem>.plan.csv`): energy MW, Regulation MW, the SOC path under each
   efficiency convention, and the day-ahead, month-ahead and lead-2 h forecasts behind it.
7. It writes `<work-dir>/<out-stem>.manifest.json`. The manifest holds:
   - the input file names, sizes and SHA-256 hashes, and the template's hash;
   - the config and the `[strategy.<name>]` options used (`config`, `strategy_options`);
   - the validation result, the read-back check and a summary of the information audit;
   - the plan's diagnostics (`plan.info`) and the plan CSV's SHA-256;
   - three fingerprints of the output: the SHA-256 of the workbook, an environment-independent
     SHA-256 of the bid table, and the plan CSV hash;
   - `run_info`: start time (UTC), runtime, command line, git commit, branch and dirty flag of
     the checkout (`null` outside a git checkout), Python, package and HiGHS versions,
     platform, CPU count and peak memory.

The exit code is 0 on success, 2 if validation or the read-back fails, and 1 on usage
errors. If the full strategy cannot produce a plan at all (it is built not to fail, but as a
last resort), `generate` prints a warning and writes the Regulation-only fallback instead of
nothing; the manifest then names the error under `fallback`. Output and work dirs inside
the data dir are refused, and the data dir is never written to.

The full strategy takes about 3 minutes for November (about 340,000 LP solves).
Runs are deterministic. Nothing is random, and the workbook's created/modified times and zip
entry dates are fixed (`Config.fixed_timestamp`), so the same inputs give the same bytes.
