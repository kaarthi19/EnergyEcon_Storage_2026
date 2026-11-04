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
| Raise headroom (RC383 D.17.2.4A, D.18.1.6) | `d_j − c_j + r_reg + r_k ≤ P` for each reserve class k ∈ {pri, con}. This is the default `reserve_headroom = "per_class"`, because the rule applies to each reserve offer separately. `"summed"` stacks primary and contingency instead. |
| Lower headroom (RC383 D.18.1.7) | `c_j − d_j + r_reg ≤ P` |
| Raise energy sustainability | `soc_{j-1} − (d_j Δt + r_reg S_reg + max(r_pri S_pri, r_con S_con))/η ≥ E_min`. Under `"summed"`, the max becomes a sum. |
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

**Energy storage offer (RC383).** The energy offer follows Market Rules Ch. 6 §5.2B, introduced by RC383 and effective 12 Aug 2025 (`storage_pairs`, `offers_table`):

- **Pairs.** 1 to 10 price–quantity pairs in increasing price order. Pairs **1–5 charge** (quantity ≤ 0 MW) and **6–10 discharge** (quantity ≥ 0 MW). Every charging price is below every discharging price. Prices have 2 decimals and stay within the Appendix 6J limits.
- **Clearing.** A charging pair clears when the clearing price is *below* its offer price. A discharging pair clears when the clearing price is *above* its offer price (§3.1.2 c–d).
- **Mapping from the internal curve.** The internal net-MW curve maps one-to-one onto these pairs:
  - each charging tranche (p, q) means "stop charging q MW once the price reaches p", i.e. the charging pair (p, −q);
  - the part of the curve that always charges is a charging pair at the price cap;
  - the part that always discharges is a discharging pair at the price floor;
  - a tranche crossing 0 MW is split into a charging pair and a discharging pair, with the discharging price raised by $0.01;
  - each side is merged to at most 5 pairs;
  - an offer with no transfer is a single (slot 6, $0.00, 0 MW) pair (§5.2B.4).
- **No rounding drift.** The algorithm converts the curve to pairs and back before submission, so the offer the simulator clears is exactly the offer exported.
- **Tests.** A unit test replays RC383's worked example (3 charging and 2 discharging pairs, cleared at six prices) and gets the published schedules.
- **Header fields.** The offered capacity (§5.2B.2.5, maximum combined transfer for energy, reserve and regulation) and the ramp rates (§5.2B.2.6) are repeated on each energy row.

**Reserve and regulation offers.** Up to `max_reserve_tranches` incremental tranches, with any unconditional part priced at $0. RC383 does not change the reserve offer format, so this is still an assumption.

## 6. BESS operating limits and market rules

- **SOC safety guard** (`guard_soc`). After the offer curves are built, the algorithm checks the worst case over every combination the market could clear. The SWEM market clearing engine co-optimises energy and reserves under the unit's joint capability, so that combination is bounded by P as well as by the offers. If the worst case could breach the SOC limits or the reserve sustainability requirement from the bid period's starting SOC, quantities are trimmed in this order:
  1. quantities above the forecast-optimal plan (reserves, then energy);
  2. then the planned quantities themselves.

  A unit test clears 1,400 random price draws against guarded offers from SOC = 5 … 95 MWh and finds no violation.
- **Reserve capability while charging.** RC383 §3.2.1 lets an ESS offer reserves of up to twice its rating while charging, subject to PSO standing capability data. `[market.reserve_cap_mw]` accepts caps up to 2P. The default is P.
- **Gate closure and open offers.** Offers close 65 minutes before the dispatch period (RC383, Table 2). When the offer for period t is fixed, periods t−2 and t−1 are still open, so the default is `lead_periods = 3`. The SOC at the start of t is therefore an interval, propagated through the open offers:
  - `"worst_case"` covers every clearing outcome. It is guaranteed but very conservative.
  - `"quantile"` (default, q = 0.999) clears each open offer at every combination of energy-price tails {1−q, mean, q} and reserve-price tails {1−q, q}. It needs several combinations because the market can trade charging against regulation-lower. Violations can only follow prices beyond these tails.
  - `"expected"` uses the expected SOC path ± `soc_margin_mwh`.

  A `soc_guard_buffer_mwh` (0.5 MWh) keeps offers just inside the 5 %/95 % limits.
- **SOC in market clearing.** RC383 does not model SOC in the market clearing engine; RC386 addresses that separately. Feasibility against SOC is therefore the participant's responsibility, which is what this guard provides.
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

## 8. Market rules: confirmed vs. assumed

**Confirmed from RC383** (EMC/RCP/139/2024/RC383, effective 12 Aug 2025):
- energy storage offer structure, sign convention, clearing rule and pair limits (§5.2B, §3.1.2);
- the 0 MW → $0.00 rule;
- the charge and discharge totals within the maximum limits;
- offered capacity and ramp rates in the offer;
- per-class joint capability for reserves (D.17.2.4A);
- the regulation max/min envelope (D.18.1.6/7);
- reserve capability up to 2× rating while charging (§3.2.1);
- 65-minute gate closure (industry comments, Table 2).

**Still assumed:**

| Item | Current setting | Where |
|---|---|---|
| Offer price floor / cap (Appendix 6J) | −4,500 / 4,500 $/MWh | `[market]` |
| Reserve/regulation offer tranche count | 5 | `[market]` |
| Regulation product | symmetric raise/lower | `optimizer.jl` |
| Reserve deployment energy | 10 % / 10 % / 2 % / 1 % | `[market.deployment]` |
| Sustain duration | 0.5 h for all reserves | `[market.sustain_hours]` |
| Ramp rates | 100 MW/min | `[market]` |
| How EMA's simulator applies gate closure | 3-period lead | `strategy.lead_periods` |
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
