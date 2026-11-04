# BESS Bidding Algorithm: EMA Hackathon (Nov 2026)

This is a bidding algorithm for the [EMA BESS Bidding Algorithm Hackathon](https://www.ema.gov.sg/content/dam/corporate/our-energy-story/energy-grid/EMA-Our-Energy-Story-Energy-Grid-BESS-Bidding-Algorithm-Hackathon.pdf.coredownload.pdf). It bids a 100 MW / 100 MWh battery (5–95 % SOC, 85 % round-trip efficiency) into the Singapore Wholesale Electricity Market.

For every half-hourly trading period it produces price–quantity offers for:
- **Energy**: an RC383 *energy storage offer* (Market Rules Ch. 6 §5.2B). Pairs 1–5 charge (≤ 0 MW) and pairs 6–10 discharge (≥ 0 MW);
- **Regulation**
- **Primary Reserve**
- **Contingency Reserve**

The package adapts the storage model in `../code/bilevel_cvx.jl`. It keeps Julia and JuMP but replaces Gurobi with the open-source HiGHS solver, so EMA can run it without a commercial licence.

- Method: [`docs/ALGORITHM.md`](docs/ALGORITHM.md) (submission item 7b)
- Data and software: [`docs/EXTERNAL_DATA_AND_SOFTWARE.md`](docs/EXTERNAL_DATA_AND_SOFTWARE.md) (submission item 7c)

## Install

You need Julia 1.10 (LTS) or later.

```bash
julia --project=hackathon -e 'using Pkg; Pkg.instantiate()'
```

## Quick start

All commands run from the repository root.

1. Run the tests:

```bash
julia --project=hackathon hackathon/test/runtests.jl
```

2. Create synthetic data, used until EMA releases the datasets on 13 Nov 2026:

```bash
julia --project=hackathon hackathon/scripts/make_synthetic.jl --days 21
```

3. Run a closed-loop backtest. Results go to `hackathon/results/backtest/`:

```bash
julia --project=hackathon hackathon/scripts/backtest.jl --data hackathon/data/synthetic/market.csv
```

4. Generate offers from the data available at a decision time and the current SOC:

```bash
julia --project=hackathon hackathon/scripts/generate_bids.jl --data hackathon/data/synthetic/market.csv --origin 2026-01-10T17:30:00 --soc 60 --out hackathon/results/bids.csv
```

### Backtest options

| Option | Meaning |
|---|---|
| `--config <toml>` | Configuration file (default `configs/default.toml`) |
| `--start <timestamp or row>` | First evaluated period (default: day 8) |
| `--end <timestamp or row>` | Last evaluated period |
| `--out <dir>` | Output folder |

The backtest writes three files:
- `periods.csv`: dispatch, awards, SOC and revenue for each period;
- `bids.csv`: every offer pair. Energy rows are RC383 slots 1–10, with the offered capacity and ramp rates;
- `summary.csv`: revenue by product, penalties, violations, cycles, and the perfect-foresight bound with the capture ratio.

### From Julia

```julia
include("hackathon/src/BESSBidding.jl"); using .BESSBidding
cfg = load_config("hackathon/configs/default.toml")
df  = load_market_data("path/to/ema_data", cfg)
bids = generate_bids(df, origin_row, soc_mwh, cfg)   # Vector{PeriodBids}
write_bids("bids.csv", bids, cfg)
```

`generate_bids` reads only rows up to `origin_row`, plus pre-dispatch values within the pre-dispatch horizon. It works when the table ends at the decision time, so EMA's simulator can call it once per trading period (rolling mode) or once per day (day-ahead mode).

## When EMA releases the data (13 Nov 2026)

1. Map EMA's column names in `[data]` / `[data.columns]` of the config. The loader accepts either a timestamp column or date plus trading-period columns. It accepts one CSV or a folder of CSVs joined on time.
2. Check the remaining `ASSUMPTION`s in `configs/default.toml` against EMA's notes and the SWEM Market Rules. RC383 already fixes the energy storage offer format, per-class reserve headroom and 65-minute gate closure. These are still open:
   - the Appendix 6J price limits;
   - the reserve and regulation offer format;
   - reserve deployment and sustain duration;
   - ramp rates;
   - how EMA's simulator applies gate closure (`lead_periods`);
   - the initial SOC.

3. Backtest on EMA's data up to the cut-off. Tune these parameters:
   - `forecast.method`;
   - `battery.degradation_cost`;
   - `strategy.horizon_periods`;
   - the grid quantiles;
   - `strategy.soc_guard`.
4. Add the sources of any external data to `docs/EXTERNAL_DATA_AND_SOFTWARE.md`.

## Layout

```
hackathon/
├── Project.toml, Manifest.toml   # pinned Julia environment
├── configs/default.toml          # battery, market-rule assumptions, strategy, data mapping
├── src/
│   ├── BESSBidding.jl            # module
│   ├── config.jl                 # typed configuration
│   ├── data.jl                   # EMA data loading -> canonical half-hourly table
│   ├── synthetic.jl              # synthetic SWEM-like data for development
│   ├── forecast.jl               # ridge / pre-dispatch / seasonal-naive forecasts (no look-ahead)
│   ├── optimizer.jl              # energy + reserve co-optimisation (JuMP + HiGHS)
│   ├── strategy.jl               # price sweeps -> offers; rolling and day-ahead modes
│   ├── bids.jl                   # offer curves, SOC safety guard, validation, export
│   ├── simulator.jl              # local co-optimised, price-taking market clearing
│   ├── backtest.jl               # closed-loop backtest + perfect-foresight benchmark
│   └── io.jl                     # outputs, generate_bids entry point
├── scripts/                      # command-line entry points
├── test/runtests.jl
└── docs/
```

## Results on synthetic data

The backtests ran over 14 synthetic days in rolling mode with the ridge forecast, using `scripts/backtest.jl`. Offers follow the RC383 format and per-class reserve headroom.

| | Default: 3-period lead (65-min gate closure), 0.999 quantile guard | 1-period lead |
|---|---|---|
| Net market revenue | **$726k** (E 168k / Reg 431k / Pri 12k / Con 114k) | $941k (E 162k / Reg 598k / Pri 13k / Con 169k) |
| Share of perfect foresight ($1,047k) | **69 %** | 90 % |
| SOC / sustainability violations | **0** (SOC 5.5–94.5 MWh) | 0 |
| Cycles per day | 0.61 | 0.54 |

The difference between the two columns is the cost of gate closure. The bid for each period is fixed while the two periods before it are still open, so the offers must stay SOC-feasible over those uncertain outcomes. If EMA's simulator lets offers be revised later, set `strategy.lead_periods` to match.

Other guard settings, 5 synthetic days, 3-period lead:

| `soc_guard` | Share of perfect foresight | Violations |
|---|---|---|
| `worst_case` | 34 % | 0 |
| `quantile`, q = 0.999 | 56 % | 0 |
| `quantile`, q = 0.99 | 55 % | 3 |
| `expected` | 67 % | 29 |

Synthetic prices are illustrative only. Expect different numbers on EMA's data.

## IP and confidentiality

Clause 20 of the Call for Participation assigns all IP in submitted code and materials to EMA on submission. Clause 21 restricts disclosure of hackathon data. Agree how this package will be submitted with the repository's authors and your institution before you submit. Do not commit EMA's datasets to this public repository. `hackathon/data/` and `hackathon/results/` are git-ignored.
