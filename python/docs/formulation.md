# Formulation

This note states the two optimisation models in `bessbid`:

1. the **clearing LP** (`bessbid/engine.py`), a replica of EMA's per-period market clearing
   that the backtest and the planner's price forecasts use; and
2. the **bid planner** (`bessbid/planner.py`, `bessbid/soc.py`, `bessbid/strategy_full.py`), a
   dynamic programme over the battery's state of charge that sets the November energy
   schedule.

Equation numbers match the `# Eq. (n)` comments in the code. Units are in
[`units.md`](units.md): MW for power, MWh for energy and SOC, S$/MWh for prices, and a trading
period of h = 0.5 h.

---

## 1. Clearing LP (`engine.py`)

One linear programme per 30-minute trading period t. It was reverse-engineered from the
training data and reproduces all 5904 training periods (Jul 1 – Oct 31) to within
0.002 S$/MWh for all four prices.

### Sets and indices

| Symbol | Meaning |
|---|---|
| f ∈ F | generating facilities in the offer stack (32 in EMA's data) |
| b ∈ B | energy bands of a facility (5 in EMA's data, ascending in price) |
| k ∈ {P, C, R} | reserve products: Primary, Contingency, Regulation |
| j ∈ J_D, J_G, J_k | BESS bands: discharge (MW > 0), charge (MW < 0), reserve k |

### Parameters

| Symbol | Meaning | Source |
|---|---|---|
| Q_fb, p^E_fb | energy band MW and offer price | offer stack (`offered_mw`, price) |
| Q^k_f, p^k_f | reserve offer MW and price of product k | offer stack |
| A_f(t) | available MW of facility f | offer stack (`available_mw`) |
| D_t | net demand (MW) | `net_demand_mw` (training) or forecast |
| Req_k(t) | reserve requirement (MW) | clearing file or learned calendar |
| σ_P, σ_C, σ_R | shortfall prices 750, 1000, 1200 S$/MWh | `Config.shortfall_price_*` |
| VoLL | value of lost load, 4500 S$/MWh (assumption; never reached) | `Config.voll` |
| ρ_g | generator ramp-up fraction, 0.42 per period | `Config.ramp_up_frac` |
| e_f(t−1) | facility energy dispatch of the previous period (ramp state) | previous solve |
| q_j, o_j | BESS band MW and offer price | our bid |
| P_max | BESS rating, 100 MW | `Config.power_mw` |

### Variables (all ≥ 0)

e_fb (energy per facility band), p_f, c_f, r_f (reserves per facility), s_P, s_C, s_R
(reserve shortfalls, uncapped), u (unserved energy), d_j and g_j (BESS discharge and charge
per band), β_kj (BESS reserve per band).

### Objective: minimise offer cost

    min  Σ_fb p^E_fb e_fb + Σ_f (p^P_f p_f + p^C_f c_f + p^R_f r_f)
         + σ_P s_P + σ_C s_C + σ_R s_R + VoLL u
         + Σ_j o_j d_j − Σ_j o_j g_j + Σ_kj o_kj β_kj                                  (1)

A charge band enters with a negative cost: it is a demand bid that clears when the price is
above its offer.

### Constraints

Energy balance (dual λ_E):

    Σ_fb e_fb + u + Σ_j d_j − Σ_j g_j = D_t                                              (2)

Reserve requirement for each k (dual λ_k):

    Σ_f k_f + s_k + Σ_j β_kj ≥ Req_k(t)                                                  (3)

Joint headroom of each facility:

    Σ_b e_fb + p_f + c_f + r_f ≤ A_f(t)                                                  (4)

Generator ramp-up, when a ramp state is given (backtests chain it from a cold start on
Jul 1; forecasts are cold-started):

    Σ_b e_fb ≤ e_f(t−1) + ρ_g A_f(t)                                                     (5)

Equation (5) is applied by clipping each facility's band capacities in merit order
(`_ramp_clip`). Bands are ascending in price, so this gives the same LP as an explicit row.

Offer bounds:

    0 ≤ e_fb ≤ Q_fb,   0 ≤ k_f ≤ Q^k_f,   s_k, u ≥ 0 (uncapped)                          (6)

BESS physical joint rows (discharge side and charge side):

    Σ_j d_j + Σ_kj β_kj ≤ P_max                                                          (7)
    Σ_j g_j + Σ_j β_Rj ≤ P_max                                                           (8)
    0 ≤ d_j, g_j, β_kj ≤ q_j                                                             (9)

The bid builders enforce the stricter offered-MW rule (12) on every bid, so rows (7)–(8)
never bind for our own bids.

### Prices

Clearing prices are the duals of the balance and requirement rows:

    π_E(t) = λ_E of (2),   π_k(t) = λ_k of (3) for k ∈ {P, C, R}                         (10)

### Price-taker awards

When prices are fixed (the price-taker evaluation), `price_taker_clear` awards each band that
is in the money:

    discharge band j:  o_j < π_E;   charge band j:  o_j > π_E;   reserve band:  o_kj < π_k   (11)

With the default `tie='strict'` (RC383 wording), a band offered exactly at the price does not
clear. If rows (7)–(8) would be exceeded, MW go to the largest margins first.

### Solver

HiGHS 1.16.0 through `highspy` (simplex, presolve off, one thread, `random_seed = 0`).
One model is built per (facilities, bands) shape and reused; each call changes costs, bounds
and row bounds. The planner's forecasts use `warm_start=False`, so a forecast never depends
on solve history. Any status other than optimal, after one cold retry, raises `EngineError`,
as does any non-finite input. A solve takes about 0.4 ms; the November plan needs about
340,000 of them.

---

## 2. Bid planner (`planner.py`, `soc.py`, `strategy_full.py`)

The planner chooses, for every period t of the month, a signed energy action x_t (MW;
x > 0 discharges, x < 0 charges). The bid for period t is x_t as one fixed energy band that
always clears (discharge at 0.01, charge at 3000), plus Regulation 100 − |x_t| at 0.01.
Because every band clears, the SOC path is known without feedback from EMA.

### Offered-capacity rule (strict reading)

In every period, for every bid:

    Σ_bands |q^E| + Σ_{k, bands} q^k ≤ P_max = 100 MW                                     (12)

With one energy band and Regulation filling the rest, (12) holds with equality:
|x_t| + (100 − |x_t|) = 100. `bids.check_capacity` enforces (12) for every bid built.

### Action values with price impact

For period t, a decision time τ and forecast inputs (net demand D̂_t, expected outage), the
forecast offer stack is cleared by LP (1)–(10) with our offer inside it, at a ladder of
13 energy levels L = {−100, −75, −50, −35, −20, −10, 0, 10, 20, 35, 50, 75, 100} MW. The
prices are averaged over the forecaster's 9 net-demand error nodes ε_n:

    Ē_t(x) = (1/9) Σ_n π_E(t; D̂_t − ε_n, x),   R̄_t(x) likewise for Regulation,   x ∈ L

and interpolated linearly between ladder levels. The value of action x is

    F_t(x) = h · ( x · Ē_t(x) + (P_max − |x|) · R̄_t(x) )                                 (13)

Averaging over the nodes turns one merit-order step into an expected price curve. Including
our offer in the LP gives the planner our own price impact.

### SOC under every efficiency convention

EMA has not said how the 85 % round-trip loss is split. For a convention (η_c, η_d) with
η_c η_d = η = 0.85, the SOC after period t is

    SOC_t = SOC_{t−1} + h (η_c · chg_t − dis_t / η_d),   chg_t = max(−x_t, 0),  dis_t = max(x_t, 0)   (14)

Write N_t = h Σ_{τ≤t} (chg_τ − dis_τ / η). Then SOC_t = s_0 + η_c N_t for every convention.
SOC is linear in η_c, so the extremes over the three conventions (all loss on charge, square
root split, all loss on discharge) sit at η_c,max = 1. The planner's state is the binding SOC

    Y_t = s_0 + η_c,max · N_t                                                            (15)

and one bound on Y_t covers every convention:

    SOC_min + m ≤ Y_t ≤ SOC_max − m   ⇔   SOC_min + m ≤ SOC_t ≤ SOC_max − m for every convention   (16)

with SOC_min = 5 MWh, SOC_max = 95 MWh and margin m = `soc_margin_mwh` = 2 MWh.

The Y grid has a step of 0.5 MWh. Actions are exact Y steps whose MW land on the 0.1 MW bid
resolution: charge in 2 MW steps, discharge in 1.7 MW steps. So the bid quantities reproduce
the planned path exactly, and `plan_month` re-checks (14) under each convention separately.

### Ramp limit

RC383 D.19.1.1A / D.19.1.2A apply up and down ramp limits to energy storage. The plan keeps

    |x_t − x_{t−1}| ≤ ρ,   x_0 = 0 (the month starts from an idle schedule)              (17)

with ρ = `bess_ramp_mw` = 42 MW (assumption: the generators' 0.42 × 100 MW per period).
ρ = 0 switches the limit off.

### Size caps and bans

    −c_cap ≤ x_t ≤ d_cap(t),   d_cap(t) = 100 MW in event windows, else 50 MW,   c_cap = 50 MW   (18)

    x_t ≥ 0   if t is in a calendar event window (11-day or 19-day cycle),
              or if the forecast capacity margin at t is below 300 MW                    (19)

The margin is nominal capacity − (net demand + outage) − reserve requirements.

**Optional overnight recharge exception** (`event_recharge`, default `"none"`, so (19) holds
as written). With `"overnight"`, the event part of (19) is lifted in period t if

    t ∈ W_19 \ W_11,   hour(t) ∈ [h_0, h_1),   Ê_t(0) ≤ p_rech                              (19a)

where W_19 and W_11 are the windows of the cycle that carries the surge window (19-day) and
of the other cycles (11-day), [h_0, h_1) = `event_recharge_hours` = [0, 7) by period start,
Ê_t(0) is the forecast energy price at x = 0 of the ladder the programme is solving with
(month-ahead or day-ahead, so it is legal at that decision time), and p_rech =
`event_recharge_max_price` = 350 S$/MWh. `"overnight_all"` drops the "\ W_11". The margin
ban of (19) still applies. A charge allowed by (19a) costs an extra hurdle

    F_t(a) ← F_t(a) − κ · h · max(−x_a, 0)   for t eligible under (19a)                     (13a)

with κ = `event_recharge_hurdle` (default 0), and the guard (23) cancels it if the lead-2 h
price is above min(400, p_rech).

**Optional event-window bias correction** (`event_bias`, default `"none"`). The day-ahead
energy forecast is biased low inside the 19-day surge window and slightly high elsewhere in
event windows ([`strategy.md`](strategy.md)). With `"residual"`, every ladder (month-ahead,
day-ahead and, with `refine = "lead2"`, lead-2 h) is shifted by a bucket correction δ_t at
every level before the values (13) are formed:

    Ē_t(x) ← Ē_t(x) + δ_{b(t)},   δ_b = n_b / (n_b + k) · mean_{s ∈ S_b} clip(π_E(s) − Ê_s, ±c)     (13b)

Here b(t) is the bucket of period t: the event cycle whose window holds t, the calendar day
of t counted from the event's onset day (0, 1, 2), whether t is in the learned surge window,
and the `event_bias_hours` (3 h) block of t's start time; δ = 0 outside event windows, where
two cycles' windows overlap, and in buckets without residuals. S_b holds the periods of past
event days in bucket b whose clearing ended before the fit cut, π_E(s) is their actual
price and Ê_s the node-averaged forecast price of the stack *without* our offer, backcast
from the inputs as of that day's own day-ahead decision time (00:00 of the day before) with
the forecaster fitted at the cut; c = `event_bias_clip` = 500 S$/MWh and k =
`event_bias_shrink` = 0. The guard (23) compares the lead-2 h price plus δ_t with its
threshold, and (19a) uses the corrected Ê_t(0). Because δ is the same at every level, our
price impact (the slope of Ē_t in x) is unchanged; (13b) adds h · x · δ to the value of an
action.

### Terminal SOC

The month ends with SOC ≥ `terminal_soc_frac` × 100 MWh = 50 MWh under every convention:

    Y_T ≥ y_term,   y_term = s_tgt if s_tgt ≤ s_0, else s_0 + (s_tgt − s_0) η_c,max / η_c,min   (20)

### Dynamic programme

With the ramp limit, the state is (Y, previous action a′). The recursion is

    V_t(Y, a′) = max_{a ∈ A_t, |x_a − x_a′| ≤ ρ}  F_t(a) + V_{t+1}(Y + ΔY(a), a)            (21)

Here A_t holds the actions allowed by (16), (18), (19) and (19a). V_{T+1}(Y, ·) = 0 if Y ≥ y_term,
and −∞ otherwise (20). Ties go to the lowest action index; idle is index 0, so it wins a tie.

The programme runs at two horizons:

- **Month-ahead.** One decision at the gate of the month's first period, with the calendar's
  event days. It gives the continuation value V^MA at the end of every day.
- **Daily.** Day D's plan is decided at 00:00 of D−1, from day-ahead values (13). It uses (21)
  with the terminal value V_end = V^MA at the end of day D:

      V^D_{end}(Y, a′) = V^MA_{t_D + 1}(Y, a′)                                           (22)

  If (22) is unreachable from the current state, the plan is made to the nearest reachable
  state, with a warning.

### Guard at each period's gate

At T − 65 min, the lead-2 h forecast for T (T's own pre-dispatch vintage) cancels a planned
charge if it forecasts any of these: a margin under 300 MW, a reserve shortfall, or an energy
price above 400 S$/MWh. The replacement is the least-charging action that still satisfies
(17), and from which (20) stays reachable under V^D:

    a_T ← argmax { x_a : x_planned ≤ x_a ≤ 0, |x_a − x_{T−1}| ≤ ρ, V^D_{T+1}(Y + ΔY(a), a) > −∞ },  a ≠ a_planned   (23)

If no such action exists, the planned charge is kept and tagged `guard_blocked`. In August to
October and in November the guard never fired.

### Information timing

Every input to (13) and (23) is read through `InfoSet` at its decision time. The
corrections δ of (13b) are computed at the fit cut (before every decision time of the month)
from clearing periods that ended before it, and recorded as `eventbias.fit`. Pre-dispatch
rows must be issued by T − 65 min, offer-stack rows published by T − 65 min, and training
clearing rows must belong to periods that ended before the decision time. The planner audits
the issue time of its own inputs (`planner.<kind>.inputs`). `generate` ends with
`assert_no_leak()`.
