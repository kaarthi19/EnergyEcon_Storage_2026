# Algorithm Description

This document covers submission item 7(b) of the EMA BESS Bidding Algorithm Hackathon Call for Participation: the bidding strategy, methodology, key assumptions, how BESS operating constraints are handled, and which parts are automated.

## 1. Problem

The algorithm bids a hypothetical **100 MW / 100 MWh BESS** with an operating **SOC range of 5–95 %** (5–95 MWh) and **85 % round-trip efficiency** into the Singapore Wholesale Electricity Market (SWEM). For every half-hourly trading period it produces price–quantity offers for four products:

- **Energy**: charging is negative MW, discharging is positive MW.
- **Regulation**
- **Primary Reserve**
- **Contingency Reserve**

The objective is to maximise total net market revenue while staying within the battery limits and market rules.

## 2. Overview

```
 market data ──► price forecast ──► energy+reserve co-optimisation ──► parametric price sweep
 (≤ cut-off)      (ridge / pre-       over a 48 h look-ahead             per product → monotone
                  dispatch / naive)   (LP, HiGHS)                        offer curves
                                                                              │
                         actual SOC ◄── market clearing ◄── SOC safety guard ◄┘
                         (feedback)      (EMA / local simulator)
```

In **rolling mode** (the default), the algorithm re-runs each trading period. It uses the latest data and the actual SOC, and produces the offer for the period `lead_periods` ahead. **Day-ahead mode** produces all 48 offers of a trading day at once.

## 3. Price forecasting (`src/forecast.jl`)

- **Information set.** At decision origin τ, only realised prices up to τ are used, plus pre-dispatch values for periods within `pd_horizon_periods` of τ. The forecaster never reads later rows, so the EMA data cut-off is respected by construction. A unit test corrupts all future prices and checks that the forecasts do not change.
- **Ridge regression** (default). One model per product, refitted at every decision. Features:
  - pre-dispatch price where available, otherwise a 7-day same-period profile, with an availability indicator;
  - the last known price for the same trading period;
  - the 7-day same-period mean;
  - a persistence term that decays with lead time;
  - time-of-day harmonics;
  - a weekend flag.

  Training samples are forecast origins spread through the previous `train_days`, each forecasting the full look-ahead, so the model learns lead-dependent accuracy.
- **Alternatives** (`forecast.method`): `predispatch` uses the pre-dispatch price where available and falls back to seasonal naive. `seasonal_naive` uses the same period on the last known day.
- **Uncertainty.** In-sample residuals give an empirical forecast distribution per product. Its quantiles set the price grid used to build the offer curves (§5).

## 4. Co-optimisation (`src/optimizer.jl`)

The model is a linear program over the look-ahead window (default 96 periods = 48 h), solved with HiGHS. The battery physics come from `code/bilevel_cvx.jl` in this repository and are extended for this market.

**Variables** (per period j): charge `c`, discharge `d`, regulation `r_reg`, primary `r_pri`, contingency `r_con` (all MW), and SOC (MWh).

**Constraints**, with Δt = 0.5 h, η = √0.85 (one-way), P = 100 MW, [E_min, E_max] = [5, 95] MWh, and S_p = sustain hours of product p:

| Constraint | Form |
|---|---|
| SOC dynamics with expected reserve deployment | `soc_j = soc_{j-1} + Δt(η c_j − d_j/η) + Σ_p δ_p r_p,j` |
| SOC limits | `E_min ≤ soc_j ≤ E_max` |
| Raise headroom | `d_j − c_j + r_reg + r_pri + r_con ≤ P` |
| Lower headroom | `c_j − d_j + r_reg ≤ P` |
| Raise energy sustainability | `soc_{j-1} − (d_j Δt + Σ_p r_p S_p)/η ≥ E_min` |
| Lower energy sustainability | `soc_{j-1} + η(c_j Δt + r_reg S_reg) ≤ E_max` |
| Charge/discharge | `c_j + d_j ≤ P`. With `use_binary = true`, a binary forbids simultaneous charge and discharge (MILP), as in `bilevel_cvx.jl`. |
| End of window | soft: stored energy valued at η × median forecast energy price; or hard: `soc_H ≥ target` |

The expected deployment coefficients δ_p come from the assumed deployment fractions (`[market.deployment]`). Regulation is modelled as symmetric raise/lower.

**Objective:**

    max Σ_j Δt [ λ^E_j (d_j − c_j) + Σ_p λ^p_j r_p,j − κ (c_j + d_j) ] + terminal value

Here κ is a degradation cost per MWh of throughput. It is a strategy parameter that suppresses low-margin cycling and is not part of market revenue.

**Price-maker option** (`price_impact > 0`). This adapts the bilevel idea in `code/bilevel_cvx.jl`. That model derived prices from the KKT conditions of a full economic dispatch. EMA supplies prices rather than a supply stack, so the lower level is replaced by a linear residual-supply approximation λ(x) = λ̂ − k·x. This adds `−Δt·k·(d − c)²` to the objective, which makes the problem a convex QP that HiGHS solves without binaries.

## 5. From schedule to offers (`src/strategy.jl`, `src/bids.jl`)

A schedule says what to do at one price. An offer must say what to do at every price. For the bid period, the algorithm sweeps each product's price over a grid:

- the grid is the forecast quantiles (energy: 2 %–98 %; reserves: 5 %–95 %), plus the price floor (energy) or 0 (reserves), plus the price cap;
- other products stay at their forecast;
- the LP is re-solved for each grid price, and the optimal first-period quantity is recorded: x(λ) for energy, r(λ) for reserves.

For an LP, x(λ) is non-decreasing in its own price (parametric LP monotonicity), so the result is a valid monotone supply curve. Each jump is then **bisected** until the switching price is known to about $1 (or 1 %). That switching price is the battery's **opportunity cost** for that MW, because it accounts for the whole look-ahead window. Small MILP inversions are repaired by isotonic correction. Tranches are merged down to the allowed maximum (`max_energy_tranches`, `max_reserve_tranches`).

**Energy offer layout** (`offers_table`): one curve over net MW from the most-charging position to the most-discharging. The quantity cleared at price λ is `base + Σ tranches with price ≤ λ`. Rows with `mw_to ≤ 0` are charging (negative MW). Rows with `mw_from ≥ 0` are discharging (positive MW). A tranche that crosses 0 MW is split. This is the assumed reading of RC383 §3.1.2 and **must be checked against the paper** before submission (§8).

## 6. BESS operating limits and market rules

- **SOC safety guard** (`guard_soc`). After the offer curves are built, the algorithm checks the worst case over every combination the market could clear. The SWEM market clearing engine co-optimises energy and reserves under the unit's joint capability, so that combination is bounded by P as well as by the offers. If the worst case could breach the SOC limits or the reserve sustainability requirement from the bid period's starting SOC, quantities are trimmed in this order:
  1. quantities above the forecast-optimal plan (reserves, then energy);
  2. then the planned quantities themselves.

  A unit test clears 1,400 random price draws against guarded offers from SOC = 5 … 95 MWh and finds no violation.
- **Open offers.** When earlier offers are still unsettled (`lead_periods > 1`, or day-ahead mode), the starting SOC is an interval, and it is propagated through those offers.
  - `soc_guard = "worst_case"` guarantees compliance but gives up revenue.
  - `soc_guard = "expected"` uses the expected SOC path ± `soc_margin_mwh`. It earns more but can produce violations.
- **Offer validation** (`validate_bids`) checks that:
  - prices are ascending and within the floor and cap;
  - the tranche count is within limits;
  - energy stays within ±P and reserves within [0, P];
  - all four products are present.

## 7. Backtesting (`src/backtest.jl`, `src/simulator.jl`)

The backtest runs closed-loop on historical or synthetic data:

1. bid using only information available at each decision time;
2. clear the offers against realised prices;
3. settle the outcome;
4. feed the actual SOC back into the next decision.

The local clearing model is a price-taker, co-optimised award. It maximises the battery's offer surplus under the joint-capability constraints, which is the dispatch that SWEM-style co-optimised prices support. EMA's own simulator is not public and may differ.

The summary reports:
- net market revenue by product;
- shortfall penalties and sustain-violation periods;
- equivalent full cycles;
- the SOC range;
- a **perfect-foresight** upper bound (the same LP with realised prices) and the **capture ratio** against it.

## 8. Key assumptions to confirm when EMA releases the data

| Item | Current setting | Where |
|---|---|---|
| Energy offer layout and sign convention (RC383 §3.1.2) | single monotone curve, charging negative | `offers_table` |
| Offer price floor / cap | −4,500 / 4,500 $/MWh | `[market]` |
| Max tranches | 10 energy, 5 per reserve | `[market]` |
| Regulation product | symmetric raise/lower | `optimizer.jl` |
| Reserve deployment energy | 10 % / 10 % / 2 % / 1 % | `[market.deployment]` |
| Sustain duration | 0.5 h for all reserves | `[market.sustain_hours]` |
| Gate closure / lead time | 1 period | `strategy.lead_periods` |
| Pre-dispatch horizon | 48 periods | `forecast.pd_horizon_periods` |
| Initial SOC | 50 % | `battery.initial_soc_frac` |
| Data schema | canonical names | `[data]`, `[data.columns]` |

## 9. Automation and human intervention

**Fully automated:**
- data loading (after the one-off column mapping);
- forecasting;
- optimisation;
- offer construction;
- SOC guard and validation;
- export;
- backtesting.

There are no manual steps per trading period.

**Needs a person:**
- one-off: mapping EMA's column names in `[data]` and confirming the market-rule assumptions in §8;
- periodic: tuning strategy parameters (degradation cost, horizon, grid quantiles, forecast method) on backtests;
- exceptional: reviewing outcomes after market events outside the training data (e.g. administered pricing), where forecasts may be unreliable.
