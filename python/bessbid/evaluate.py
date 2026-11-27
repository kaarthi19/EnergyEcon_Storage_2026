"""Walk-forward evaluation of the full strategy on pseudo-Novembers.

For each training month M (default Aug, Sep, Oct) the full strategy is planned exactly as
for November: the forecaster is fitted at 00:00 of the day before M (net-demand history
capped there, as November's history ends on Oct 31), every period's bid uses only data
legal at its own gate, and no clearing result of M is fed back. The bids are then settled
by :func:`bessbid.backtest.simulate` in

* ``maker`` mode (our offers re-cleared inside the EMA-replica LP with the actual stacks,
  net demand and requirements; generator ramp state chained), and
* ``taker`` mode (actual training prices, strict ties),

under every efficiency convention, against two baselines:

* ``regulation``: 100 MW of Regulation at the AS floor price (``cfg.as_floor_price``) in every
  period (the fallback strategy);
* ``pf_cap50`` / ``pf_cap100``: a perfect-foresight bound. The *actual* stacks and net
  demand of each period are cleared with our offer at the planner's ladder of energy levels
  (price impact included, baseline ramp state), and the same SOC programme picks the best
  always-clearing schedule with hindsight, under the same SOC, capacity and terminal rules
  (energy capped at 50 or 100 MW) and the same BESS ramp limit (``cfg.bess_ramp_mw``). It
  uses future information and is only a yardstick.

The output is a tidy frame with one row per (policy, month, mode, convention) plus ``ALL``
rows (sums over the months; ``revenue_per_30d`` and ``uplift_per_30d`` scale by the total
number of days).
"""

from __future__ import annotations

import time
from typing import Sequence

import numpy as np
import pandas as pd

from .backtest import PSEUDO_MONTHS, REQ_COLS, MarketData, convention_label, simulate, _resolve_conv
from .bids import PeriodBid, energy_fixed, regulation_only, to_table
from .config import Config
from .engine import BessOffer, ClearingEngine
from .periods import month_slots
from .planner import (LEVELS, NEG, action_values, dp_backward_ramp, dp_forward_ramp, dp_plan, make_action_grid,
                      ramp_active, terminal_values)
from .soc import y_terminal_min

__all__ = ["perfect_foresight_plan", "evaluate", "summarize", "single_miss_exposure", "CONVENTIONS"]

CONVENTIONS = ("discharge", "sqrt", "charge")
_SCALARS = ("revenue", "revenue_per_30d", "rev_E", "rev_P", "rev_C", "rev_R", "dis_mwh", "chg_mwh",
            "energy_periods", "soc_min", "soc_max", "soc_final", "breach_periods", "breach_max_mwh",
            "price_impact_E_mean", "price_impact_R_mean", "days", "runtime_s")
#: reported only when a run uses an SOC policy other than 'none'
_POLICY_SCALARS = ("clipped_periods", "clipped_mwh", "rejected_periods", "rejected_mwh")


def _offer(x: float, cfg: Config) -> BessOffer:
    if x > 0:
        e = ((cfg.discharge_floor_price, float(x)),)
    elif x < 0:
        e = ((cfg.charge_cap_price, float(x)),)
    else:
        e = ()
    q = cfg.power_mw - abs(x)
    return BessOffer(energy=e, regulation=((cfg.as_floor_price, q),) if q > 0 else ())


def perfect_foresight_plan(month: str, data: MarketData, cfg: Config | None = None, cap_mw: float = 50.0,
                           levels=LEVELS, engine: ClearingEngine | None = None,
                           y_step: float = 0.5, charge_step_mw: float = 2.0,
                           ramp_mw: float | None = None) -> tuple[list[PeriodBid], float]:
    """Hindsight-optimal always-clearing schedule for a training month (a bound, not a
    strategy). Returns (bids, value the programme expected)."""
    cfg = cfg or Config()
    eng = engine or ClearingEngine(cfg, warm_start=False)
    lv = np.asarray(levels, float)
    offers = [_offer(x, cfg) for x in lv]
    cl = data.clearing.set_index("ts")
    slots = month_slots(month)
    ts = pd.DatetimeIndex([s.start for s in slots])
    # ramp state: no-BESS market from the first clearing period up to the month
    prev = None
    wts = cl.index[cl.index < ts[0]]
    for t in wts:
        prev = eng.clear(data.stacks.period(int(data.stacks.index_of(t))), float(cl.at[t, "net_demand_mw"]),
                         cl.loc[t, REQ_COLS].to_numpy(float), prev).gen_e
    E = np.zeros((len(ts), len(lv)))
    R = np.zeros_like(E)
    for i, t in enumerate(ts):
        st = data.stacks.period(int(data.stacks.index_of(t)))
        nd = float(cl.at[t, "net_demand_mw"])
        rq = cl.loc[t, REQ_COLS].to_numpy(float)
        for j, o in enumerate(offers):
            r = eng.clear(st, nd, rq, prev, o)
            E[i, j], R[i, j] = r.E, r.R
        prev = eng.clear(st, nd, rq, prev).gen_e
    grid = make_action_grid(cfg, max_charge_mw=cap_mw, max_discharge_mw=cap_mw, y_step=y_step,
                            charge_step_mw=charge_step_mw)
    F = action_values(E, R, lv, grid.mw, cfg)
    ramp = cfg.bess_ramp_mw if ramp_mw is None else ramp_mw
    s0 = grid.state_of(cfg.initial_soc_mwh)
    V_end = terminal_values(grid, y_terminal_min(cfg))
    if ramp_active(ramp, cfg):
        zero = int(np.where(grid.mw == 0)[0][0])
        V, pol = dp_backward_ramp(F, grid, None, V_end, ramp, keep=[0])
        val = float(V[0][s0, zero])
        if val <= NEG / 2:
            raise RuntimeError("no feasible perfect-foresight plan")
        acts, _ = dp_forward_ramp(pol, grid, s0, zero)
    else:
        acts, _, val = dp_plan(F, grid, s0, None, V_end)
    mw = grid.mw[acts]
    bids = [energy_fixed(s.date, s.period, float(x), cfg, regulation_mw="fill", tag="pf") if x != 0
            else regulation_only(s.date, s.period, cfg, tag="pf") for s, x in zip(slots, mw)]
    return bids, val


def _row(policy: str, month: str, summary: dict, cv, cfg: Config) -> dict:
    d = {"policy": policy, "month": month, "mode": summary["mode"],
         "convention": convention_label(_resolve_conv(cv, cfg))}
    d.update({k: summary[k] for k in _SCALARS})
    if summary.get("soc_policy", "none") != "none":
        d["soc_policy"] = summary["soc_policy"]
        d.update({k: summary[k] for k in _POLICY_SCALARS})
    return d


def evaluate(ds, cfg: Config | None = None, months: Sequence[str] = PSEUDO_MONTHS, params=None,
             modes: Sequence[str] = ("maker", "taker"), conventions: Sequence = CONVENTIONS,
             perfect_foresight: Sequence[float] = (50.0, 100.0), data: MarketData | None = None,
             info_factory=None, progress=None, soc_policy: str = "none") -> tuple[pd.DataFrame, dict]:
    """Walk-forward evaluation. ``ds``: a loaded :class:`bessbid.io.Dataset` (or None when
    ``data`` and ``info_factory`` are given). Returns (summary frame, {month: plan frame}).
    ``perfect_foresight``: energy caps (MW) of the hindsight bounds to compute (empty to
    skip). ``info_factory(cfg) -> InfoSet``: a fresh information set per month (default
    ``InfoSet.from_dataset(ds, cfg)``). ``soc_policy``: how the evaluator treats an award
    that would leave the SOC limits (:func:`bessbid.backtest.simulate`; 'none', 'clip' or
    'reject'), applied to the full strategy under each convention.

    The Regulation-only baseline offers the full rating, or, when ``params`` caps Regulation
    by the sustainability rule (``regulation_soc_rule``), the MW the initial SOC sustains."""
    from .infoset import InfoSet
    from .strategy_full import FullParams, baseline_regulation_mw, plan_month

    cfg = cfg or Config()
    params = params or FullParams()
    md = data or MarketData.from_dataset(ds, start=cfg.stack_fit_start)
    eng = ClearingEngine(cfg)
    rows, plans = [], {}
    for m in months:
        mcfg = cfg.replace(month=m)
        info = info_factory(mcfg) if info_factory else InfoSet.from_dataset(ds, mcfg)
        t0 = time.perf_counter()
        bids, frame, pinfo = plan_month(m, info, mcfg, params)
        info.assert_no_leak()
        gen_s = time.perf_counter() - t0
        plans[m] = frame
        tb = to_table(bids)
        reg_mw = baseline_regulation_mw(mcfg, params)
        reg = to_table([regulation_only(s.date, s.period, mcfg, mw=reg_mw) for s in month_slots(m)])
        for mode in modes:
            base = simulate(reg, m, md, mode=mode, cfg=mcfg, engine=eng).summary
            rows.append(_row("regulation", m, base, "sqrt", mcfg))
            for cv in conventions:
                s = simulate(tb, m, md, mode=mode, convention=cv, cfg=mcfg, engine=eng,
                             soc_policy=soc_policy).summary
                r = _row("full", m, s, cv, mcfg)
                r["generate_s"] = gen_s
                r["max_ramp_mw"] = pinfo.get("max_ramp_mw")
                rows.append(r)
        for cap in perfect_foresight:
            pb, _ = perfect_foresight_plan(m, md, mcfg, cap_mw=cap)
            ptb = to_table(pb)
            for mode in modes:
                rows.append(_row(f"pf_cap{int(cap)}", m, simulate(ptb, m, md, mode=mode, cfg=mcfg,
                                                                   engine=eng).summary, "sqrt", mcfg))
        if progress:
            progress(f"{m}: planned in {gen_s:.0f}s")
    return summarize(pd.DataFrame(rows)), plans


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    """Add uplift vs regulation (same month and mode) and ``ALL`` rows."""
    df = df.copy()
    base = df[df["policy"] == "regulation"].set_index(["month", "mode"])["revenue"]
    df["uplift"] = [r.revenue - base.get((r.month, r.mode), np.nan) for r in df.itertuples()]
    df["uplift_per_30d"] = df["uplift"] / df["days"] * 30.0
    agg = []
    for (pol, mode, cv), g in df.groupby(["policy", "mode", "convention"], sort=False):
        d = {"policy": pol, "month": "ALL", "mode": mode, "convention": cv}
        for k in ("revenue", "rev_E", "rev_P", "rev_C", "rev_R", "dis_mwh", "chg_mwh", "energy_periods",
                  "breach_periods", "days", "runtime_s", "uplift") + tuple(c for c in _POLICY_SCALARS if c in g):
            d[k] = g[k].sum()
        d["soc_min"], d["soc_max"] = g["soc_min"].min(), g["soc_max"].max()
        d["breach_max_mwh"] = g["breach_max_mwh"].max()
        d["revenue_per_30d"] = d["revenue"] / d["days"] * 30.0
        d["uplift_per_30d"] = d["uplift"] / d["days"] * 30.0
        d["months_positive"] = int((g["uplift"] > 0).sum())
        if "generate_s" in g:
            d["generate_s"] = g["generate_s"].sum()
        if "max_ramp_mw" in g:
            d["max_ramp_mw"] = g["max_ramp_mw"].max()
        agg.append(d)
    return pd.concat([df, pd.DataFrame(agg)], ignore_index=True)



def single_miss_exposure(energy_mw, cfg: Config, conventions: Sequence = CONVENTIONS,
                         margin: float = 0.0) -> pd.DataFrame:
    """Breach exposure of an open-loop schedule if exactly one energy band fails to clear.

    ``energy_mw``: the planned energy schedule (MW, + discharge) of a month. For each period
    with a non-zero band, the SOC path is recomputed without that band (all other bands
    clear); a case breaches if the path then leaves [soc_min + margin, soc_max - margin]
    anywhere from that period on (a missed band shifts every later SOC by the same amount).
    Returns one row per (convention, side): cases, breaching cases, their share, the worst
    SOC reached and the largest breach (MWh)."""
    from .backtest import soc_path

    x = np.asarray(energy_mw, float)
    dis, chg = np.clip(x, 0, None), np.clip(-x, 0, None)
    lo, hi = cfg.soc_min_mwh + margin, cfg.soc_max_mwh - margin
    rows = []
    for cv in conventions:
        ec, ed = _resolve_conv(cv, cfg)
        soc = soc_path(dis, chg, (ec, ed), cfg)
        smin = np.minimum.accumulate(soc[::-1])[::-1]  # lowest SOC from t on
        smax = np.maximum.accumulate(soc[::-1])[::-1]
        h = cfg.period_hours
        for side, mask, shift in (("discharge", dis > 0, h * dis / ed), ("charge", chg > 0, -h * ec * chg)):
            idx = np.flatnonzero(mask)
            new_min = smin[idx] + shift[idx]
            new_max = smax[idx] + shift[idx]
            br = np.maximum(lo - new_min, 0.0) + np.maximum(new_max - hi, 0.0)
            rows.append({"convention": convention_label((ec, ed)), "side": side, "cases": int(len(idx)),
                         "breach_cases": int((br > 1e-9).sum()),
                         "breach_share": float((br > 1e-9).mean()) if len(idx) else 0.0,
                         "worst_soc_min": float(new_min.min()) if len(idx) else float("nan"),
                         "worst_soc_max": float(new_max.max()) if len(idx) else float("nan"),
                         "breach_max_mwh": float(br.max()) if len(idx) else 0.0})
    return pd.DataFrame(rows)
