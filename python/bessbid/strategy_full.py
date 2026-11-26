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
   up to 100 MW and charging is banned.
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

import numpy as np
import pandas as pd

from .bids import PeriodBid, energy_fixed
from .config import Config
from .periods import month_slots, parse_month
from .planner import (
    LEVELS,
    NEG,
    ValueModel,
    dp_backward,
    make_action_grid,
    terminal_values,
)
from .soc import binding_soc, check_soc, soc_paths, y_terminal_min

__all__ = ["FullParams", "FullStrategy", "plan_month"]


@dataclass(frozen=True)
class FullParams:
    #: largest charge MW outside events (charging is banned inside events)
    charge_cap_mw: float = 50.0
    #: largest discharge MW outside event windows
    discharge_cap_mw: float = 50.0
    #: largest discharge MW inside event windows (19-day surge, 11-day outage)
    event_discharge_cap_mw: float = 100.0
    #: ban charging in calendar event windows
    no_charge_in_events: bool = True
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

    def __post_init__(self) -> None:
        if self.refine not in ("none", "guard", "lead2"):
            raise ValueError("refine must be 'none', 'guard' or 'lead2'")
        if self.lead2_scope not in ("all", "events"):
            raise ValueError("lead2_scope must be 'all' or 'events'")
        if self.plan_time not in ("midnight", "gate"):
            raise ValueError("plan_time must be 'midnight' or 'gate'")


def _event_masks(cal, Ts: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
    t = Ts.to_numpy(dtype="datetime64[ns]")
    ev = np.asarray(cal.in_event(t), bool)
    surge = np.asarray(cal.is_surge(t), bool)
    return ev, surge


def _allowed(grid, ev: np.ndarray, margin: np.ndarray | None, p: FullParams) -> np.ndarray:
    mw = grid.mw
    n = len(ev)
    A = np.ones((n, grid.n_actions), bool)
    chg = mw < 0
    A[:, chg] = (-mw[chg] <= p.charge_cap_mw + 1e-9)[None, :]
    if p.no_charge_in_events:
        A[np.ix_(ev, chg)] = False
    if margin is not None and p.guard_margin_mw is not None:
        A[np.ix_(np.asarray(margin) < p.guard_margin_mw, chg)] = False
    dis = mw > 0
    cap = np.where(ev, p.event_discharge_cap_mw, p.discharge_cap_mw)
    A[:, dis] = mw[dis][None, :] <= cap[:, None] + 1e-9
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
    x0 = int(np.argmin(np.abs(vm.levels)))

    def values(lad):
        if p.price_impact:
            return vm.values(lad, grid) - hurdle
        flat = {k: np.repeat(lad[k][:, [x0]], len(vm.levels), axis=1) for k in ("E", "R")}
        return vm.values(flat, grid) - hurdle
    ev, surge = _event_masks(fc.calendar, Ts)
    nodes_da = fc.nd.nodes_da if p.use_nodes else None
    nodes_l2 = fc.nd.nodes_lead if p.use_nodes else None
    y_term = y_terminal_min(cfg)
    V_term = terminal_values(grid, y_term)

    # ---- month-ahead values: one decision at the first period's gate --------------------
    dec0 = infoset.cutoff(Ts[0])
    inp0 = fc.da_inputs(Ts, dec0)
    lad_ma = cached("lad_ma", lambda: vm.ladder(Ts, dec0, inp0["nd"].to_numpy(), inp0["outage"].to_numpy(),
                                                 kind="month_ahead", nodes=nodes_da))
    F_ma = values(lad_ma)
    A_ma = _allowed(grid, ev, lad_ma["margin"], p)
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
                Ts[il2], decs, l2["nd"].to_numpy()[il2], l2["outage"].to_numpy()[il2], kind="lead2", nodes=nodes_l2))
            F_l2 = np.full((n, grid.n_actions), np.nan)
            if len(il2):
                F_l2[il2] = values(lad_l2)
        if progress:
            progress(f"lead-2h forecasts done ({time.perf_counter() - t0:.1f}s)")

    # ---- daily plans ---------------------------------------------------------------------
    s = grid.nearest_state(cfg.initial_soc_mwh)
    if abs(grid.y[s] - cfg.initial_soc_mwh) > 1e-9:
        raise ValueError("initial SOC must lie on the planning grid; change y_step")
    acts = np.zeros(n, dtype=int)
    Y = np.zeros(n)
    tag = np.empty(n, dtype=object)
    da_E = np.zeros(n)
    da_R = np.zeros(n)
    da_margin = np.zeros(n)
    planned = np.zeros(n)
    nxt_ok = None
    days = sorted(set(Ts.normalize()))
    i0 = 0
    for D in days:
        idx = np.where(Ts.normalize() == D)[0]
        TD = Ts[idx]
        if p.plan_time == "gate":
            dec = max(infoset.cutoff(D), cut)
        else:
            dec = max(D - pd.Timedelta(days=p.plan_lead_days), cut)
        inp = fc.da_inputs(TD, dec)
        lad = cached(("lad_da", D), lambda: vm.ladder(TD, dec, inp["nd"].to_numpy(), inp["outage"].to_numpy(),
                                                       kind="day_ahead", nodes=nodes_da))
        F = values(lad)
        A = _allowed(grid, ev[idx], lad["margin"], p)
        V_end = V_ma[idx[-1] + 1]
        V, pol = dp_backward(F, grid, A, V_end)
        if V[0, s] <= NEG / 2:
            raise RuntimeError(f"{D.date()}: no feasible plan from Y={grid.y[s]}")
        zero = int(np.where(grid.mw == 0)[0][0])
        for k, i in enumerate(idx):
            a = int(pol[k, s])
            planned[i] = grid.mw[a]
            tg = "plan"
            if p.refine == "lead2" and (p.lead2_scope == "all" or ev[i]):
                # one-step re-optimisation on T's lead-2 h values, DA continuation value
                st = s + grid.steps
                ok = (st >= 0) & (st < grid.n_states) & A[k]
                cand = np.where(ok, F_l2[i] + V[k + 1][np.clip(st, 0, grid.n_states - 1)], NEG)
                a2 = int(cand.argmax())
                if a2 != a:
                    a, tg = a2, "lead2"
            if l2 is not None and grid.mw[a] < 0:
                r = l2.loc[Ts[i]]
                margin_l2 = float(np.sum(fc.stacks.nom)) - float(r.nd_eff) - float(r.req_P + r.req_C + r.req_R)
                short = float(r.sP + r.sC + r.sR)
                if margin_l2 < p.guard_margin_mw or float(r.E) > p.guard_price or short > 1e-6:
                    a, tg = zero, "guard"
            acts[i] = a
            s = s + int(grid.steps[a])
            Y[i] = grid.y[s]
            tag[i] = tg
            da_E[i] = lad["E"][k, x0]
            da_R[i] = lad["R"][k, x0]
            da_margin[i] = lad["margin"][k]
        i0 += len(idx)
        if progress and D.day % 5 == 0:
            progress(f"planned through {D.date()} ({time.perf_counter() - t0:.1f}s)")

    _check_gates(infoset)
    mw = grid.mw[acts]
    dis = np.clip(mw, 0, None)
    chg = np.clip(-mw, 0, None)
    chk = check_soc(dis, chg, cfg, margin=0.0, terminal_mwh=cfg.terminal_soc_frac * cfg.energy_mwh)
    if not chk.ok:
        raise RuntimeError(f"planned SOC breaks a limit: {chk.to_dict()}")
    Yc = binding_soc(dis, chg, cfg)
    if np.max(np.abs(Yc - Y)) > 1e-6:
        raise RuntimeError("planned Y path does not match the bid quantities")

    bids = []
    for i, sl in enumerate(slots):
        x = float(mw[i])
        layer = "regulation" if x == 0 else ("event" if ev[i] else "energy")
        bids.append(energy_fixed(sl.date, sl.period, x, cfg, regulation_mw="fill",
                                 tag=f"full:{layer}:{tag[i]}") if x != 0 else
                    _reg_only(sl, cfg, p))
    frame = pd.DataFrame({"ts": Ts, "date": [s_.date for s_ in slots], "period": [s_.period for s_ in slots]})
    frame["energy_mw"] = mw
    frame["planned_mw"] = planned
    frame["regulation_mw"] = cfg.power_mw - np.abs(mw)
    frame["source"] = tag
    frame["event"] = ev
    frame["surge"] = surge
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

    def generate(self, month: str, infoset, cfg: Config) -> list[PeriodBid]:
        if infoset is None:
            raise ValueError("the full strategy needs market data (an InfoSet)")
        bids, frame, info = plan_month(month, infoset, cfg, self.params, progress=self.progress)
        self.plan_frame, self.plan_info = frame, info
        return bids
