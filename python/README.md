# bessbid: bid generator for EMA's BESS bidding hackathon

`bessbid` is our team's entry to the Energy Market Authority of Singapore (EMA) BESS bidding
hackathon. It turns EMA's competition data into a November 2026 bid schedule in EMA's Excel
template, and the code reproduces that schedule. The battery is EMA's hypothetical 100 MW /
100 MWh BESS: SOC 5-95 %, 85 % round trip, 50 % SOC at the start of Nov 1, 30-minute
periods. It bids in four products: Energy, Primary Reserve, Contingency Reserve and
Regulation.

The strategy in one paragraph: every period offers Regulation, the most valuable product in
every training period, at a price that always clears. On top of that sits a planned energy
schedule offered as fixed quantities that always clear, so the SOC path is known without
feedback from EMA. A dynamic programme over SOC sets those quantities from structural price
forecasts that include our own price impact: the forecast offer stack is cleared by a
replica of EMA's clearing LP with our offer inside. The plan respects the strict 100 MW
capacity rule, the SOC limits under every efficiency convention, a ramp limit and the
information rules. In walk-forward tests on August-October it adds about +115k S$ per
30 days over Regulation alone as a price maker (+130k as a price taker). For November we
expect about +100k. The full strategy description is in
[`docs/strategy.md`](docs/strategy.md).

This is a hackathon submission, not a paper, so there is no DOI, archive or citation file.
No competition data, and nothing derived from it, is stored in this repository (hackathon
clause 21).

## Quick start

You need Python 3.10 or newer and EMA's data folder. Run these commands from a checkout of
this repository. They were tested verbatim in a fresh virtual environment, with Python 3.13
on Linux.

```bash
cd python
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-figures.txt

# unit tests on small synthetic markets; no EMA data needed (about 6 minutes)
python -m pytest -q

# November bids, validation and figures (about 4 minutes)
./run_all.sh --skip-tests --data-dir "/path/to/ema bidding comp" --work-dir /path/to/work
```

Use a work dir outside the repository: everything written there derives from EMA's
confidential data. The run ends with:

```
Done. Workbook: /path/to/work/submission.xlsx
      Plan: /path/to/work/submission.plan.csv; manifest: /path/to/work/submission.manifest.json; figures: /path/to/work/figures/
```

`requirements.txt` pins two tested sets of versions: one for Python 3.12 and newer, and one
for Python 3.10 and 3.11. With either set the workbook is byte-identical.
`requirements-figures.txt` adds matplotlib; it is needed only for the figures. Without it,
pass `--skip-figures`.

## Reproducing the submission

`./run_all.sh --data-dir D --work-dir W` runs the whole pipeline. Without `--skip-tests` it
first runs the unit tests. It writes:

| Output (in `W`) | Command | Reads | Runtime |
|---|---|---|---|
| `submission.xlsx`, the bid workbook for EMA | `python -m bessbid generate --data-dir D --work-dir W --out W/submission.xlsx --plan-out W/submission.plan.csv --config config/default.toml` | EMA's data folder `D` | about 3.2 min (about 340,000 LP solves) |
| `submission.plan.csv`, the per-period plan: energy MW, Regulation MW, SOC under every convention, forecasts | the same `generate` call | | |
| `submission.manifest.json`: inputs and their hashes, config, checks, information audit, run record | the same `generate` call | | |
| validation report (stdout) | `python -m bessbid validate W/submission.xlsx` | the workbook | < 5 s |
| `figures/fig01_stress_window.{pdf,svg,png}` (Nov 16-21) and `figures/figS01_november_plan.*` (the month), each with a `*_numbers.json` sidecar | `python plots/fig_november_plan.py W/submission.plan.csv W/figures` | the plan CSV | < 30 s |
| Aug-Oct walk-forward table (`docs/strategy.md`) | `python -m bessbid evaluate --data-dir D --work-dir W` | EMA's data | about 15 min |
| replication of all 5904 training clearing prices (`docs/market_model.md`) | `BESSBID_DATA_DIR=D python -m pytest tests/test_engine_data.py` | EMA's data | < 1 min |

Runtimes were measured on a Linux machine with 4 vCPUs (Intel Xeon, 2.1 GHz) and 15 GB of
memory. `generate` uses one core and about 0.7 GB of memory at peak. The first run on a new
work dir also builds parquet caches of the inputs, which adds about 40 s and raises the
peak to about 2 GB.

**Determinism.** Nothing is random: HiGHS runs single-threaded with a fixed seed, and the
workbook's created/modified times and zip entry dates are fixed. The same inputs therefore
give the same bytes. On EMA's Phase 1 data, with the pinned versions, the November workbook
has these fingerprints, which `generate` writes to the manifest:

| | SHA-256 |
|---|---|
| `submission.xlsx` | `c6260df22a9262adfc8bc3ea4743f2502b21d0c93e0e070e35d1cc3d831ed1a5` |
| bid table (`table_sha256`, independent of the xlsx writer) | `2240964c8146686f238c01d686b897774955ed4aaaafd73229675c8c25c2ce30` |
| `submission.plan.csv` | `15d9dd1a466c3a75c6f70b59fffd3f9d8fa7180d4bed8c04484fe7a61bb3cb7e` |

Running with `--config config/default.toml` or with no `--config` gives the same bytes.

**Checks in every run.** `generate` checks the workbook against every template rule with an
independent validator, reads it back and compares it with the bids, and checks that no input
was used before its legal time (`assert_no_leak`). It stops with exit code 1 if the November
pre-dispatch file is missing or incomplete. Details: [`docs/rules.md`](docs/rules.md).

**Command reference.**

```bash
python -m bessbid generate --data-dir D --work-dir W --out W/submission.xlsx \
    [--strategy full|regulation] [--plan-out FILE.csv] [--template FILE.xlsx] \
    [--month 2026-11] [--config FILE.toml] [--allow-incomplete-inputs]
python -m bessbid validate W/submission.xlsx [--month 2026-11] [--config FILE.toml]
python -m bessbid inspect --data-dir D --work-dir W        # find and load the data, print a summary
python -m bessbid evaluate --data-dir D --work-dir W [--month 2026-08 ...] [--no-pf] [--config FILE.toml]
python -m bessbid backtest --data-dir D --work-dir W (--bids X.xlsx | --strategy NAME) \
    [--month 2026-10 ...] [--mode maker|taker|both] [--convention sqrt|charge|discharge|all] \
    [--soc-policy none|clip|reject] [--tie strict|inclusive] [--config FILE.toml]
python -m bessbid.forecast --data-dir D --work-dir W --month 2026-11   # a month of forecasts
```

- `--strategy regulation` writes the fallback: 100 MW of Regulation at 0.01 in every period.
  It reads no market data.
- `--template` is required only if the data folder does not contain
  `BESS_Competition_Template.xlsx`.
- `--allow-incomplete-inputs` is for diagnostics only, not for a submission.

Exit codes: 0 for success, 1 for a usage or input error, 2 if validation or the read-back
fails.

## Data

EMA's hackathon datasets are **confidential** (hackathon clause 21) and are **not included**.
Registered teams receive them from EMA; they are synthetic but may not be redistributed.
Put the folder anywhere outside the repository and pass it with `--data-dir`. The code finds
the files by name, at any depth.

- [`data/DATA_SOURCES.md`](data/DATA_SOURCES.md) lists each of the seven files: vintage,
  terms, the module that reads it, and its known quirks. The quirks include the +8 h
  timestamp shift in the Part 1 stacks (the cause of the period-33 duplicates), the June
  stacks copied from July, and mixed timestamp formats.
- [`data/README.md`](data/README.md) explains where to put the data.

Caches, bids, plans, manifests and figures go to `--work-dir` and are never committed.
`.gitignore` blocks csv, parquet, xlsx and pickle files, `figures/`, `outputs/`, `work/`,
and everything in `data/` except its two README files.

## Repository structure

```
python/
├── README.md                  this file
├── run_all.sh                 tests -> generate -> validate -> figures
├── requirements.txt           pinned runtime and test dependencies
├── requirements-figures.txt   pinned matplotlib (figures only)
├── pyproject.toml             package metadata (pip install .)
├── config/default.toml        every configuration key with its default value
├── bessbid/                   the package (modules below)
├── plots/                     figure scripts and labstyle.py (lab figure style)
├── tests/                     pytest; data tests run only with BESSBID_DATA_DIR set
├── data/                      README and DATA_SOURCES.md only; the data stays outside the repo
└── docs/                      formulation, units, strategy, rules, market model, tests
```

| Module | Contents |
|---|---|
| `config.py` | `Config` (frozen dataclass), `load_config(path)` and `load_strategy_options(path)`. TOML/JSON overrides may be flat or grouped in tables; unknown keys are errors |
| `periods.py` | `month_slots(month)`, `period_start(date, k)`, `period_of(ts)`. All times are naive SGT |
| `io.py` | `find_files`, `find_template`, `load_clearing`, `load_predispatch(kind)`, `load_stacks`, `load_all`, `stack_report`, `copied_dates`, `file_sha256`. Parquet cache in `<work>/cache`, keyed by file name, size and mtime |
| `infoset.py` | `InfoSet` (time-gated, audited access to every input), `AuditEntry`, `LeakError` |
| `bids.py` | `Band`, `BidRow`, `PeriodBid`; builders `regulation_only`, `energy_fixed`, `energy_curve`, `make_period_bid(clip_as=...)`; `to_table` / `from_table` |
| `validate.py` | `validate_table(df, cfg, month)`, `validate_xlsx(path, cfg, month)`, `Violation`, `format_report` |
| `writer.py` | `write_submission(bids, template, out, cfg, month)` and `read_back(path)` |
| `strategy.py` | the `Strategy` protocol, `register_strategy`, `get_strategy(name, options=...)`, `check_complete` |
| `strategy_regulation.py` | `RegulationOnlyStrategy` (the fallback) |
| `strategy_full.py` | `FullParams` (`from_mapping`), `plan_month(month, infoset, cfg, params) -> (bids, plan frame, info)`, `FullStrategy` (registered as `full`, the CLI default) |
| `engine.py` | EMA-replica clearing LP: `ClearingEngine`, `clear`, `clear_period`, `price_taker_clear`, `replicate`; `StackArrays`, `PeriodStack`, `BessOffer`, `ClearResult` |
| `planner.py` | `ValueModel` (forecast stack cleared with our own offer), `make_action_grid`, `action_values`, `dp_backward` / `dp_forward` / `dp_plan`, `dp_backward_ramp` / `dp_forward_ramp`, `soften_end_values` |
| `soc.py` | SOC under every efficiency convention: `soc_paths`, `binding_soc`, `check_soc`, `y_bounds`, `y_terminal_min`, `SocIntervalTracker` (tested, not used by the shipped strategy) |
| `calendar.py` | `learn_calendar(infoset, cut) -> EventCalendar` (outage cycles, contingency and surge windows learned from data) |
| `netdemand.py` | `NetDemandModel`: legal lead-2 h / 3 h net demand, day-ahead profile plus surge, error nodes |
| `stackmodel.py` | `StackModel`: offer-price factor model and the blend of the legal stacks (`forecast_stack(T, decision_time, outage)`) |
| `forecast.py` | `Forecaster(data, cfg).fit(cut)`: `.lead2`, `.lead2_many`, `.day_ahead`, `.scenarios`; `walk_forward`, `evaluate`, `month_forecasts` |
| `_clearlp.py` | small reference clearing LP, cross-checked against `engine.py` in the tests |
| `checks.py` | `calendar_consistency(fc, Ts)`: the learned event calendar against each period's legal pre-dispatch vintage (reported only) |
| `backtest.py` | `simulate(bids, month, data, mode, convention, soc_policy, ...)`, `run_months`, `MarketData`, `soc_path` |
| `evaluate.py` | `evaluate(ds, cfg, months, params)`: walk-forward pseudo-Novembers, against Regulation-only and `perfect_foresight_plan` |
| `runinfo.py` | `run_info`: git commit and dirty flag, Python, package and HiGHS versions, platform, runtime (the manifest's `run_info`) |
| `cli.py` | `python -m bessbid generate \| validate \| inspect \| backtest \| evaluate` |

**Adding a strategy.** A strategy is any object with a `name`, a `needs_data` flag and
`generate(month, infoset, cfg) -> list[PeriodBid]` that returns one bid per period. Register
it with `bessbid.strategy.register_strategy("name", "module:Factory")`. A strategy should
read data only from the `InfoSet` and build bids only with the `bids.py` builders, which
enforce rounding and the capacity rule. `write_submission` refuses tables that fail
`validate_table`.

## Model notes

- **Formulation.** [`docs/formulation.md`](docs/formulation.md) states the clearing LP
  (Eq. (1)-(11)) and the planner (Eq. (12)-(23)): the SOC dynamic programme, the value
  model with price impact, the capacity rule, the SOC limits under three conventions with
  margin, the ramp limit, the event charging ban and the terminal SOC. The matching code
  lines in `engine.py`, `planner.py`, `soc.py`, `strategy_full.py` and `bids.py` carry
  `# Eq. (n)` comments. Units and signs are in [`docs/units.md`](docs/units.md).
- **Strategy, results and open questions.** [`docs/strategy.md`](docs/strategy.md) covers
  the strategy, the walk-forward results, the investigations, what to expect for November,
  and the assumptions to confirm with EMA.
- **Rules.** [`docs/rules.md`](docs/rules.md) covers the capacity rule, the offer rules, the
  information rules, the data fixes, and what `generate` does step by step.
- **Market engine, backtest and forecasts.** See
  [`docs/market_model.md`](docs/market_model.md).
- **Tests.** [`docs/testing.md`](docs/testing.md) describes what each test file checks.
- **Software.** Python >= 3.10, tested with 3.13 (numpy 2.5.3, pandas 3.0.5) and 3.11
  (numpy 2.2.6, pandas 2.2.3). The LP solver is HiGHS 1.16.0 through `highspy` 1.16.0. It
  is open source and the only solver used, so there is no commercial-solver configuration
  and no reduced configuration: the full November run takes about 4 minutes on one core.
  The exact versions of each run are in the manifest's `run_info`.
- **Configuration.** [`config/default.toml`](config/default.toml) lists every key with its
  default value and a comment. A file passed with `--config` needs only the keys it
  changes. `[strategy.full]` sets the strategy parameters (`FullParams`) and
  `[strategy.regulation]` sets the fallback's. The main options:

  | Key | Default | Meaning |
  |---|---|---|
  | `capacity_rule` | `"strict"` | sum of \|energy MW\| + all AS MW <= 100 per period (`"max_side"`: the looser reading) |
  | `soc_margin_mwh` | 2.0 | SOC margin inside 5-95 MWh kept by the planner |
  | `terminal_soc_frac` | 0.5 | minimum SOC at the end of the month, under every convention |
  | `bess_ramp_mw` | 42.0 | largest change of the energy schedule per period (0 = no limit) |
  | `gate_closure_min` | 65 | information cut: data for period T must be available by T - 65 min |
  | `[strategy.full] charge_cap_mw`, `discharge_cap_mw`, `event_discharge_cap_mw` | 50, 50, 100 | size caps of the energy actions (MW) |
  | `[strategy.full] guard_margin_mw`, `guard_price` | 300, 400 | charging bans and the lead-2 h charge guard |
  | `[strategy.full] refine` | `"guard"` | `"none"`, `"guard"` or `"lead2"` (gate-time re-optimisation) |

## Licence

Licensing of this code is pending. The hackathon terms (clause 20) assign intellectual
property in submissions to EMA, and the team will decide the licence once that is settled.
The MIT `LICENSE` at the repository root comes from the repository template and predates
the hackathon; this folder adds no licence file of its own. EMA's data is not covered by
any licence here: it stays under the hackathon's confidentiality terms (clause 21).

## Contact

Team contact: to be added before the submission.
