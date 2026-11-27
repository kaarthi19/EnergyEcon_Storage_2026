# Bidding strategy

This is the strategy description for the November 2026 submission. The BESS is EMA's
hypothetical 100 MW / 100 MWh battery: SOC limits 5-95 %, 85 % round-trip efficiency,
50 % SOC at the start of Nov 1 period 1, 30-minute periods, and 0 MWh reserve and regulation
activation. The equations are in [`formulation.md`](formulation.md), the units in
[`units.md`](units.md), and the rules the code enforces in [`rules.md`](rules.md).

## Main submission: `--strategy full`

Regulation is the base product. On top of it, a planned energy overlay that always clears.

**Layer 0: Regulation.** Every period offers `100 - |energy MW|` of Regulation in one band
at the AS floor price, so the strict capacity rule holds with equality. There are no Primary
or Contingency offers (see the investigations below).

**Layer 1: energy overlay.** Energy is offered only as fixed quantities that always clear:
discharge at the floor price and charge at the cap price (3000). The cleared MW therefore
equal the offered MW whether EMA re-clears the market with our offers (price-maker) or uses
fixed prices (price-taker), and our code knows the SOC in every period without feedback
from EMA. The quantities come from a dynamic programme over SOC (`planner.py`):

- Values include our own price impact. For each period, the forecast stack is cleared with
  our offer inside the LP at 13 signed energy levels (-100 to +100 MW, Regulation filling
  the rest). The prices are averaged over the 9 net-demand error nodes of the forecaster,
  which turns a single merit-order step into an expected price curve. The value of an
  action x is 0.5 h x (x E(x) + (100 - |x|) R(x)).
- Size limits: charge at most 50 MW; discharge at most 50 MW, or 100 MW inside calendar
  event windows.
- **Ramp limit.** RC383 adds energy-storage up/down ramp constraints (D.19.1.1A,
  D.19.1.2A) on the net energy schedule. EMA has not given a ramp rate for the hypothetical
  BESS, and the bids are open-loop, so a band curtailed by a ramp limit would never be made
  up and would shift the SOC for the rest of the month. The plan therefore keeps
  |x(t) - x(t-1)| <= `bess_ramp_mw` = 42 MW (the generators' 0.42 x available MW per period
  in the replicated engine), starting from 0 MW on Nov 1 period 1. The programme's state is
  then (Y, previous action) (`planner.dp_backward_ramp`). `bess_ramp_mw = 0` in the config
  file switches the limit off. Cost: see the results below.
- Bans: no charging inside 11-day or 19-day event windows, or when the day-ahead forecast
  capacity margin is below 300 MW. An overnight exception for the 19-day windows exists as
  an option (`event_recharge`) but is off: it lost money in Sep and Oct (see "Overnight
  recharge inside events" below).
- SOC: the programme's state is the *binding* SOC `Y = s0 + eta_c_max x N`, where `N` is
  the grid-side net energy stored. For any convention, SOC = s0 + eta_c x N, so keeping Y
  in [5 % + margin, 95 % - margin] keeps the SOC inside the limits under **every**
  efficiency convention (all loss on charge, sqrt split, or all loss on discharge). The
  margin is `soc_margin_mwh` = 2 MWh. Actions are exact SOC steps on a 0.5 MWh grid (charge
  in 2 MW steps, discharge in 1.7 MW steps), so the bid quantities reproduce the planned
  path exactly.
- Terminal SOC: the month ends with SOC >= `terminal_soc_frac` (default 0.5) under every
  convention.

**Layer 2: events.** The same programme plans the 19-day stress event. A month-ahead value
function (one decision at the gate of the month's first period) sees the calendar's event
days. It is the continuation value at the end of each daily plan, so the battery arrives
full before the event, discharges up to 100 MW in the periods with the highest forecast
value, and recharges after the event window.

**Guards at each period's gate.** At T - 65 min the lead-2 h forecast for T cancels a
planned charge if it shows a margin under 300 MW, a reserve shortfall or an energy price
above 400. The guard only picks an action from which the month-end SOC target is still
reachable and that respects the ramp limit (the least charging such action; if the planned
charge is the only one, it is kept and tagged `guard_blocked`). The daily policy is defined
for every (SOC, previous action), so the rest of the day continues optimally. If a day's
plan cannot reach the month-ahead target at all, it plans to the nearest reachable SOC and
records a warning instead of stopping. In Aug-Oct and November the guard never fired.

**What the November pre-dispatch file does.** The plan is decided from data available on
Oct 31 (day D's plan at 00:00 of D-1, whose own pre-dispatch vintages are not issued yet),
so the November pre-dispatch vintages (lead 2 h / 3 h, legal at each period's gate) feed
only this guard and the calendar check below. Because the guard never fires on the real
November data, the bids would be the same without that file; `generate` nevertheless
refuses to run without it (exit 1), so a file that is missing or not recognised in EMA's
folder layout cannot go unnoticed and the guard is always live. Re-optimising on the
lead-2 h vintages (`refine='lead2'`) lost money in every backtest month (see the
investigations), so they are deliberately not used for more.

**Calendar check (reported, never changes a bid).** The plan relies on the event calendar
learned at the fit cut: it fills the battery before the predicted 19-day surge days and
bans charging inside event windows. `checks.calendar_consistency` compares that calendar
with each period's own legal pre-dispatch vintage:
- outage events: the known outage at the vintage's issue time is above the calendar's
  threshold exactly when the calendar says an event is active then;
- surges: in each predicted surge window the vintage net demand must be at least half the
  learned surge MW above the same time of day one and two weeks earlier and later
  (non-surge periods); a run of 4+ periods with such an excess outside the predicted
  windows is reported as unexplained.

The result goes to the manifest (`plan.info.calendar_check`) and one line on stderr; any
disagreement is printed as a `WARNING` and listed in `plan.info.warnings`. Results: November
agrees in all 1440 periods (271 event periods observed and predicted), and both predicted
surge windows are confirmed (Nov 1 00:00-11:30 +639 MW, Nov 19 00:00 - Nov 20 11:30 +747 MW,
learned surge 669 MW), with no unexplained surge. Aug, Sep and Oct, checked the same way at
their own fit cuts, also agree in every period with every surge confirmed. A calendar
shifted by one day is flagged on both counts (tested).

**Information timing.**
- The forecaster is fitted at 00:00 the day before the month (Oct 31 for November), with
  net-demand history capped there. Training clearing rows and actuals are used only if
  their period ended strictly *before* the decision time (the Oct 30 23:30 period, which
  ends exactly at Oct 31 00:00, is not used).
- Day D's plan is decided at 00:00 of D-1, using day-ahead forecasts.
- The guard for period T uses T's own lead-2 h vintage at T - 65 min.
- Earlier periods' bids always clear and are our own decisions, so the SOC at T is known
  at every earlier decision time.

Every forecast and value call records an audit entry, including the issue time of any
pre-dispatch vintage behind the planner's net-demand and outage inputs
(`planner.<kind>.inputs`); `generate` ends with `assert_no_leak()`. A test perturbs every row not legal at T's gate and checks that bids for
periods up to T come out identical.

**Layer 3 (not used).** `soc.SocIntervalTracker` keeps a worst-case SOC interval for
price-responsive bands that may or may not clear. Such a band only fits if every later
committed action stays feasible in the worst case and the terminal SOC still holds. The
planned path ends at the terminal minimum and empties the battery during events, so the
room is about zero. Re-optimising each period on lead-2 h values (`refine='lead2'`), which
is the information such a band would use, lost money in every month (table below).

### Walk-forward results (pseudo-Novembers)

Each training month is planned exactly as November would be: fitted at 00:00 the day
before the month, and every bid limited to data legal at its own gate. The result is then
settled by `backtest.simulate`. Revenue does not depend on the efficiency convention,
because the energy offers always clear.

| Month | Regulation only, maker | Full, maker | Uplift, maker | Uplift, taker | Perfect-foresight uplift (cap 50 MW, maker) |
|---|---|---|---|---|---|
| Aug | 1,205,144 | 1,315,124 | +109,980 | +126,932 | +153,841 |
| Sep | 1,209,195 | 1,329,542 | +120,348 | +128,011 | +157,567 |
| Oct | 2,126,795 | 2,247,849 | +121,054 | +143,123 | +220,019 |
| **per 30 days** | 1,480,804 | 1,595,385 | **+114,581** | **+129,804** | +173,291 |

(With the 42 MW ramp limit and the strict clearing cut. The perfect-foresight bound obeys
the same ramp limit.) These are training-month averages; **for November expect about +100k
(maker), not +115k** (see "What to expect for November" below).

- **Positive in every month** under both modes.
- The full strategy captures 66 % of the perfect-foresight uplift (maker).
- **Cost of the ramp limit:** none measurable. Without it (`bess_ramp_mw = 0`) the uplift
  is +114,437 (maker) and +129,815 (taker) per 30 days; with it, +114,581 and +129,804. The
  plans already ramp in and out over several periods because the price curves are smooth,
  so the limit mostly reshapes a few steps (e.g. the largest Nov 19 block falls from 74.8
  to 64.6 MW).
- **Cost of the strict clearing cut** (not using the period that ends exactly at the fit
  time): -0.6k maker, +0.2k taker per 30 days against the earlier, inclusive cut.
- **Revenue by product, Aug-Oct, maker:** energy +614,748; Regulation 4,277,768
  (-263,365 against Regulation only, because the energy MW displace Regulation MW).
- **SOC range:** [7.0, 93.0] MWh under the all-loss-on-discharge convention,
  [10.4, 89.6] under sqrt, and [13.4, 86.6] under all-loss-on-charge.
- **0 breaches** under all three conventions in both modes. Every month ends at >= 50 %.
- **Runtime:** about 190 s per month to plan.
- `python -m bessbid evaluate` reproduces this table; `evaluate.perfect_foresight_plan`
  builds the hindsight bound.

**Investigations** (maker and taker, Aug-Oct; differences in SGD over the three months, or
per month where stated):

- *Regulation offer price.* 0.00 or 0.01 is best. Any price up to about 11 gives the same
  revenue (the lowest training regulation price was 11.3). Offering 100 MW at 12 loses
  22-34k per month and at 15 loses about 330k per month, because cheap generator Regulation
  offers then displace ours. Splitting 5 MW into a second band at 15 or 25 loses 17k or
  57k per month.
- *Primary/Contingency.* Moving part of the 100 MW from Regulation to C or P, even only in
  the raised-contingency windows with hindsight timing, loses in every month and mode:
  R80+C20 -5k to -15k per month, C100 -26k to -74k, R70+P30 -21k to -112k. Regulation is
  priced above C and P in every training period, and our price impact does not reverse that.
- *Energy size vs price impact.*
  - Capping both directions at 30 / 40 / 50 / 75 / 100 MW gives maker uplifts within ±4k of
    each other. 50 MW is the balanced choice: 40 MW is +4k maker and -4k taker.
  - Planning without our own price impact loses 10.5k (maker).
  - Planning on a single point forecast instead of the 9 net-demand error nodes loses 20k
    (maker).
- *Other options tested and rejected:*
  - a throughput hurdle per MWh: +3k to +5k at 5-10 SGD/MWh (mixed by month, noise level),
    -39k at 20 SGD/MWh;
  - allowing charging inside event windows: -2.5k (+14k in Aug, -16k in Sep-Oct);
  - the narrower overnight exception (`event_recharge`): 0k over Aug-Oct, -6k to -7k in
    Sep and Oct (see the next section);
  - correcting the event-window forecast bias (`event_bias`): +0.2k maker, -0.3k taker per
    30 days, losing in Aug and Oct (see "Event-window forecast bias" below);
  - lead-2 h re-optimisation in all periods: -21k; only inside event windows: -3.3k;
  - deciding the daily plan at the gate of D's first period instead of 00:00 of D-1: -0.3k.
- *Terminal SOC.* Ending the month at 5 % instead of 50 % would add about 10k per month.
  It is a config option (`terminal_soc_frac`); 50 % is kept until EMA says otherwise.
- *Wider SOC margin* (`soc_margin_mwh`, with the ramp limit). The bids are open-loop, so a
  band that failed to clear in one period would shift the SOC for the rest of the month.
  With the 2 MWh margin, dropping any one energy band of the Aug-Oct plans breaches 5-95 %
  later in 87-94 % of cases (all-loss-on-discharge convention; the worst path reaches -18
  and 136 MWh). Making every single miss safe needs a margin of one full ramp step
  (42 MW x 0.5 h / 0.85 = 24.7 MWh), and that is expensive:

  | `soc_margin_mwh` | Planned Y range | Uplift per 30 days, maker / taker | Single-miss breaches (charge / discharge, all-loss-on-discharge) |
  |---|---|---|---|
  | 2 (default) | 7-93 | +114.6k / +129.8k | 417/460, 384/421 |
  | 12 | 17-83 | +91.6k / +101.2k | 200/381, 189/371 |
  | 25 | 30-70 | +57.2k / +61.2k | 0/330, 12/255 |

  A 12 MWh margin costs about 23k (maker) / 29k (taker) per 30 days and still leaves half
  of the single misses breaching; 25 MWh costs about 57k / 69k. The default stays at 2 MWh:
  every fixed band cleared in full in every backtest and stress scenario (the prices 0.01
  and 3000 are far outside every observed clearing price), and the ramp limit removes the
  one known EMA-side reason a band might not clear. How EMA treats an SOC breach is on the
  list of questions below.

(The investigations above were run before the ramp limit and the strict clearing cut were
added; both changed the headline by less than 1k per 30 days, so the comparisons stand.)

### Overnight recharge inside events (option, off by default)

The 19-day event window lasts 60 h (12:00 on day 1 to 23:30 on day 3) and holds two surge
mornings. With the ban, the battery enters full, empties on the first surge day and sits
empty through the second surge morning; in November it is empty on the morning of Nov 20,
one of the month's highest day-ahead forecast mornings (10:00-11:30). `[strategy.full] event_recharge`
lifts the ban in a narrow set of periods, Eq. (19a) in [`formulation.md`](formulation.md):

- `"overnight"`: night hours (`event_recharge_hours`, default 00:00-06:30 period starts) of
  the 19-day windows (the cycle that carries the surge window), never where an 11-day window
  overlaps; `"overnight_all"` also allows the 11-day windows' nights;
- only where the plan's own forecast price at x = 0 is at most `event_recharge_max_price`
  (350), from the month-ahead or day-ahead ladder, so legal at that plan's decision time;
- the margin ban (300 MW) still applies, and the lead-2 h guard cancels such a charge if
  T's lead-2 h price is above 350 (instead of 400), as well as on a thin margin or shortfall;
- ramp, SOC (three conventions, 2 MWh margin), capacity and terminal rules are unchanged;
  the programme simply gets more allowed actions;
- `event_recharge_hurdle` (S$/MWh, default 0) adds a planning cost per MWh charged under the
  exception.

Walk-forward, Aug-Oct, uplift over Regulation only (SGD; same evaluator as the main table):

| Option | Aug maker | Sep maker | Oct maker | Per 30 d maker | Aug taker | Sep taker | Oct taker | Per 30 d taker |
|---|---|---|---|---|---|---|---|---|
| `"none"` (default) | 109,980 | 120,348 | 121,054 | 114,581 | 126,932 | 128,011 | 143,123 | 129,804 |
| `"overnight"` | 123,324 | 113,401 | 114,629 | 114,572 | 140,523 | 121,125 | 136,807 | 129,931 |
| difference | +13,344 | -6,946 | -6,425 | -9 | +13,591 | -6,886 | -6,317 | +127 |
| `"overnight"`, hurdle 60 | 123,324 | 115,315 | 121,054 | 117,291 | 140,523 | 123,056 | 143,123 | 132,620 |
| difference | +13,344 | -5,033 | 0 | +2,710 | +13,591 | -4,955 | 0 | +2,816 |

`"overnight_all"` gives exactly the `"overnight"` numbers: an 11-day window ends at 07:00,
so a charge in its night can only be sold outside events at up to 50 MW, which the
programme never found worth it. All variants: SOC range [7.0, 93.0] / [10.4, 89.6] /
[13.4, 86.6] MWh (discharge / sqrt / charge-loss conventions), 0 breaches in both modes,
month-end SOC 50 %, largest ramp step 42 MW. The exception charged 86, 139 and 86 MWh
inside events in Aug, Sep and Oct.

Why it does not pay: **the forecast is biased low on stress nights.** On the eligible
night periods the actual energy price exceeded the day-ahead forecast by a median of +87
(Aug), +55 (Sep) and +65 (Oct) S$/MWh (+63 to +69 against lead-2 h), while on ordinary nights
the median error is -4 to +5. Stress-night charging is therefore dearer than planned, and
0.85 x the later sale price beat it only when the sale hours spiked: in Aug the option let
the planner discharge more into a spiking Aug 16 evening (+26.7k that day) and refill on the
Aug 17 night (Aug 17 net -13.1k); in Sep the Sep 24 refill lost 4.6k (the forecast morning
peak did not materialise) and three small trades 2.3k; in Oct the exception let the planner
discharge into a modest Oct 30 evening and refill on the Oct 31 night at nearly the same
price for the month-end SOC target (-6.3k). A hurdle of 60 S$/MWh (about the measured
bias) removes the October trade but not Sep 24.

Stress tests on the forecast markets (the plan cleared by the independent LP against the
forecast stack and net demand of each month, event deltas shifted by one day or scaled by
0.7 / 1.3; uplift difference "overnight" minus "none", maker, k SGD):

| Market | Base | Shift -1 d | Shift +1 d | MW x 0.7 | MW x 1.3 |
|---|---|---|---|---|---|
| Aug | +13.2 | -0.9 | -3.7 | -2.0 | -1.2 |
| Sep | +11.9 | +1.4 | +12.5 | -2.7 | +3.2 |
| Oct | +1.2 | +25.1 | +6.9 | +1.5 | +0.6 |
| Nov | +8.3 | +3.3 | +7.6 | -4.6 | +1.8 |

Taker differences are within 4.2k of these. The shifted-calendar downside does not get
worse (November's worst shifted case is +58.5k for the default, +66.1k with the option),
but the forecast markets share the stress-night bias, so they overstate the option: they
show Sep +11.9k and Oct +1.2k where the actual walk-forward lost 6.9k and 6.4k. Without a
hurdle the November plan charges 155 MWh inside events (the Nov 1, Nov 19 and Nov 20
nights); with a hurdle of 60, November's plan keeps only the Nov 20 recharge (86 MWh charged 03:30-05:30,
discharged 10:00-11:30): +5.8k on the base forecast market, -1.5k / +6.0k shifted, -2.9k /
-3.2k scaled. Note also that the lead-2 h forecast for Nov 20 10:00-10:30, which the guard
does not use for discharges, is about 35 % below the day-ahead forecast.

**Decision:** the default stays `"none"`. The adoption rule was a gain in every month in
both modes without a materially worse shifted-calendar downside; the option loses in Sep
and Oct in both modes (the hurdle-60 variant still loses in Sep), and its three-month total
is about zero. The option stays in the code and in `config/default.toml`, tested, for a
team that wants to take the Nov 20 bet (`event_recharge = "overnight"`,
`event_recharge_hurdle = 60`).

### Event-window forecast bias (option, off by default)

The bias above is not limited to stress nights. Over Aug-Oct (walk-forward, the day-ahead
energy price at x = 0 averaged over the net-demand nodes, without our offer) the forecast
is unbiased outside events, 5-15 S$/MWh *too high* on the onset afternoon of the 19-day
event, on the afternoon of day X+2 and in the 11-day windows, and too low throughout the
surge window: by a mean of +63 in the X+1 surge night (00:00-05:30), +110 at X+1 midday
(12:00-17:30) and +77 in the X+2 surge night. Part of it is structural: the forecast adds
one flat surge MW to the net-demand profile, while the actual surge is larger at night and
midday (in surge windows night net demand is under-forecast by 36-67 MW, against 20-33 MW
on ordinary nights); the rest comes from the stack side and was not decomposed.

`[strategy.full] event_bias = "residual"` (`bessbid/eventbias.py`, Eq. (13b) in
[`formulation.md`](formulation.md)) learns an additive correction per event bucket and
adds it to every level of the planner's energy-price ladders:

- buckets: the event cycle, the day of the event counted from its onset day (X, X+1, X+2),
  the surge flag and a 3-hour block of the day (`event_bias_hours`); no correction outside
  events or where an 11-day window overlaps the 19-day one (the overlap residual changed
  sign between July, September and October);
- the correction is the mean residual of the bucket (each clipped to +-500), learned at the
  plan's fit cut from backcasts of the past event days: the forecaster fitted at the cut
  forecasts each past event day from the inputs as of that day's own day-ahead decision
  time, and the residual is the actual training price of periods that ended before the cut.
  So August learns from July only, September from Jul-Aug, and November from Jul-Oct. The
  backcasts are in-sample for the model parameters, but their bucket pattern matches the
  true walk-forward residuals (X+1 00:00-05:30: +55 backcast, +63 walk-forward);
- an audit entry `eventbias.fit` records the cut and the newest clearing period used; the
  per-period information test (bids up to T unchanged when every row not legal at T's gate
  is perturbed) and a test that perturbs everything after the cut both pass;
- capacity, ramp, SOC (three conventions, 2 MWh margin), terminal SOC and the event
  charging ban are unchanged; the shift only changes the values. Runtime +5 to +12 s.

**Forecast error, Aug-Oct walk-forward** (each month corrected with what was known at its
own cut; event windows without overlaps; S$/MWh):

| Periods | n | MAE before | MAE after | Mean error (actual - forecast) before | after |
|---|---|---|---|---|---|
| all event windows | 750 | 44.0 | 30.9 | +17.3 | +4.9 |
| - Aug / Sep / Oct | 219 / 306 / 225 | 45.6 / 44.2 / 42.2 | 27.6 / 29.7 / 35.9 | +31.9 / +10.7 / +12.1 | +17.3 / -2.2 / +2.4 |
| surge window | 303 | 88.6 | 59.0 | +57.2 | +9.2 |
| surge nights (00:00-06:30) | 112 | 68.5 | 23.5 | +68.2 (median +64.9) | +18.4 (median +10.4) |
| - Aug / Sep / Oct | 28 / 56 / 28 | 102.3 / 52.1 / 67.3 | 49.6 / 12.9 / 18.7 | +102.3 / +51.7 / +67.3 | +49.6 / +3.9 / +16.3 |
| event windows outside the surge | 447 | 13.8 | 11.9 | -9.7 | +1.9 |
| ordinary periods (unchanged) | 3633 | 14.1 | 14.1 | +0.1 | +0.1 |
| overlaps (not corrected) | 33 | 427.2 | 427.2 | +242.9 | +242.9 |

**Decision value, Aug-Oct walk-forward** (uplift over Regulation only, SGD; same evaluator
as the main table):

| Option | Aug maker | Sep maker | Oct maker | Per 30 d maker | Aug taker | Sep taker | Oct taker | Per 30 d taker |
|---|---|---|---|---|---|---|---|---|
| default (`event_bias = "none"`) | 109,980 | 120,348 | 121,054 | 114,581 | 126,932 | 128,011 | 143,123 | 129,804 |
| `"residual"` | 109,573 | 121,644 | 120,862 | 114,808 | 126,387 | 129,434 | 141,455 | 129,546 |
| difference | -407 | +1,297 | -192 | +227 | -544 | +1,423 | -1,669 | -258 |
| `"residual"` + `event_recharge = "overnight"` | 112,632 | 115,897 | 120,862 | 113,932 | 129,439 | 123,797 | 141,455 | 128,703 |
| difference to the default | +2,651 | -4,451 | -192 | -650 | +2,507 | -4,214 | -1,669 | -1,101 |
| difference to `"overnight"` alone | -10,692 | +2,496 | +6,233 | -640 | -11,084 | +2,672 | +4,648 | -1,228 |

All variants: SOC range [7.0, 93.0] / [10.4, 89.6] / [13.4, 86.6] MWh, 0 breaches in both
modes, month-end SOC 50 %, largest ramp step 42 MW.

Why a better forecast does not plan better:

- *Without the recharge exception* the correction changes only 22, 19 and 8 periods in Aug,
  Sep and Oct. Charging is banned inside events and the battery already arrives full (Y =
  93 MWh, the top of the planning range, at every Aug-Oct event onset), so all
  the corrected periods can change is *when* inside the window the stored energy is sold and
  how the 11-day evenings are shaped. The bias is roughly common to the hours the planner
  chooses between, so the order barely changes, and what does change is spike timing: Aug 16
  +12.8k and Aug 17 -13.0k (taker), Sep 4 +1.1k, Oct 31 -1.7k.
- *With the recharge exception* the correction does what the hurdle was meant to do: the
  stress-night charge falls from 86 / 139 / 86 MWh to 21 / 107 / 0 MWh, which removes the
  October loss (+6.2k against `"overnight"`) and part of September's (+2.5k). But it also
  removes most of the profitable August refill (-10.7k), and the September 24 refill still
  loses 5.1k: there the forecast morning peak did not happen, which a level correction
  cannot fix.

**Forecast-market stress tests** (the plan cleared by the independent LP against each
month's forecast market; difference to the default, maker, k SGD; every energy band
cleared in full in every case):

| Market | Option | Base | Shift -1 d | Shift +1 d | MW x 0.7 | MW x 1.3 |
|---|---|---|---|---|---|---|
| Aug | `"residual"` | -0.8 | 0.0 | -5.1 | -0.5 | -0.8 |
| Sep | `"residual"` | -0.4 | -0.3 | -0.5 | -0.2 | -0.2 |
| Oct | `"residual"` | -2.2 | -0.1 | +0.1 | +0.1 | -0.1 |
| Nov | `"residual"` | -0.1 | 0.0 | 0.0 | -10.9 | 0.0 |
| Aug | `"residual"` + overnight | +2.1 | -0.2 | -10.6 | -1.3 | -1.4 |
| Sep | `"residual"` + overnight | +11.4 | +0.4 | +11.4 | -3.0 | -2.3 |
| Oct | `"residual"` + overnight | -2.2 | -0.1 | +0.1 | +0.1 | -0.1 |
| Nov | `"residual"` + overnight | +1.8 | +2.0 | -1.1 | -12.1 | -1.5 |

November net-demand noise draws: -0.1 / -0.1 / 0.0 (`"residual"`), +1.8 to +2.0 (with
overnight). Taker differences are within 3.5k of these except November x 0.7 (-2.6k and
-3.7k taker). The forecast markets share the uncorrected bias, so they understate the
corrected plan's surge-hour sales; they mainly show that the option does not create a new
downside, except in a smaller-than-expected November event (MW x 0.7, -10.9k maker,
-2.6k taker: the corrected plan moves discharge into larger 18:00 and Nov 19 13:30-14:30
blocks, whose price impact is bigger in a weak event). The worst shifted-calendar case of
November is unchanged (+58.5k for both the default and `"residual"`; 57.4k with overnight).

With `"residual"` the November plan changes in 18 periods on 4 days (MW, default -> option):
Nov 3 15:00 10.2 -> 3.4, 16:00 10.2 -> 0, 18:00 66.3 -> 74.8, 18:30 25.5 -> 34.0; Nov 14
16:30 25.5 -> 3.4, 17:00 1.7 -> 0, 17:30 37.4 -> 34.0, 18:00 61.2 -> 74.8, 18:30 20.4 -> 34.0;
Nov 19 13:30 23.8 -> 34.0, 14:00 64.6 -> 74.8, 14:30 23.8 -> 37.4, 18:00 34.0 -> 0; Nov 25
15:00 40.8 -> 35.7, 15:30 23.8 -> 13.6, 17:30 40.8 -> 35.7, 18:00 40.8 -> 51.0, 18:30 0 -> 10.2.
Expected November effect on the forecast market: -0.1k (maker), 0.0k (taker). With the
overnight exception as well, the only event charge left is 40 MW at Nov 19 02:30 (20 MWh,
against 155 MWh for `"overnight"` alone), and the Nov 20 recharge disappears.

**Decision:** the default stays `"none"`. The adoption rule was a gain in every month in
both modes without a breach or a worse shifted-calendar downside; `"residual"` loses in Aug
and Oct in both modes and its three-month total is +0.2k (maker) / -0.3k (taker) per 30
days, which is noise. Combined with the overnight exception it loses in Sep and Oct. The
November workbook is therefore unchanged (`c6260df2...`); with `event_bias = "residual"` it
would be `2023c8a93eff3629317098d5b8390be942130437e04df407baec00c54fd74a77`. The option
stays in the code and in `config/default.toml`, tested: the corrected forecast is the better
input for any future use that charges inside events or values event hours against ordinary
ones (e.g. a stochastic programme over calendar scenarios).

### What to expect for November

The headline above is the average of three training months, and the strategy settings
(caps, margins, guard) were chosen on those same months, so it is in-sample with respect to
them. November itself has less energy value: no energy on Nov 20 and Nov 26 (event
windows), little on Nov 4 and 15, a pre-charge on Nov 18 that costs money, and the gain on
Nov 19. Clearing the November plan against the forecast November market (the forecast stack
and net demand fitted at the Oct 31 cut, an independent LP) gives:

| November scenario | Uplift, maker | Uplift, taker |
|---|---|---|
| Base forecast | +96.7k | +110.5k |
| Net-demand error drawn from Aug-Oct lead-2 h errors (3 draws) | +93.8k to +105.6k | +110.9k to +121.2k |
| 19-day event one day early / late | +67.1k / +58.5k | +79.7k / +71.0k |
| Event MW x 0.7 / x 1.3 | +88.9k / +106.0k | +102.7k / +119.8k |

The same evaluator gave +120.0k (maker) for the earlier October plan against +119.6k
actual, so it is not biased low. **The realistic November expectation is therefore about +100k (maker range about
94-106k, taker 110-121k), not the +115k three-month average.** Every energy offer cleared in
full in every scenario, so the SOC path holds. Possible upside, not adopted: ending the
month at 5 % SOC if EMA confirms there is no end-of-month requirement (+10k to +16k). The
narrow exception to the event charging ban on the 19-day surge nights has now been tested
(`event_recharge`, above): about zero over Aug-Oct and negative in Sep and Oct, so it is
not adopted. Correcting the event-window forecast bias (`event_bias`, above) cuts the
event-window forecast error by 30 % but leaves the plan's value unchanged (-0.1k on the
November forecast market), so it is not adopted either.

## Fallback submission: `--strategy regulation`

Each period offers 100 MW of Regulation in one band at 0.01 (not 0.00, which the template's
number format shows as "-"). It makes no Energy, Primary or Contingency offers: those rows
are written with every band blank, as the template's own example does for period 2.

Why this is the fallback:

- Regulation cleared above Contingency and Primary in every training period.
- Activation is 0 MWh, so SOC stays at 50 % and cannot breach a limit under any efficiency
  convention.
- It reads no market data, so it cannot break an information rule.
- An offer at 0.01 clears whether EMA re-clears the market with our offers or prices them
  at fixed clearing prices.

## Assumptions to confirm with EMA

- **BESS ramp rate.** Is an ESS ramp limit applied in the simulation (RC383 D.19.1.1A /
  D.19.1.2A), and at what rate? The plan assumes 42 MW per period (`bess_ramp_mw`); a
  faster or no limit costs nothing, a slower one would need a smaller value.
- **SOC breaches.** If a cleared schedule would leave 5-95 %, is it clipped, rejected or
  penalised, and is dispatch SOC-checked before clearing? The plan keeps a 2 MWh margin and
  relies on every fixed band clearing in full (it did in every backtest and stress
  scenario); a band that did not clear would shift the SOC for the rest of the month. A
  margin that absorbs any single miss costs about 57k per 30 days (see the investigations).
- **End-of-month SOC.** Is there a requirement? The plan ends at 50 %.
- **Folder layout.** If EMA's run folder does not hold the template workbook, pass
  `--template`.
