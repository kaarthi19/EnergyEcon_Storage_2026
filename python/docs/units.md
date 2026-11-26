# Units and conventions

| Quantity | Unit | Names in code and outputs | Notes |
|---|---|---|---|
| Power: offers, awards, dispatch, demand, capacity, outages, ramp | MW | `*_mw`, `mw`, `offered_mw`, `energy_mw`, `regulation_mw`, `net_demand_mw`, `bess_ramp_mw` | average over the trading period |
| Energy | MWh | `*_mwh`, `energy_mwh`, `charge_mwh`, `discharge_mwh` | MW × 0.5 h per period |
| State of charge (SOC) | MWh | `soc_*`, `soc_binding`, `soc_margin_mwh`, `soc_min_mwh` | 100 MWh battery, so MWh = % of capacity. `*_frac` keys are fractions of `energy_mwh` |
| Binding SOC Y | MWh | `soc_binding` | the SOC under "all loss on discharge" (η_c = 1), the tightest convention (`docs/formulation.md`, Eq. (15)) |
| Prices: energy, reserves, offers, shortfall, VoLL | S$/MWh | `price`, `E`, `P`, `C`, `R`, `fc_*_E`, `*_price` | Singapore dollars, nominal 2026. Reserve prices are "S$/MWh equivalent", paid per MW per hour |
| Revenue | S$ | `rev`, `rev_E`, `rev_P`, `rev_C`, `rev_R`, `revenue`, `uplift` | per period: price × MW × 0.5 h; `*_per_30d` scales a month to 30 days |
| Trading period | 0.5 h | `period_hours = 0.5`, `period` 1–48 | period 1 = 00:00–00:30 SGT, period k starts at (k − 1) × 30 min |
| Time | naive SGT (UTC+8, no DST) | `ts`, `issue_time`, `pub`, decision times | the input files' +08:00 offsets are parsed and dropped |
| Gate closure | min | `gate_closure_min = 65` | data for a period starting at T must be available by T − 65 min |
| Pre-dispatch lead | h | `lead_hours`, `predispatch_leads` | issue time = T − lead |
| Efficiency | fraction | `round_trip_eff`, `efficiency_conventions` (η_c, η_d) | η_c η_d = 0.85 |

## Sign conventions

- Energy MW: **positive = discharge** (selling), **negative = charge** (buying), as in EMA's
  template and RC383 s3.1.2.
- Energy revenue = price × (discharge − charge) × 0.5 h, so charging costs money.
- Reserve and Regulation MW are always positive.
- SOC moves by 0.5 h × (η_c × charge − discharge / η_d) per period. Reserve and Regulation
  activation is 0 MWh, as EMA specifies.

## Rounding

- Bid MW are rounded **toward zero** to 0.1 MW (`mw_resolution`), so rounding never adds
  capacity.
- Bid prices are whole cents.
- Planner SOC grid: 0.5 MWh (`y_step`).
