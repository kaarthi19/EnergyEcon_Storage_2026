# External Datasets and Software

This document covers submission item 7(c) of the EMA BESS Bidding Algorithm Hackathon Call for Participation.

## External datasets

**None at present.** The algorithm uses only EMA-provided data: realised energy and ancillary-service prices, plus pre-dispatch information.

`src/synthetic.jl` generates synthetic data for development and testing only. It is not used for evaluation.

Any external data added later (e.g. weather forecasts) must be listed here with:
- its source;
- its licence;
- the period it covers, which must respect EMA's data cut-off;
- how it is used.

## Software

All dependencies are open source. No commercial licence or online service is needed, so EMA can run the algorithm independently, as the Call requires.

| Component | Version | Licence | Use |
|---|---|---|---|
| Julia | 1.10 (LTS) | MIT | Language runtime |
| JuMP.jl | 1.32.1 | MPL-2.0 | Optimisation modelling |
| HiGHS.jl / HiGHS | 1.26.1 | MIT | LP / MILP / QP solver |
| DataFrames.jl | 1.8.2 | MIT | Tabular data |
| CSV.jl | 1.3.0 | MIT | CSV input and output |
| Julia standard library (Dates, Statistics, LinearAlgebra, Random, TOML, Printf, Test) | bundled | MIT | Dates, statistics, ridge regression, configuration, tests |

Exact versions of all transitive dependencies are pinned in `Manifest.toml`.

The rest of this repository's paper code (`code/*.jl`) uses Gurobi, a commercial solver. The hackathon package does **not** depend on Gurobi.
