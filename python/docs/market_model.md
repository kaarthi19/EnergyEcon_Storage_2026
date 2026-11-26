# Market engine, backtest and price forecasts

The equations of the clearing LP are in [`formulation.md`](formulation.md) (Eq. (1)-(11)).

## Market engine and backtest

### `engine.py`: exact replica of EMA's clearing

One LP per 30-minute period, solved with HiGHS (`highspy`) directly:

- It minimises offer cost.
- Energy balance: generation + unserved + BESS discharge - BESS charge = `net_demand_mw`.
  The energy price is the dual of this row.
- Reserve rows: offered + shortfall >= requirement. Shortfalls are uncapped and priced at 750
  (P), 1000 (C) and 1200 (R). VoLL is 4500, an assumption, since energy was never unserved.
- Per unit: e + p + c + r <= `available_mw`. Reserve caps are the reserve `offered_mw`.
- Energy ramp-up: e(t) <= e(t-1) + 0.42 x `available_mw`(t). It is applied by clipping bands
  in merit order, which is the same LP as an explicit ramp row.
- Inputs must be finite: a NaN or infinite stack value, net demand, requirement or BESS
  band raises `EngineError`, as does any model update HiGHS rejects (a NaN used to be
  ignored silently, leaving the previous period's bounds in place).

**BESS offers** enter as RC383 bands:
- discharge (MW > 0) is supply at its offer price;
- charge (MW < 0) is a demand bid, cleared when the price is below the offer;
- reserve bands are supply at their price.

The engine also applies two physical joint rows: discharge + P + C + R <= 100 and
charge + R <= 100 (`bess_pmax`). The bid builders separately enforce the stricter
offered-MW rule (|energy| + all AS <= 100). For our own bids, therefore, these rows never
bind.

**Price-taker awards.** `price_taker_clear(offer, prices, tie=)` awards the bands that are in
the money. With the default `tie='strict'` (RC383 wording), a band offered exactly at the
price does not clear; `'inclusive'` clears it. If the joint rows would be exceeded, MW go to
the largest margins first.

**Speed.** One model per (facilities, bands) shape is reused: each call changes bounds,
costs and row bounds and warm-starts the simplex. A solve takes about 0.4 ms. With
`warm_start=False` the results do not depend on solve history.

**Replication** (`replicate(stacks, clearing)`): all 5904 training periods, Jul 1 - Oct 31,
cold start Jul 1, ramp chained as EMA did. Runtime is 2.3 s.

| Price | MAE | Max error |
|---|---|---|
| E | 0.0003 | 0.0019 |
| P | 0.0003 | 0.0020 |
| C | 0.0003 | 0.0020 |
| R | 0.0004 | 0.0018 |

Unmet reserve MW agree to within 0.002. BESS insertion matches the Phase A engine to 1e-12.

### `backtest.py`: EMA-replica evaluator

```python
from bessbid import io
from bessbid.backtest import MarketData, simulate, run_months
md = MarketData.from_dataset(io.load_all(data_dir, work_dir), start="2026-07-01")
r = simulate(bid_table, "2026-10", md, mode="maker", convention="sqrt", soc_policy="clip")
r.frame     # one row per period: prices, awards, revenue by product, SOC (+ per convention)
r.summary   # totals, revenue per 30 days, SOC min/max/final, breaches, clips, rejections
```

**Modes.**
- `maker`: our offers inside the LP, with actual stacks, net demand and requirements. The
  ramp state is chained from a cold start on Jul 1. `ramp='baseline'` instead chains the
  no-BESS dispatch (the Phase A convention).
- `taker`: actual training prices, with `price_taker_clear` awards.

**SOC.** SOC starts at 50 MWh and moves by 0.5 h x (eta_c x charge - discharge / eta_d).
`convention` is `'sqrt'`, `'charge'`, `'discharge'` or an (eta_c, eta_d) pair. When a period
would leave 5-95 MWh, `soc_policy` decides what happens:
- `none`: report the breach;
- `clip`: trim the energy offer to what SOC allows, before clearing;
- `reject`: drop that period's energy offer and keep its reserves.

Every run also reports the SOC path of the realised dispatch under all three conventions.

**Settlement.**
- Energy: price x (discharge - charge) x 0.5 h, so charging costs money.
- Reserves: price x MW x 0.5 h.

**Golden numbers** (regulation-only, 100 MW at 0.01 (identical at 0.00), sqrt convention):

| Window | Maker (re-cleared) | Taker (actual prices) |
|---|---|---|
| October | 2.0582 M per 30 days (2.1268 M for the 31 days) | 2.4062 M per 30 days |
| Jul-Oct | 1,415,911 per 30 days | 1,575,608 per 30 days |

These equal the Phase A values exactly. Pseudo-Novembers are Aug, Sep and Oct
(`PSEUDO_MONTHS`). A whole month runs in about 2.5 s as maker (of which about 1.8 s is the
ramp warm-up from Jul 1) and about 0.1 s as taker.

```bash
python -m bessbid backtest --data-dir D --work-dir W --strategy regulation \
    --month 2026-08 2026-09 2026-10 --mode both --convention all --soc-policy clip
python -m bessbid backtest --data-dir D --work-dir W --bids some_october_bids.xlsx --month 2026-10
```

The summary goes to stdout and to `<work>/backtest_<label>.json`.

## Price forecasts

Prices are forecast structurally, the way EMA clears them: a forecast offer stack, forecast
net demand and the event calendar go through the clearing LP (`engine.py`, cold-started so a
forecast never depends on earlier solves).

- **Calendar** (`calendar.py`). Learned at a cut from the known-outage state in
  pre-dispatch rows issued by then: outage onsets are explained greedily by periodic cycles
  (time of day, period in days, anchor); each cycle's duration is the candidate that best
  reproduces the observed states and its event MW is the median onset jump. The raised
  contingency requirement and the demand surge are windows relative to a cycle, learned from
  training clearing rows that ended by the cut. Nothing is hard-coded. On EMA's data the
  calendar learned from data up to Jul 28 already classifies every training and November
  known-outage state above 400 MW with 0 exceptions.
- **Net demand** (`netdemand.py`). Lead forecast: T's own lead-2 h vintage, else lead 3 h,
  `load - solar` with forecast solar set to zero in periods of the day where training solar
  was always zero. Day-ahead: mean of the same period over the last 4 days of the same day
  type, outside surge windows, plus the learned surge. Under the strict reading
  (`Config.strict_predispatch`) the history is training actual net demand; otherwise it is
  the lead-0.5 h vintages of past periods. The hour-of-day bias correction exists
  (`bias_correction=True`) but is off: it made lead-2 h error slightly worse.
- **Stack** (`stackmodel.py`). log price = base(unit, band, period of day) + factor; the
  factor is regressed on stack outage MW and weekend. The forecast for T blends the 7 most
  recent published stacks for T's period of day (publication per row, so lags of 29+ days),
  normalised to zero outage and weekday, rescaled to T's expected outage and day type, with
  outaged units restored. Stack periods that are exact copies of a published period 28 days
  later (EMA's June files) are left out of the fit.
- **Forecasts** (`forecast.py`). `lead2(T)`: decision `T - 65 min`; expected outage = the
  vintage's known outage moved forward with the calendar. `day_ahead(day)`: decision 00:00
  the day before (any decision time can be given). `scenarios(...)`: net-demand error nodes
  (and event MW x 0.87/1/1.13 day-ahead in event windows) through the LP, times whole-day
  empirical log-residual blocks of the same regime from a walk-forward residual library;
  gives means, quantiles, P(spike) and price paths. Every forecast checks its decision time
  against the fit cut and writes an audit entry.

Walk-forward accuracy, Aug-Oct, monthly refits at 00:00 the day before each month (MAE in
SGD/MWh; spike = price > 400):

| | Energy MAE | Primary | Contingency | Regulation | Energy spike precision / recall |
|---|---|---|---|---|---|
| lead 2 h | 18.8 | 1.27 | 1.80 | 2.27 | 0.74 / 0.97 |
| day-ahead, strict reading | 23.1 | 2.45 | 3.08 | 3.55 | 0.70 / 0.93 |
| day-ahead, broad reading | 22.1 | 1.95 | 2.53 | 3.00 | 0.72 / 0.96 |

Scenario ensembles (Sep-Oct, residual library from Aug 1): q10/q50/q90 coverage
0.06-0.10 / 0.42-0.48 / 0.82-0.87; P(energy spike) >= 0.5 has precision/recall 0.78/0.97
(lead 2 h) and 0.64/0.99 (day-ahead).

```bash
# a month of forecasts (lead 2 h, day-ahead, month-ahead, ensembles) into the work dir
python -m bessbid.forecast --data-dir D --work-dir W --month 2026-11
```

The output (`<work>/forecasts_<month>.parquet`, a daily CSV and a JSON summary) is derived
from EMA's data and stays in the work dir.
