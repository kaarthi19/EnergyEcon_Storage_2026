# Bilevel Optimization for Energy Storage Bidding

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

This project implements a bilevel optimization model for strategic storage bidding in electricity markets. The upper level optimizes storage bidding decisions, while the lower level solves the economic dispatch problem that clears the market. The repository also contains the Python scripts that reproduce every table and figure in the paper from the model outputs.

> **EMA BESS Bidding Algorithm Hackathon (Nov 2026):** [`hackathon/`](hackathon/README.md) adapts this repository's storage model into a bidding algorithm for the Singapore Wholesale Electricity Market. It covers energy, regulation, primary reserve and contingency reserve, and is written in Julia with JuMP and the open-source HiGHS solver.

## Contents

- [Key Features](#key-features)
- [Installation](#installation)
  - [Julia model](#julia-model)
  - [Python analysis](#python-analysis)
- [Project Structure](#project-structure)
- [Model Usage](#model-usage)
  - [Quick Test Run](#quick-test-run)
  - [Scenario Runs](#scenario-runs)
  - [Model Outputs](#model-outputs)
  - [Batch Processing](#batch-processing)
  - [Overview of All Scenarios](#overview-of-all-scenarios)
- [Analysis and Figure Reproduction](#analysis-and-figure-reproduction)
  - [Overview of Reproduction Steps](#overview-of-reproduction-steps)
  - [Generate Summary Statistics](#generate-summary-statistics)
  - [Generate Tables and Figures](#generate-tables-and-figures)
- [Data Sources](#data-sources)
- [License](#license)
- [Maintainer](#maintainer)

## Key Features

- **Bilevel Optimization**: Implements convex bilevel formulation for storage bidding
- **Economic Dispatch**: Solves lower-level market clearing with variable renewable energy (VRE) integration
- **Storage Modeling**: Supports configurable storage capacity, duration, and efficiency parameters
- **Scenario Setting**: Supports different VRE penetration levels, storage capacities, renewable production incentives, and other sensitivity factors
- **Reproducible Analysis**: One Python script per table/figure, reading either the processed results included in this repo or your own model runs

## Installation

### Julia model

**Prerequisites**

- [Julia](https://julialang.org/downloads/) 1.9.3
- [Gurobi Optimizer](https://www.gurobi.com/downloads/gurobi-software/) 10.0.1 with a valid license (academic licenses available)

**Setup**

1. Clone the repository:
   ```bash
   git clone https://github.com/Power-Lab/EnergyEcon_Storage_2026.git
   cd EnergyEcon_Storage_2026
   ```

2. Install the Julia packages used by the scripts in `code/`:
   ```julia
   using Pkg
   Pkg.add([
       PackageSpec(name="JuMP",        version="1.15.1"),
       PackageSpec(name="BilevelJuMP", version="0.6.2"),
       PackageSpec(name="Gurobi",      version="1.0.4"),
       PackageSpec(name="HiGHS",       version="1.7.2"),
       PackageSpec(name="DataFrames",  version="1.6.1"),
       PackageSpec(name="CSV",         version="0.10.11"),
       PackageSpec(name="PrettyTables",version="2.2.8"),
       PackageSpec(name="FileIO",      version="1.16.1"),
       PackageSpec(name="Plots",       version="1.39.0"),
       PackageSpec(name="VegaLite",    version="3.2.3"),
   ])
   ```

3. Set up the Gurobi license (follow the Gurobi installation instructions).

### Python analysis

The scripts in `analysis/` require Python 3.9 and the following package versions:

```bash
pip install numpy==1.26.4 pandas==1.5.3 plotly==5.14.0 kaleido==0.2.1
```

## Project Structure

```
├── code/                          # Julia source code
│   ├── bilevel_cvx.jl             # Strategic Storage: convex bilevel formulation
│   ├── ed.jl                      # Central Control: economic dispatch model
│   ├── run.jl                     # Quick single-period test run (parameters set inside the script)
│   └── run_all_periods.jl         # Full multi-period run (parameters set by the run name)
├── data/                          # Input datasets
│   ├── data_WECC_small_mod/       # WECC system used for all model runs in the paper
│   ├── data_WECC_large/           # Full WECC system
├── batch/                         # SLURM batch scripts
├── result/                        # Raw model output, one folder per scenario (created by the model)
├── analysis/                      # Reproduces the tables and figures in the paper
│   ├── common.py                  # Shared helpers and plot styling
│   ├── result_summary.py          # Builds result_summary.csv from the scenario folders
│   ├── result_summary.csv         # Scenario-level summary metrics
│   ├── plot_table1.py             # Table 1
│   ├── plot_table2.py             # Table 2
│   ├── plot_table4.py             # Table 4
│   ├── plot_table5.py             # Table 5
│   ├── plot_figure3.py            # Figure 3
│   ├── plot_figure4.py            # Figure 4
│   ├── plot_figure5.py            # Figure 5
│   ├── plot_figure6.py            # Figure 6
│   ├── output/                    # Tables (.csv) and figures (.pdf) written by the plot scripts
│   └── result_simplified/         # Processed model outputs used by the plot scripts
└── LICENSE                        # MIT License
```

## Model Usage

### Quick Test Run

To check that your Julia and Gurobi setup works, run the single-period test script:

```bash
julia code/run.jl
```

`run.jl` simulates one 4-day window (week 2) on the small `data_WECC_very_small` dataset with 80 GW of 4-hour storage, 95% one-way efficiency, and wind and solar capacity scaled by 8x and 4x.

### Scenario Runs

All scenarios in the paper are run with `code/run_all_periods.jl`. Every scenario parameter is encoded in the run name passed on the command line, so no script editing is required to reproduce any configuration used in the paper:

```bash
julia code/run_all_periods.jl <run_name>
```

The run name format is:

```
b{storage_gw}_hrs{duration}_w{wind_scale}_s{solar_scale}_days{simulation_days}_ptc{production_incentive}[_eff{efficiency}][_rc{ramping_charge}]
```

| Field | Meaning | Values used in the paper |
|---|---|---|
| `b{storage_gw}` | Storage capacity, GW | 0, 20, 40 |
| `hrs{duration}` | Storage duration, hours | 4 |
| `w{wind_scale}` / `s{solar_scale}` | Wind / solar capacity multiplier | `w1_s1` = 15% VRE, `w3_s3` = 40%, `w5_s5` = 65%, `w7_s7` = 80% |
| `days{simulation_days}` | Length of each simulation window, days | 4 |
| `ptc{incentive}` | Renewable production incentive, \$/MW | 0, 5, 10 |
| `eff{efficiency}` *(optional)* | Storage one-way efficiency, % | 85, 98 (default: 95 if omitted) |
| `rc{charge}` *(optional)* | Ramping charge on strategic storage, \$/MW | `rc01` = 0.1, `rc05` = 0.5, `rc1` = 1.0, `rc2` = 2.0 (default: off if omitted) |

Example:

```bash
julia code/run_all_periods.jl b20_hrs4_w3_s3_days4_ptc10_eff85
```

runs 20 GW storage, 4-hour duration, 40% VRE, a \$10/MWh incentive, and 85% efficiency, with no ramping charge.

### Model Outputs

Each run writes to `result/<result_name>/`, where the folder name is derived from the run name, e.g. `b20_hrs4_w3_s3_days4_ptc10` becomes `tscc_all_weeks_3w_3s_20b_4hrs_-10ptc_4days`. Optional `eff`/`rc` tokens are appended at the end.

| File | Content |
|---|---|
| `hourly_dispatch_central.csv` | Hourly dispatch, net demand, and prices under Central Control, all windows concatenated |
| `hourly_dispatch_strategic.csv` | Same for Strategic Storage, plus the storage's charge and discharge price offers |
| `summary_system.csv` | System cost, storage profit, average price, true generation cost without the incentive, and negative-price statistics for both regimes (and ramping charge totals if applicable) |
| `summary_weekly.csv` | Status, MIP gap, system cost, storage profit, and average price for each window |
| `params.csv` | Key simulation parameters |
| `params_cap_mix.csv` | Installed capacity by resource type |
| `generation/` | Per-generator output of each window (`iso_gen_<n>.csv`, `bi_gen_<n>.csv`) |
| `figure/` | Generation mix, storage dispatch, price, and state-of-energy plots of each window (`*.pdf`) |

### Batch Processing

For running the full multi-period simulation, run directly on a local machine or submit as a batch job on an HPC cluster:

```bash
# Run directly on a local machine
julia code/run_all_periods.jl <run_name>

# Submit a batch job on an HPC cluster
cd batch
sbatch <run_name>.sh
```

### Overview of All Scenarios

The table below lists every model configuration used to produce the paper's results. Each row gives the run command and the resulting output folder under `result/`. The last column shows which tables and figures each scenario feeds: to reproduce a table or figure, run every scenario that lists it. Scenarios not used by any table or figure are marked "Supplementary" — these were run as part of the broader sensitivity analysis but are not individually referenced in the published text.

#### Baseline scenarios — VRE share × renewable incentive (20 GW, 95% efficiency, no ramping charge)

| VRE share | PTC (\$/MW) | Run command | Output folder | Paper reference |
|---|---|---|---|---|
| 15% | 0 | `julia code/run_all_periods.jl b20_hrs4_w1_s1_days4_ptc0` | `tscc_all_weeks_1w_1s_20b_4hrs_0ptc_4days` | Table 4 |
| 15% | 5 | `julia code/run_all_periods.jl b20_hrs4_w1_s1_days4_ptc5` | `tscc_all_weeks_1w_1s_20b_4hrs_-5ptc_4days` | Table 4 |
| 15% | 10 | `julia code/run_all_periods.jl b20_hrs4_w1_s1_days4_ptc10` | `tscc_all_weeks_1w_1s_20b_4hrs_-10ptc_4days` | Table 4 |
| 40% | 0 | `julia code/run_all_periods.jl b20_hrs4_w3_s3_days4_ptc0` | `tscc_all_weeks_3w_3s_20b_4hrs_0ptc_4days` | Table 2, Figure 5, Figure 6 |
| 40% | 5 | `julia code/run_all_periods.jl b20_hrs4_w3_s3_days4_ptc5` | `tscc_all_weeks_3w_3s_20b_4hrs_-5ptc_4days` | Table 4 |
| 40% | 10 | `julia code/run_all_periods.jl b20_hrs4_w3_s3_days4_ptc10` | `tscc_all_weeks_3w_3s_20b_4hrs_-10ptc_4days` | Table 2, Table 4, Figure 3, Figure 4, Figure 5, Figure 6 |
| 65% | 0 | `julia code/run_all_periods.jl b20_hrs4_w5_s5_days4_ptc0` | `tscc_all_weeks_5w_5s_20b_4hrs_0ptc_4days` | Supplementary |
| 65% | 5 | `julia code/run_all_periods.jl b20_hrs4_w5_s5_days4_ptc5` | `tscc_all_weeks_5w_5s_20b_4hrs_-5ptc_4days` | Table 4 |
| 65% | 10 | `julia code/run_all_periods.jl b20_hrs4_w5_s5_days4_ptc10` | `tscc_all_weeks_5w_5s_20b_4hrs_-10ptc_4days` | Table 4 |
| 80% | 0 | `julia code/run_all_periods.jl b20_hrs4_w7_s7_days4_ptc0` | `tscc_all_weeks_7w_7s_20b_4hrs_0ptc_4days` | Table 2, Table 4, Figure 5, Figure 6 |
| 80% | 5 | `julia code/run_all_periods.jl b20_hrs4_w7_s7_days4_ptc5` | `tscc_all_weeks_7w_7s_20b_4hrs_-5ptc_4days` | Table 4 |
| 80% | 10 | `julia code/run_all_periods.jl b20_hrs4_w7_s7_days4_ptc10` | `tscc_all_weeks_7w_7s_20b_4hrs_-10ptc_4days` | Table 2, Table 4, Figure 3, Figure 5, Figure 6 |

#### No Storage benchmark (0 GW)

| VRE share | PTC (\$/MW) | Run command | Output folder | Paper reference |
|---|---|---|---|---|
| 40% | 10 | `julia code/run_all_periods.jl b0_hrs4_w3_s3_days4_ptc10` | `tscc_all_weeks_3w_3s_0b_4hrs_-10ptc_4days` | Table 2, Figure 3 |
| 80% | 10 | `julia code/run_all_periods.jl b0_hrs4_w7_s7_days4_ptc10` | `tscc_all_weeks_7w_7s_0b_4hrs_-10ptc_4days` | Table 2, Figure 3 |

#### Table 5 — Storage capacity sensitivity (40 GW, 95% efficiency, no ramping charge)

| VRE share | PTC (\$/MW) | Run command | Output folder | Paper reference |
|---|---|---|---|---|
| 15% | 10 | `julia code/run_all_periods.jl b40_hrs4_w1_s1_days4_ptc10` | `tscc_all_weeks_1w_1s_40b_4hrs_-10ptc_4days` | Supplementary |
| 40% | 0 | `julia code/run_all_periods.jl b40_hrs4_w3_s3_days4_ptc0` | `tscc_all_weeks_3w_3s_40b_4hrs_0ptc_4days` | Table 5 |
| 40% | 10 | `julia code/run_all_periods.jl b40_hrs4_w3_s3_days4_ptc10` | `tscc_all_weeks_3w_3s_40b_4hrs_-10ptc_4days` | Table 5 |
| 65% | 0 | `julia code/run_all_periods.jl b40_hrs4_w5_s5_days4_ptc0` | `tscc_all_weeks_5w_5s_40b_4hrs_0ptc_4days` | Supplementary |
| 65% | 10 | `julia code/run_all_periods.jl b40_hrs4_w5_s5_days4_ptc10` | `tscc_all_weeks_5w_5s_40b_4hrs_-10ptc_4days` | Supplementary |
| 80% | 0 | `julia code/run_all_periods.jl b40_hrs4_w7_s7_days4_ptc0` | `tscc_all_weeks_7w_7s_40b_4hrs_0ptc_4days` | Table 5 |
| 80% | 10 | `julia code/run_all_periods.jl b40_hrs4_w7_s7_days4_ptc10` | `tscc_all_weeks_7w_7s_40b_4hrs_-10ptc_4days` | Table 5 |

#### Table 5 — Storage efficiency sensitivity (20 GW, no ramping charge)

| VRE share | PTC (\$/MW) | Efficiency | Run command | Output folder | Paper reference |
|---|---|---|---|---|---|
| 40% | 0 | 85% | `julia code/run_all_periods.jl b20_hrs4_w3_s3_days4_ptc0_eff85` | `tscc_all_weeks_3w_3s_20b_4hrs_0ptc_4days_eff85` | Supplementary |
| 40% | 0 | 98% | `julia code/run_all_periods.jl b20_hrs4_w3_s3_days4_ptc0_eff98` | `tscc_all_weeks_3w_3s_20b_4hrs_0ptc_4days_eff98` | Supplementary |
| 40% | 10 | 85% | `julia code/run_all_periods.jl b20_hrs4_w3_s3_days4_ptc10_eff85` | `tscc_all_weeks_3w_3s_20b_4hrs_-10ptc_4days_eff85` | Table 5 |
| 40% | 10 | 98% | `julia code/run_all_periods.jl b20_hrs4_w3_s3_days4_ptc10_eff98` | `tscc_all_weeks_3w_3s_20b_4hrs_-10ptc_4days_eff98` | Table 5 |
| 80% | 0 | 85% | `julia code/run_all_periods.jl b20_hrs4_w7_s7_days4_ptc0_eff85` | `tscc_all_weeks_7w_7s_20b_4hrs_0ptc_4days_eff85` | Supplementary |
| 80% | 0 | 98% | `julia code/run_all_periods.jl b20_hrs4_w7_s7_days4_ptc0_eff98` | `tscc_all_weeks_7w_7s_20b_4hrs_0ptc_4days_eff98` | Supplementary |
| 80% | 10 | 85% | `julia code/run_all_periods.jl b20_hrs4_w7_s7_days4_ptc10_eff85` | `tscc_all_weeks_7w_7s_20b_4hrs_-10ptc_4days_eff85` | Table 5 |
| 80% | 10 | 98% | `julia code/run_all_periods.jl b20_hrs4_w7_s7_days4_ptc10_eff98` | `tscc_all_weeks_7w_7s_20b_4hrs_-10ptc_4days_eff98` | Table 5 |

#### Figure 6 — Ramping charge sensitivity (20 GW, 95% efficiency)

| VRE share | PTC (\$/MW) | Ramping charge (\$/MW) | Run command | Output folder | Paper reference |
|---|---|---|---|---|---|
| 40% | 0 | 0.1 | `julia code/run_all_periods.jl b20_hrs4_w3_s3_days4_ptc0_rc01` | `tscc_all_weeks_3w_3s_20b_4hrs_0ptc_4days_rc01` | Figure 6 |
| 40% | 0 | 0.5 | `julia code/run_all_periods.jl b20_hrs4_w3_s3_days4_ptc0_rc05` | `tscc_all_weeks_3w_3s_20b_4hrs_0ptc_4days_rc05` | Figure 6 |
| 40% | 0 | 1.0 | `julia code/run_all_periods.jl b20_hrs4_w3_s3_days4_ptc0_rc1` | `tscc_all_weeks_3w_3s_20b_4hrs_0ptc_4days_rc1` | Figure 6 |
| 40% | 0 | 2.0 | `julia code/run_all_periods.jl b20_hrs4_w3_s3_days4_ptc0_rc2` | `tscc_all_weeks_3w_3s_20b_4hrs_0ptc_4days_rc2` | Figure 6 |
| 40% | 10 | 0.1 | `julia code/run_all_periods.jl b20_hrs4_w3_s3_days4_ptc10_rc01` | `tscc_all_weeks_3w_3s_20b_4hrs_-10ptc_4days_rc01` | Figure 6 |
| 40% | 10 | 0.5 | `julia code/run_all_periods.jl b20_hrs4_w3_s3_days4_ptc10_rc05` | `tscc_all_weeks_3w_3s_20b_4hrs_-10ptc_4days_rc05` | Figure 6 |
| 40% | 10 | 1.0 | `julia code/run_all_periods.jl b20_hrs4_w3_s3_days4_ptc10_rc1` | `tscc_all_weeks_3w_3s_20b_4hrs_-10ptc_4days_rc1` | Figure 6 |
| 40% | 10 | 2.0 | `julia code/run_all_periods.jl b20_hrs4_w3_s3_days4_ptc10_rc2` | `tscc_all_weeks_3w_3s_20b_4hrs_-10ptc_4days_rc2` | Figure 6 |
| 80% | 0 | 0.1 | `julia code/run_all_periods.jl b20_hrs4_w7_s7_days4_ptc0_rc01` | `tscc_all_weeks_7w_7s_20b_4hrs_0ptc_4days_rc01` | Figure 6 |
| 80% | 0 | 0.5 | `julia code/run_all_periods.jl b20_hrs4_w7_s7_days4_ptc0_rc05` | `tscc_all_weeks_7w_7s_20b_4hrs_0ptc_4days_rc05` | Figure 6 |
| 80% | 0 | 1.0 | `julia code/run_all_periods.jl b20_hrs4_w7_s7_days4_ptc0_rc1` | `tscc_all_weeks_7w_7s_20b_4hrs_0ptc_4days_rc1` | Figure 6 |
| 80% | 0 | 2.0 | `julia code/run_all_periods.jl b20_hrs4_w7_s7_days4_ptc0_rc2` | `tscc_all_weeks_7w_7s_20b_4hrs_0ptc_4days_rc2` | Figure 6 |
| 80% | 10 | 0.1 | `julia code/run_all_periods.jl b20_hrs4_w7_s7_days4_ptc10_rc01` | `tscc_all_weeks_7w_7s_20b_4hrs_-10ptc_4days_rc01` | Figure 6 |
| 80% | 10 | 0.5 | `julia code/run_all_periods.jl b20_hrs4_w7_s7_days4_ptc10_rc05` | `tscc_all_weeks_7w_7s_20b_4hrs_-10ptc_4days_rc05` | Figure 6 |
| 80% | 10 | 1.0 | `julia code/run_all_periods.jl b20_hrs4_w7_s7_days4_ptc10_rc1` | `tscc_all_weeks_7w_7s_20b_4hrs_-10ptc_4days_rc1` | Figure 6 |
| 80% | 10 | 2.0 | `julia code/run_all_periods.jl b20_hrs4_w7_s7_days4_ptc10_rc2` | `tscc_all_weeks_7w_7s_20b_4hrs_-10ptc_4days_rc2` | Figure 6 |

## Analysis and Figure Reproduction

### Overview of Reproduction Steps

**In scope:**
- Table 1, Table 2, Table 4, and Table 5 (saved as `.csv`)
- Figure 3, Figure 4, Figure 5, and Figure 6 (saved as `.pdf`)

**Out of scope:**
- Table 3 — lists the parameter values used in the sensitivity analysis; not derived from model outputs.
- Figures 1 and 2 — conceptual/framework diagrams, not derived from model outputs.

The scripts in `analysis/` reproduce the tables and figures of the paper in two steps:

1. **Generate summary statistics** — `result_summary.py` condenses the model outputs of all scenarios into one file, `result_summary.csv`.
2. **Generate tables and figures** — one `plot_table*.py` / `plot_figure*.py` script per table or figure reads the summary file and/or the detailed per-scenario outputs.

By default the scripts read the processed model outputs included in this repository (`analysis/result_summary.csv` and `analysis/result_simplified/`), so no model run is needed to reproduce the paper's results. A processed copy of `result_summary.csv` is already included, so Step 1 is only needed if you want to regenerate it (for example after running new scenarios).

Make sure the [Python dependencies](#python-analysis) are installed. Every script writes the result to `analysis/output/`. All commands below are run from the `analysis/` directory:

```bash
cd analysis
```

### Generate Summary Statistics

Tables 2, 4, 5 and Figures 5, 6 read `result_summary.csv`, which holds one row per scenario and regime (system cost, storage profit, average price, negative-price statistics, etc.).

To regenerate `result_summary.csv` from the processed outputs included in this repository:

```bash
python result_summary.py --root result_simplified --out result_summary.csv
```

If you have run new scenarios (raw output is under `result/` at the repository root), summarize them into a separate file:

```bash
python result_summary.py --root ../result --out result_summary_new.csv
```

### Generate Tables and Figures

Run each script below. Together, they reproduce every table and figure in scope.

| Step | Command | Reproduces | Output |
|---|---|---|---|
| 1 | `python plot_table1.py` | Table 1: capacity mix in the WECC region | `output/table1.csv` |
| 2 | `python plot_table2.py` | Table 2: system costs and storage profits | `output/table2.csv` |
| 3 | `python plot_table4.py` | Table 4: market outcomes by VRE share and incentive | `output/table4.csv` |
| 4 | `python plot_table5.py` | Table 5: storage capacity and efficiency sensitivity | `output/table5.csv` |
| 5 | `python plot_figure3.py` | Figure 3: price duration curves | `output/figure3.pdf` |
| 6 | `python plot_figure4.py` | Figure 4: storage dispatch and prices in a representative period | `output/figure4.pdf` |
| 7 | `python plot_figure5.py` | Figure 5: supplier surplus by resource type | `output/figure5.pdf` |
| 8 | `python plot_figure6.py` | Figure 6: impacts of ramping charges | `output/figure6.pdf` |

## Data Sources

The project uses WECC (Western Electricity Coordinating Council) system data from [PowerGenome](https://github.com/PowerGenome/PowerGenome), an open-source tool for building input files for power system models. Data includes:
- Generator parameters and costs
- Load profiles
- Variable renewable energy capacity factors
- Fuel prices

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Maintainer

**Zhenhua Zhang**  
Email: zhenhua@ucsd.edu  
Affiliation: University of California, San Diego