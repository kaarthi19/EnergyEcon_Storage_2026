# Tests

```bash
cd python
python -m pytest                       # unit tests on small synthetic markets (about 5 min)
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
  at T's gate is perturbed, for T on the first, a middle and the last day of the month, and
  for the gate-time daily plan and the lead-2 h refinement;
- the ramp limit (every step <= 42 MW from 0 MW at the start) and switching it off;
- a guard that fires on every charge keeps the month-end SOC target (it used to abort);
- a planner input issued after its decision time raises `LeakError`.

`test_event_recharge.py` checks the optional overnight recharge exception (`event_recharge`):
the eligibility mask on a hand-made calendar (night hours only, the 19-day stress cycle
only, never where an 11-day window overlaps; `"overnight_all"` adds those; hours that wrap
past midnight), that the price cap and the margin ban still hold in the allowed-action
matrix, and, on the synthetic market, that charging inside events happens only in eligible
periods while the capacity, ramp, SOC (three conventions, 2 MWh margin) and terminal rules
hold, that an exception whose price cap or hurdle can never be met reproduces the default
bids exactly, and the per-period information cut with the exception switched on.

`test_event_bias.py` checks the optional event-window bias correction (`event_bias`): the
bucket labels on a hand-made calendar (event day counted from the onset day, surge flag,
hour block, `"overlap"` where two windows overlap), the estimator (mean, clip, shrinkage; no
correction outside events, in overlaps or in buckets without residuals), that the
forecaster's backcast mode is scoped to its `with` block, and on the synthetic market (a
September plan, since the synthetic stacks start on Jul 1): the residuals use only periods
that ended before the fit cut and are audited as `eventbias.fit`; perturbing every
clearing, pre-dispatch and stack row not known at the cut leaves the corrections
bit-identical, while changing a known clearing price changes them; the corrected plan keeps
the capacity, ramp, SOC (three conventions, 2 MWh margin), terminal and event-ban rules;
a zero correction reproduces the default bids exactly; and bids up to T are identical when
every row not legal at T's gate is perturbed (T on the first day and inside an event).
`test_event_bias_data.py` (data) fits the correction at the November cut on EMA's data:
one audit entry at the cut, upward corrections in the surge nights and downward ones in the
11-day windows and on the onset afternoon, five July-October events behind the X+1
surge-night buckets, and a correction in exactly the 267 November event periods; and at the
August cut, that perturbing every row not known at Jul 31 00:00 leaves the July-only
correction bit-identical.

`test_soc_planner.py` also checks the ramp-limited programme against brute force and that
it equals the plain programme without a limit. `test_engine.py` checks that non-finite
inputs raise. `test_writer_cli.py` checks that `generate` stops (exit 1) when the test
pre-dispatch file is missing or covers only part of the month, that
`--allow-incomplete-inputs` turns this into a warning, that the coverage counts only legal
lead-2 h / 3 h vintages, and the Regulation-only fallback when planning fails.
`test_strategy_full.py` also checks the calendar check (consistent on the synthetic market,
flagged for a calendar shifted by one day). `test_io.py` checks whole-word and
folder-based file classification and header trimming.

`test_contingency.py` checks the options behind the scenario configs
(`config/scenarios/`): the `physical` capacity reading in the builders and the validator;
`regulation_cap` against brute force; that `soc.DeviationTracker` keeps a planned path and
the month-end target feasible when any subset of the extra bands clears; that the SOC
programme gives the same result for state-dependent values that do not depend on the state;
`single_miss_exposure` on a hand-made path; and, on the synthetic market, that with no
option set the `physical` reading gives the default bids, that `reserve_fill` puts
Contingency exactly on the charging MW, that both Regulation sustainability rules keep every
period's Regulation within the cap, that price-responsive bands leave the planned schedule
untouched (tracker mode: 0 breaches with no SOC policy, both modes, every convention; local
mode: 0 breaches under clip and reject) and respect the information cut, the evaluator's
`soc_policy` and capped Regulation baseline, and that every scenario file loads with the
initial SOC on the planning grid.

`test_evaluate.py` checks the walk-forward harness and the perfect-foresight bound.
`test_strategy_data.py` (data, slow) runs the November `generate` end to end and an October
pseudo-November that must beat Regulation-only by more than 50k under both modes and all
conventions with 0 breaches.

Tests marked `slow` (all 5904 periods; Jul-Oct goldens) take about 10 s; deselect them with
`-m "not slow"`.

`test_config_periods.py` checks the config file handling: overrides in TOML and JSON,
unknown keys, `config/default.toml` reproducing every default exactly (and naming every key
of `Config`, `FullParams` and the fallback), the `[strategy.<name>]` tables with type
conversion, and the CLI refusing a table for an unknown strategy.

`BESSBID_WORK_DIR` reuses the parquet caches. Without it, the tests build the caches in a
temporary folder.
