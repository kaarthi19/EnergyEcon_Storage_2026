# Data sources

All inputs come from the Energy Market Authority of Singapore (EMA) for its BESS bidding
hackathon. The data is **synthetic**: every file carries a `data_label` column saying it is
not actual NEMS data. It is also **confidential**: hackathon clause 21 bars sharing it outside
the competition. None of these files, and nothing derived from them, is in this repository.
`.gitignore` blocks csv, parquet, xlsx and pickle files, the `work/`, `outputs/` and
`figures/` folders, and everything under `data/` except the two README files.

How to get the data: registered hackathon teams receive it from EMA with the competition
pack. It cannot be requested from the authors of this code.

## Files

| ID | File (as released by EMA) | Vintage / release | Accessed | Coverage | Terms | Redistributable? | Location | Used by | Known quirks |
|---|---|---|---|---|---|---|---|---|---|
| clearing_train | `training_market_clearing_prices.csv` in "(Part 1) Phase_1_Competition_Training_Datasets" | EMA hackathon Phase 1 training data, released Nov 2026 | Nov 2026 | Jul 1 – Oct 31 2026, every 30-min period (5904 rows): energy, Primary, Contingency and Regulation prices, load, solar, net demand, requirements, unmet reserve | Confidential, hackathon clause 21 | No | any folder under `--data-dir` | `io.load_clearing` → `infoset.InfoSet` (clearing rows, training actuals), `backtest.MarketData`, `engine.replicate` | `unserved_energy_mw` is always 0; Primary (110 MW) and Regulation (105 MW) requirements are constant |
| predispatch_train | `training_pre_dispatch_forecasts.csv` in "(Part 1) ..." | Phase 1 training, Nov 2026 | Nov 2026 | Jul 1 – Oct 31 2026, 4 leads (0.5, 1, 2, 3 h) per period; forecast and synthetic actual load and solar, known outage at issue time | Confidential, clause 21 | No | under `--data-dir` | `io.load_predispatch("train")` → `InfoSet`, `netdemand.NetDemandModel`, `calendar.learn_calendar` | Forecast solar is above 0 in about half of the night periods, while actual solar is 0: night solar is zeroed in `netdemand.py`. `known_outage_mw_at_issue` is the outage at the issue time, not at the period |
| stacks_train_p1 | `(Part 1) training_offer_stacks_with_delay.csv` in "(Part 1) ..." | Phase 1 training, Nov 2026 | Nov 2026 | offer stacks for Jun 3 00:00 – Aug 3 11:30 2026 (after the fix below), 32 facilities × 8 rows per period (energy 5 bands; Primary, Contingency, Regulation 1 band each) | Confidential, clause 21 | No | under `--data-dir` | `io.load_stacks` → `InfoSet` (per-row publication gate), `stackmodel.StackModel`, `backtest.MarketData` | **+8 h shift:** in the first 28 days (rows with index 0–344063), `period_sgt` is 8 h later than the trading period in the `period` column. This causes the **period-33 duplicates**: the rows labelled Jul 1 00:00–07:30, period 33–48, are really Jun 30 16:00–23:30. Fixed by rebuilding the time from trading date and period (`io.rebuild_stack_ts`) and dropping duplicates only after that fix. **June stacks are an exact copy of July** shifted by 28 days: flagged by `io.copied_dates` and kept out of fits (`Config.stack_fit_start = 2026-07-01`). Publication time = period time + 28 d 8 h for every row |
| stacks_train_p2 | `(Part 2)training_offer_stacks_with_delay.csv` in "(Part 2) Phase_1_Competition_Training_Datasets" | Phase 1 training, Nov 2026 | Nov 2026 | offer stacks for Aug 3 12:00 – Oct 3 23:30 2026 | Confidential, clause 21 | No | under `--data-dir` | `io.load_stacks` (as above) | No space after "(Part 2)" in the file name. Space-separated timestamps |
| predispatch_test | `november_pre_dispatch_forecasts.csv` in "Phase_1_Competition_Test_Dataset" | Phase 1 test data, Nov 2026 | Nov 2026 | Nov 1 – Nov 30 2026, 1440 periods × 4 leads; forecast load and solar, known outage (no actuals) | Confidential, clause 21 | No | under `--data-dir` | `io.load_predispatch("test")` → `InfoSet`; the lead-2 h charge guard and `checks.calendar_consistency`. **Required:** `generate` stops (exit 1) without it | Only a period's own lead-2 h and lead-3 h rows are legal at its gate (T − 65 min); the lead-1 h and lead-0.5 h rows are issued too late |
| stacks_test | `testing_delayed_offer_stacks.csv` in "Phase_1_Competition_Test_Dataset" | Phase 1 test data, Nov 2026 | Nov 2026 | offer stacks for Oct 4 00:00 – Nov 2 23:30 2026, published Nov 1 08:00 – Dec 1 07:30 (each row has its own publication time) | Confidential, clause 21 | No | under `--data-dir` | `io.load_stacks` → `InfoSet` (publication gate), `stackmodel.StackModel` | **Mixed timestamp formats:** ISO `T` in `period_sgt`, a space in `assumed_publication_time_sgt` (the training files use spaces). All offsets are +08:00, parsed to naive SGT by `io.parse_sgt`. The unnamed index column restarts at 0. `data_label` text differs slightly between files |
| template | `BESS_Competition_Template.xlsx` (top of the data folder) | Phase 1, Nov 2026 | Nov 2026 | bid sheet layout for 1440 periods × 4 products, plus the BESS assumptions | Confidential, clause 21 | No | under `--data-dir`, or pass `--template` | `io.find_template` → `writer.write_submission`; `validate.validate_xlsx` checks the output against its rules | Prices and quantities use the number format `#,##0.00;[Red]\(#,##0.00\);\-`, so 0.00 shows as "-". Always-clearing offers are therefore priced at 0.01 |

File discovery (`io.find_files`) looks at the name of every file under `--data-dir`:
"offer_stack" marks a stack file, "pre_dispatch" (or "predispatch") a pre-dispatch file,
"clearing" plus "price" a clearing file, and "template" in an `.xlsx` name the template.
The whole words "train"/"training" and "test"/"testing"/"november" decide training versus
test. When the file name has neither, the nearest folder name that does decides. "(Part 1)"
prefixes, spaces and nesting do not matter, so EMA's own folder layout works.

## Manual fixes applied in code

All fixes are in `bessbid/io.py`. The raw files are never changed.

| Code | Fix | Evidence |
|---|---|---|
| `io.rebuild_stack_ts` | stack period time = trading date + (period − 1) × 30 min | After the fix, `period` equals the period of day in 100 % of rows, there are no duplicates, and the series from Jun 3 to Nov 2 has no gaps. The kept copy for Jul 1 00:00–07:30 reproduces the clearing price at its marginal offer to 0.05 %; the dropped copy is off by 108 % |
| `io._build_stacks` | drop duplicates only after the time fix; the key (ts, facility, product, band) must then be unique | as above |
| `io.copied_dates`, `Config.stack_fit_start` | leave June stacks out of model fits | June equals July shifted by −28 days in every cell |
| `io.parse_sgt` | parse mixed `T`/space ISO timestamps with offsets into naive SGT | format differences between files |
| `io._read_csv` | trim header names | robustness against EMA re-saving the files |

## Data we created

None. There is no hand-collected or digitised data. Everything the code uses is computed
from the files above at run time, and stays in the work dir.
