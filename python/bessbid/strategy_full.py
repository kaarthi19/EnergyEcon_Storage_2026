"""The full strategy: Regulation base plus a planned, always-clearing energy overlay.

Layers
------
0. **Regulation** in every period: ``power - |energy MW|`` at the AS floor price, one band.
   No Primary or Contingency (Regulation clears above both in every training period, and
   every split tried loses money; see the README).
1. **Energy overlay.** Fixed-quantity energy offers that always clear (discharge at the
   floor price, charge at the cap price), so the cleared MW equal the offered MW in both a
   price-maker and a price-taker evaluation and our own code knows the SOC exactly without
   any feedback from EMA. Quantities come from an SOC dynamic programme
   (:mod:`bessbid.planner`) on forecast values that include our own price impact (the
   forecast stack is cleared with our offer inside the LP, averaged over net-demand error
   nodes).
2. **Events.** The same programme covers the 19-day stress event: the month-ahead values
   (one decision at the first period's gate) see the calendar's event days, so the
   continuation value makes the battery arrive full; inside event windows discharge may go
   up to 100 MW and charging is banned. An optional exception (``event_recharge``, off by
   default) lifts the ban in cheap night periods of the 19-day windows, so the battery can
   refill for the second surge morning; it lost money in Sep and Oct (docs/strategy.md).
3. **Guards at each period's gate.** At ``T - 65 min`` the lead-2 h forecast of T (T's own
   pre-dispatch vintage) may cancel a planned charge when the forecast margin is thin, a
   reserve shortfall or an energy spike is forecast. The policy of the day's programme is
   defined for every SOC, so the rest of the day continues optimally from the new SOC.

Information rules
-----------------
* the forecaster is fitted at 00:00 of the day before the month (Oct 31 for November),
  with the net-demand history capped at that cut, as in November;
* month-ahead values: decision time = gate of the month's first period;
* day D's plan: decision time = 00:00 of D-1 (legal for every period of D);
* the guard (and the optional lead-2 refinement) for period T: decision time T - 65 min;
* bids of earlier periods are our own decisions, and always-clearing, so the SOC at T is
  known at any earlier decision time.

Every forecast and value call records an audit entry; ``generate`` ends with
``infoset.assert_no_leak()``.

SOC
---
The programme works on the binding SOC ``Y = s0 + eta_c_max * N`` (see :mod:`bessbid.soc`),
which keeps the SOC inside [5 % + margin, 95 % - margin] under every efficiency convention,
and ends the month with SOC >= ``cfg.terminal_soc_frac`` under every convention. The final
bids are re-checked under each convention separately.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .bids import PeriodBid, energy_fixed
from .config import Config, coerce_fields
from .periods import month_slots, parse_month
from .planner import (
    LEVELS,
    NEG,
    ValueModel,
    dp_backward,
    dp_backward_ramp,
    make_action_grid,
    ramp_active,
    ramp_ok,
    soften_end_values,
    terminal_values,
)
from .soc import binding_soc, check_soc, soc_paths, y_terminal_min

__all__ = ["FullParams", "FullStrategy", "plan_month"]


@dataclass(frozen=True)
class FullParams:
    #: largest charge MW (charging is banned inside events unless ``event_recharge`` allows it)
    charge_cap_mw: float = 50.0
    #: largest discharge MW outside event windows
    discharge_cap_mw: float = 50.0
    #: largest discharge MW inside event windows (19-day surge, 11-day outage)
    event_discharge_cap_mw: float = 100.0
    #: ban charging in calendar event windows
    no_charge_in_events: bool = True
    #: exception to that ban: 'none'; 'overnight': charging is allowed in the night hours of
    #: the 19-day stress-cycle windows (the cycle that carries the surge window), unless an
    #: 11-day window overlaps; 'overnight_all': in the night hours of every event window.
    #: Only in periods whose forecast energy price (x = 0, the plan's own ladder) is at most
    #: ``event_recharge_max_price``; the margin ban and the lead-2 h guard still apply.
    event_recharge: str = "none"
    #: night hours [start, end) of the exception, by period start time (h, 0-24; wraps)
    event_recharge_hours: tuple = (0.0, 7.0)
    #: largest forecast energy price (S$/MWh) of a period in which the exception applies; the
    #: lead-2 h guard also cancels such a charge above this price
    event_recharge_max_price: float = 350.0
    #: extra cost (S$/MWh charged) of a charge allowed by the exception, for the forecast's
    #: low bias on stress nights (actual minus day-ahead forecast: median +55 to +87 in Aug-Oct)
    event_recharge_hurdle: float = 0.0
    #: ban charging when the forecast capacity margin is below this (MW)
    guard_margin_mw: float = 300.0
    #: lead-2 h guard: cancel a planned charge if the lead-2 h energy forecast exceeds this
    guard_price: float = 400.0
    #: 'none': day-ahead plan only; 'guard': lead-2 h guards; 'lead2': guards plus a one-step
    #: re-optimisation of T's action on lead-2 h values
    refine: str = "guard"
    #: where the 'lead2' re-optimisation applies: 'all' periods or only calendar 'events'
    lead2_scope: str = "all"
    #: hurdle cost per MWh of energy throughput (discharge + charge), SGD
    hurdle_per_mwh: float = 0.0
    #: average forecast prices over net-demand error nodes
    use_nodes: bool = True
    #: planning grid for Y (MWh)
    y_step: float = 0.5
    #: charge MW granularity
    charge_step_mw: float = 2.0
    #: price of the Regulation band (None: cfg.as_floor_price)
    regulation_price: float | None = None
    #: the forecaster is fitted at 00:00 this many days before the month starts
    plan_lead_days: int = 1
    #: decision time of day D's plan: 'midnight' = 00:00 of D - plan_lead_days;
    #: 'gate' = the gate of D's first period (D 00:00 - gate closure), the latest time
    #: that is legal for every period of D
    plan_time: str = "midnight"
    #: ladder of energy levels cleared with our offer
    levels: tuple = LEVELS
    #: value actions with our own price impact (False: price-taker values at x = 0)
    price_impact: bool = True
    #: largest change of the energy schedule between consecutive periods (MW); None:
    #: ``cfg.bess_ramp_mw``; 0: no limit. The month starts from 0 MW.
    ramp_mw: float | None = None

    def __post_init__(self) -> None:
        if self.refine not in ("none", "guard", "lead2"):
            raise ValueError("refine must be 'none', 'guard' or 'lead2'")
        if self.lead2_scope not in ("all", "events"):
            raise ValueError("lead2_scope must be 'all' or 'events'")
        if self.plan_time not in ("midnight", "gate"):
            raise ValueError("plan_time must be 'midnight' or 'gate'")
        if self.ramp_mw is not None and self.ramp_mw < 0:
            raise ValueError("ramp_mw must be >= 0 (0 = no limit)")
        if self.event_recharge not in ("none", "overnight", "overnight_all"):
            raise ValueError("event_recharge must be 'none', 'overnight' or 'overnight_all'")
        if self.event_recharge_hurdle < 0:
            raise ValueError("event_recharge_hurdle must be >= 0")
        h = tuple(self.event_recharge_hours)
        if len(h) != 2 or not all(0.0 <= float(x) <= 24.0 for x in h):
            raise ValueError("event_recharge_hours must be [start, end] with hours in 0..24")

    @classmethod
    def from_mapping(cls, d: Mapping[str, Any]) -> "FullParams":
        """FullParams from a ``[strategy.full]`` config table; keys left out keep their
        defaults, unknown keys raise ``ValueError``."""
        return cls(**coerce_fields(cls, d, "[strategy.full]"))


def _event_masks(cal, Ts: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
    t = Ts.to_numpy(dtype="datetime64[ns]")
    ev = np.asarray(cal.in_event(t), bool)
    surge = np.asarray(cal.is_surge(t), bool)
    return ev, surge


def _recharge_mask(cal, Ts: pd.DatetimeIndex, p: FullParams) -> np.ndarray:
    """Periods in which the event charging ban may be lifted (``event_recharge``): inside
    an event window of an eligible cycle and inside the night hours. The price condition
    is applied per plan in :func:`_allowed`. Uses only the calendar learned at the fit cut."""
    n = len(Ts)
    if p.event_recharge == "none" or not cal.cycles:
        return np.zeros(n, bool)
    hod = np.asarray(Ts.hour + Ts.minute / 60.0, float)
    lo, hi = (float(x) for x in p.event_recharge_hours)
    night = ((hod >= lo) & (hod < hi)) if lo <= hi else ((hod >= lo) | (hod < hi))
    t = Ts.to_numpy(dtype="datetime64[ns]")
    if p.event_recharge == "overnight_all":
        cyc = np.asarray(cal.in_event(t), bool)
    else:  # 'overnight': the stress cycle only, and no other event at the same time
        stress = cal.surge.cycle if cal.surge is not None else None
        if stress is None:
            return np.zeros(n, bool)
        cyc = np.asarray(cal.cycle(stress).active(t), bool)
        for c in cal.cycles:
            if c.name != stress:
                cyc &= ~np.asarray(c.active(t), bool)
    return night & cyc


def _allowed(grid, ev: np.ndarray, margin: np.ndarray | None, p: FullParams,
             rech: np.ndarray | None = None, price0: np.ndarray | None = None) -> np.ndarray:
    """Allowed actions per period. ``rech``: periods eligible for the event-recharge
    exception (:func:`_recharge_mask`); ``price0``: the plan's forecast energy price at x = 0
    for the same periods (the exception needs it at most ``event_recharge_max_price``)."""
    mw = grid.mw
    n = len(ev)
    A = np.ones((n, grid.n_actions), bool)
    chg = mw < 0
    A[:, chg] = (-mw[chg] <= p.charge_cap_mw + 1e-9)[None, :]  # Eq. (18): charge cap
    if p.no_charge_in_events:
        ban = np.asarray(ev, bool).copy()
        if rech is not None and price0 is not None:
            # Eq. (19a): overnight recharge exception in cheap night periods
            ban &= ~(np.asarray(rech, bool) & (np.asarray(price0, float) <= p.event_recharge_max_price))
        A[np.ix_(ban, chg)] = False  # Eq. (19): no charging in event windows
    if margin is not None and p.guard_margin_mw is not None:
        A[np.ix_(np.asarray(margin) < p.guard_margin_mw, chg)] = False  # Eq. (19): thin margin
    dis = mw > 0
    cap = np.where(ev, p.event_discharge_cap_mw, p.discharge_cap_mw)
    A[:, dis] = mw[dis][None, :] <= cap[:, None] + 1e-9  # Eq. (18): discharge cap
    return A


def plan_month(month: str, infoset, cfg: Config, params: FullParams | None = None, progress=None,
               forecaster=None, cache: dict | None = None) -> tuple[list[PeriodBid], pd.DataFrame, dict]:
    """Plan a month. Returns (bids, per-period plan frame, info dict).

    ``cache``: optional dict reused across calls with the same month, data, levels and
    node setting (parameter studies); it stores the forecasts and price ladders, which do
    not depend on the other parameters."""
    from .forecast import Forecaster

    p = params or FullParams()
    t0 = time.perf_counter()
    y_, m_ = parse_month(month)
    first = pd.Timestamp(year=y_, month=m_, day=1)
    slots = month_slots(month)
    Ts = pd.DatetimeIndex([s.start for s in slots])
    n = len(Ts)
    cut = first - pd.Timedelta(days=p.plan_lead_days)
    cache = {} if cache is None else cache
    ckey = (month, tuple(p.levels), p.use_nodes, p.plan_lead_days, p.plan_time)
    if cache.get("key") not in (None, ckey):
        cache.clear()
    cache["key"] = ckey

    def cached(name, fn):
        if name not in cache:
            cache[name] = fn()
        return cache[name]

    fc = forecaster or cached("fc", lambda: Forecaster(infoset, cfg, nd_history_cap=True).fit(cut))
    vm = ValueModel(fc, cfg, levels=p.levels, reg_price=p.regulation_price)
    grid = make_action_grid(cfg, max_charge_mw=p.charge_cap_mw,
                            max_discharge_mw=max(p.discharge_cap_mw, p.event_discharge_cap_mw),
                            y_step=p.y_step, charge_step_mw=p.charge_step_mw)
    hurdle = p.hurdle_per_mwh * cfg.period_hours * np.abs(grid.mw)[None, :]
    ramp = cfg.bess_ramp_mw if p.ramp_mw is None else p.ramp_mw
    use_ramp = ramp_active(ramp, cfg)
    R_ok = ramp_ok(grid, ramp if use_ramp else None)  # R_ok[a_prev, a]
    x0 = int(np.argmin(np.abs(vm.levels)))

    def values(lad):
        if p.price_impact:
            return vm.values(lad, grid) - hurdle
        flat = {k: np.repeat(lad[k][:, [x0]], len(vm.levels), axis=1) for k in ("E", "R")}
        return vm.values(flat, grid) - hurdle
    ev, surge = _event_masks(fc.calendar, Ts)
    rech = _recharge_mask(fc.calendar, Ts, p) & ev
    nodes_da = fc.nd.nodes_da if p.use_nodes else None
    nodes_l2 = fc.nd.nodes_lead if p.use_nodes else None
    chg_mwh = cfg.period_hours * np.clip(-grid.mw, 0, None)

    def rech_cost(mask):
        """Eq. (13a): hurdle on charging in exception periods (rows of ``mask``)."""
        return p.event_recharge_hurdle * np.asarray(mask, float)[:, None] * chg_mwh[None, :]

    y_term = y_terminal_min(cfg)  # Eq. (20)
    V_term = terminal_values(grid, y_term)

    # ---- month-ahead values: one decision at the first period's gate --------------------
    dec0 = infoset.cutoff(Ts[0])
    inp0 = fc.da_inputs(Ts, dec0)
    lad_ma = cached("lad_ma", lambda: vm.ladder(Ts, dec0, inp0["nd"].to_numpy(), inp0["outage"].to_numpy(),
                                                 kind="month_ahead", nodes=nodes_da,
                                                 inputs_time=inp0["input_time"].to_numpy()))
    F_ma = values(lad_ma) - rech_cost(rech)
    A_ma = _allowed(grid, ev, lad_ma["margin"], p, rech, lad_ma["E"][:, x0])
    days = sorted(set(Ts.normalize()))
    day_idx = [np.where(Ts.normalize() == D)[0] for D in days]
    if use_ramp:
        # only the values at the day boundaries are needed: V_ma[t][s, a_prev]
        V_ma, _ = dp_backward_ramp(F_ma, grid, A_ma, V_term, ramp, keep=[ix[-1] + 1 for ix in day_idx],
                                   policy=False)
    else:
        V_ma, _ = dp_backward(F_ma, grid, A_ma, V_term)
    if progress:
        progress(f"month-ahead values done ({time.perf_counter() - t0:.1f}s)")

    # ---- lead-2 h inputs for the guards (each period at its own gate) -------------------
    l2 = None
    lad_l2 = None
    if p.refine in ("guard", "lead2"):
        l2 = cached("l2", lambda: fc.lead2_many(Ts))
        if p.refine == "lead2":
            il2 = np.arange(n) if p.lead2_scope == "all" else np.where(ev)[0]
            decs = [infoset.cutoff(T) for T in Ts[il2]]
            lad_l2 = cached(("lad_l2", p.lead2_scope), lambda: vm.ladder(
                Ts[il2], decs, l2["nd"].to_numpy()[il2], l2["outage"].to_numpy()[il2], kind="lead2", nodes=nodes_l2,
                inputs_time=pd.to_datetime(l2["issue_time"]).to_numpy()[il2]))
            F_l2 = np.full((n, grid.n_actions), np.nan)
            if len(il2):
                F_l2[il2] = values(lad_l2) - rech_cost(rech[il2])
        if progress:
            progress(f"lead-2h forecasts done ({time.perf_counter() - t0:.1f}s)")

    # ---- daily plans ---------------------------------------------------------------------
    s = grid.nearest_state(cfg.initial_soc_mwh)
    if abs(grid.y[s] - cfg.initial_soc_mwh) > 1e-9:
        raise ValueError("initial SOC must lie on the planning grid; change y_step")
    zero = int(np.where(grid.mw == 0)[0][0])
    a_prev = zero  # Eq. (17): the month starts from an idle energy schedule (x_0 = 0)
    acts = np.zeros(n, dtype=int)
    Y = np.zeros(n)
    tag = np.empty(n, dtype=object)
    da_E = np.zeros(n)
    da_R = np.zeros(n)
    da_margin = np.zeros(n)
    planned = np.zeros(n)
    warnings_: list[str] = []
    cols = np.arange(grid.n_actions)

    def nxt_value(V, k, st):
        """Value-to-go after period k for each action (landing states ``st``, any index)."""
        stc = np.clip(st, 0, grid.n_states - 1)
        v = V[k + 1][stc, cols] if use_ramp else V[k + 1][stc]
        return np.where((st >= 0) & (st < grid.n_states), v, NEG)

    for D, idx in zip(days, day_idx):
        TD = Ts[idx]
        if p.plan_time == "gate":
            dec = max(infoset.cutoff(D), cut)
        else:
            dec = max(D - pd.Timedelta(days=p.plan_lead_days), cut)
        inp = fc.da_inputs(TD, dec)
        lad = cached(("lad_da", D), lambda: vm.ladder(TD, dec, inp["nd"].to_numpy(), inp["outage"].to_numpy(),
                                                       kind="day_ahead", nodes=nodes_da,
                                                       inputs_time=inp["input_time"].to_numpy()))
        F = values(lad) - rech_cost(rech[idx])
        A = _allowed(grid, ev[idx], lad["margin"], p, rech[idx], lad["E"][:, x0])
        V_end = V_ma[idx[-1] + 1]  # Eq. (22): month-ahead continuation value

        def solve(V_end):
            if use_ramp:
                return dp_backward_ramp(F, grid, A, V_end, ramp)
            return dp_backward(F, grid, A, V_end)

        V, pol = solve(V_end)
        if (V[0][s, a_prev] if use_ramp else V[0][s]) <= NEG / 2:
            # the month-ahead target is out of reach (e.g. today's bans differ): plan to the
            # nearest reachable state instead of giving up
            V, pol = solve(soften_end_values(V_end, grid))
            warnings_.append(f"{D.date()}: month-ahead SOC target unreachable from Y={grid.y[s]}; "
                             "planned to the nearest reachable state")
        for k, i in enumerate(idx):
            a = int(pol[k, s, a_prev]) if use_ramp else int(pol[k, s])
            planned[i] = grid.mw[a]
            tg = "plan"
            st = s + grid.steps
            if p.refine == "lead2" and (p.lead2_scope == "all" or ev[i]):
                # one-step re-optimisation on T's lead-2 h values, DA continuation value
                ok = A[k] & R_ok[a_prev]
                cand = np.where(ok, F_l2[i] + nxt_value(V, k, st), NEG)
                a2 = int(cand.argmax())
                if a2 != a and cand[a2] > NEG / 2:
                    a, tg = a2, "lead2"
            if l2 is not None and grid.mw[a] < 0:
                r = l2.loc[Ts[i]]
                margin_l2 = float(np.sum(fc.stacks.nom)) - float(r.nd_eff) - float(r.req_P + r.req_C + r.req_R)
                short = float(r.sP + r.sC + r.sR)
                gp = min(p.guard_price, p.event_recharge_max_price) if (ev[i] and rech[i]) else p.guard_price
                if margin_l2 < p.guard_margin_mw or float(r.E) > gp or short > 1e-6:
                    # cancel the charge, or cut it as far as the ramp limit and the month-end
                    # SOC target still allow (the least charging such action)
                    # Eq. (23)
                    ok = (grid.mw <= 0) & (grid.mw >= grid.mw[a]) & R_ok[a_prev] & (nxt_value(V, k, st) > NEG / 2)
                    ok[a] = False
                    if ok.any():
                        a = int(np.flatnonzero(ok)[np.argmax(grid.mw[ok])])
                        tg = "guard"
                    else:
                        tg = "guard_blocked"
            acts[i] = a
            s = s + int(grid.steps[a])
            a_prev = a
            Y[i] = grid.y[s]
            tag[i] = tg
            da_E[i] = lad["E"][k, x0]
            da_R[i] = lad["R"][k, x0]
            da_margin[i] = lad["margin"][k]
        if progress and D.day % 5 == 0:
            progress(f"planned through {D.date()} ({time.perf_counter() - t0:.1f}s)")

    _check_gates(infoset)
    mw = grid.mw[acts]
    dis = np.clip(mw, 0, None)
    chg = np.clip(-mw, 0, None)
    chk = check_soc(dis, chg, cfg, margin=0.0, terminal_mwh=cfg.terminal_soc_frac * cfg.energy_mwh)
    if not chk.ok:
        bounds = check_soc(dis, chg, cfg, margin=0.0)
        if not bounds.ok:  # cannot happen on the planning grid; never write such bids
            raise RuntimeError(f"planned SOC breaks a limit: {chk.to_dict()}")
        warnings_.append(f"month-end SOC target missed: final SOC {chk.final_min:.1f} MWh "
                         f"< {cfg.terminal_soc_frac * cfg.energy_mwh:.1f} MWh")
    steps_mw = np.abs(np.diff(np.concatenate([[0.0], grid.mw[acts]])))
    if use_ramp and steps_mw.max() > ramp + 1e-6:
        raise RuntimeError(f"planned energy schedule breaks the ramp limit {ramp} MW: {steps_mw.max():.1f}")
    Yc = binding_soc(dis, chg, cfg)
    if np.max(np.abs(Yc - Y)) > 1e-6:
        raise RuntimeError("planned Y path does not match the bid quantities")

    # ---- diagnostics (reported, never change a bid) ---------------------------------------
    from .checks import calendar_consistency

    cal_check = calendar_consistency(fc, Ts)
    if not cal_check["ok"]:
        warnings_.extend(f"calendar check: {m}" for m in cal_check["messages"])

    bids = []
    for i, sl in enumerate(slots):
        x = float(mw[i])
        layer = "regulation" if x == 0 else ("event" if ev[i] else "energy")
        bids.append(energy_fixed(sl.date, sl.period, x, cfg, regulation_mw="fill",  # Eq. (12)
                                 tag=f"full:{layer}:{tag[i]}") if x != 0 else
                    _reg_only(sl, cfg, p))
    frame = pd.DataFrame({"ts": Ts, "date": [s_.date for s_ in slots], "period": [s_.period for s_ in slots]})
    frame["energy_mw"] = mw
    frame["planned_mw"] = planned
    frame["regulation_mw"] = cfg.power_mw - np.abs(mw)
    frame["source"] = tag
    frame["event"] = ev
    frame["surge"] = surge
    frame["event_recharge_ok"] = rech
    frame["soc_binding"] = Y
    for conv, path in soc_paths(dis, chg, cfg).items():
        frame[f"soc_c{conv[0]:.4f}_d{conv[1]:.4f}"] = path
    frame["fc_day_ahead_E"] = da_E
    frame["fc_day_ahead_R"] = da_R
    frame["fc_day_ahead_margin"] = da_margin
    frame["fc_month_ahead_E"] = lad_ma["E"][:, int(np.argmin(np.abs(vm.levels)))]
    if l2 is not None:
        frame["fc_lead2_E"] = l2["E"].reindex(Ts).to_numpy()
        frame["fc_lead2_R"] = l2["R"].reindex(Ts).to_numpy()
        frame["fc_lead2_issue_time"] = l2["issue_time"].reindex(Ts).to_numpy()
    info = {
        "params": {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(p).items()},
        "fit_cut": str(cut),
        "solves": vm.n_solves,
        "soc_check": chk.to_dict(),
        "energy_periods": int((mw != 0).sum()),
        "discharge_mwh": float(dis.sum() * cfg.period_hours),
        "charge_mwh": float(chg.sum() * cfg.period_hours),
        "guarded_periods": int((tag == "guard").sum()),
        "event_charge_periods": int(((mw < 0) & ev).sum()),
        "event_charge_mwh": float(chg[ev].sum() * cfg.period_hours),
        "guard_blocked_periods": int((tag == "guard_blocked").sum()),
        "ramp_mw": float(ramp) if use_ramp else None,
        "max_ramp_mw": float(steps_mw.max()),
        "warnings": warnings_,
        "calendar_check": cal_check,
        "runtime_s": time.perf_counter() - t0,
    }
    return bids, frame, info


def _check_gates(infoset) -> None:
    """Every planner value used for period T was computed at a decision time no later than
    T's gate (``T - gate_closure_min``), and from data available at that decision time."""
    from .infoset import LeakError

    bad = [e for e in infoset.audit if e.accessor.startswith("planner.") and e.for_period is not None
           and (e.decision_time > infoset.cutoff(e.for_period) or not e.ok)]
    if bad:
        raise LeakError(f"{len(bad)} planner values computed after their period's gate: {bad[:3]}")


def _reg_only(sl, cfg: Config, p: FullParams) -> PeriodBid:
    from .bids import regulation_only

    return regulation_only(sl.date, sl.period, cfg, price=p.regulation_price, tag="full:regulation")


@dataclass
class FullStrategy:
    name: str = "full"
    needs_data: bool = True
    params: FullParams = field(default_factory=FullParams)
    progress: object = None
    #: per-period plan of the last ``generate`` call (planned dispatch, SOC, forecasts)
    plan_frame: pd.DataFrame | None = None
    plan_info: dict | None = None

    @classmethod
    def from_options(cls, options: Mapping[str, Any], **kwargs) -> "FullStrategy":
        """The strategy with the parameters of a ``[strategy.full]`` config table."""
        return cls(params=FullParams.from_mapping(options), **kwargs)

    def generate(self, month: str, infoset, cfg: Config) -> list[PeriodBid]:
        if infoset is None:
            raise ValueError("the full strategy needs market data (an InfoSet)")
        bids, frame, info = plan_month(month, infoset, cfg, self.params, progress=self.progress)
        self.plan_frame, self.plan_info = frame, info
        return bids
