"""Event-window bias correction of the planning energy price (``event_bias = "residual"``).

The structural day-ahead forecast is unbiased on ordinary days but biased low inside the
19-day stress windows: in Aug-Oct the actual energy price beat the planner's day-ahead
forecast by a mean of +63 S$/MWh in the surge nights of day X+1 and +110 in its midday
hours, while it was 10-15 *above* the actual price on the afternoon of the onset day and
in the 11-day outage windows (``docs/strategy.md``). This module learns an additive
correction of the energy price per *event bucket* and the planner adds it to every level
of its price ladders (Eq. (13b) in ``docs/formulation.md``).

Buckets
-------
:func:`event_keys` labels each period from the event calendar learned at the fit cut:

* outside every event window: no bucket (no correction);
* inside exactly one cycle's window: ``"<cycle>:d<k>:<s|n>:h<hh>"`` with ``k`` the calendar
  day counted from the event's onset day (19-day cycle: 0 = day X from 12:00, 1 = X+1,
  2 = X+2), ``s``/``n`` whether the period is in the learned surge window, and ``hh`` the
  first hour of its ``block_hours`` block (period start time);
* inside two windows at once (an 11-day event overlapping the 19-day one): ``"overlap"``,
  which is never corrected (its residual changed sign between July, September and
  October, so a correction there is noise).

Fitting (information rules)
---------------------------
:func:`fit_event_bias` runs at the plan's fit cut (00:00 of the day before the month). With
the forecaster fitted at that cut it *backcasts* every past event day D that ended before
the cut: the node-averaged clearing price of the forecast stack without our offer, from the
inputs as of D's own day-ahead decision time (00:00 of D-1; stack rows published by then,
net-demand history ended by then), exactly as the planner forecasts a future day. The
residual is the actual training clearing price minus that backcast, for periods whose
clearing ended before the cut (``InfoSet.clearing_known_at(cut)``). So an August plan
learns from July only and the November plan from July-October; nothing after the cut is
read, and an audit entry ``eventbias.fit`` records the newest clearing period used.

The model parameters behind a backcast come from data known at the cut, so the backcast
of a July day is in-sample for the model (the calendar, surge MW and stack factor model
include that day). On Aug-Oct the backcast residuals have the same bucket pattern as the
true walk-forward residuals (e.g. day X+1 00:00-05:30: +55 backcast vs +63 walk-forward),
and the correction learned from them cut the out-of-sample event-window MAE of the
day-ahead energy forecast from 44 to 31 S$/MWh in every month (bias +17 -> +4).

The correction per bucket is the mean residual (each residual clipped to +-``clip``),
shrunk by ``n / (n + shrink)`` with ``n`` the number of residual periods in the bucket.
Buckets that no past event visited get no correction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .engine import ClearingEngine, PeriodStack
from .periods import PERIOD

__all__ = ["event_keys", "EventBias", "backcast_residuals", "fit_event_bias"]

OVERLAP = "overlap"


def event_keys(calendar, Ts, block_hours: float = 3.0) -> np.ndarray:
    """Bucket label per period (``""`` outside events, ``"overlap"`` where two cycles'
    windows overlap); see the module docstring."""
    Ts = pd.DatetimeIndex(Ts)
    n = len(Ts)
    out = np.full(n, "", dtype=object)
    if n == 0 or calendar is None or not calendar.cycles:
        return out
    t = Ts.to_numpy(dtype="datetime64[ns]")
    act = np.array([np.asarray(c.active(t), bool) for c in calendar.cycles])
    cnt = act.sum(0)
    surge = np.asarray(calendar.is_surge(t), bool)
    hod = np.asarray(Ts.hour + Ts.minute / 60.0, float)
    bh = float(block_hours)
    blk = (np.floor(hod / bh) * bh).astype(int)
    out[cnt >= 2] = OVERLAP
    for j, c in enumerate(calendar.cycles):
        m = act[j] & (cnt == 1)
        if not m.any():
            continue
        ph = np.asarray(c.phase(t[m]), np.int64)
        onset = Ts[m] - pd.to_timedelta(ph * 30, unit="min")
        day = np.asarray((Ts[m].normalize() - onset.normalize()).days, int)
        out[np.flatnonzero(m)] = [f"{c.name}:d{d}:{'s' if s else 'n'}:h{h:02d}"
                                  for d, s, h in zip(day, surge[m], blk[m])]
    return out


@dataclass
class EventBias:
    """Additive energy-price corrections per event bucket (S$/MWh, actual - forecast)."""

    cut: pd.Timestamp
    block_hours: float
    table: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=["delta", "n", "mean", "days"]))

    def delta(self, calendar, Ts) -> np.ndarray:
        """Correction for each period of ``Ts`` (0 outside events, in overlaps and in
        buckets without residuals)."""
        keys = event_keys(calendar, Ts, self.block_hours)
        d = self.table["delta"].to_dict() if len(self.table) else {}
        return np.array([float(d.get(k, 0.0)) if k and k != OVERLAP else 0.0 for k in keys], float)

    def to_dict(self) -> dict:
        return {"cut": str(self.cut), "block_hours": self.block_hours,
                "buckets": {k: {c: (round(float(v), 3) if c != "days" else int(v)) for c, v in r.items()}
                            for k, r in self.table.iterrows()}}


def backcast_residuals(fc, cut, nodes=None, start=None) -> pd.DataFrame:
    """Residuals ``actual - backcast`` of the day-ahead energy price for every period of
    the past event days before ``cut`` (see the module docstring). ``fc``: a
    :class:`bessbid.forecast.Forecaster` fitted at ``cut``. Returns a frame indexed by
    period start with columns ``forecast``, ``actual``, ``resid``, ``day``."""
    cut = pd.Timestamp(cut)
    if fc.cut is None or fc.cut != cut:
        raise ValueError("the forecaster must be fitted at the correction's cut")
    info = fc.info
    clr = info.clearing_known_at(cut)
    cols = ["forecast", "actual", "resid", "day"]
    if clr.empty:
        return pd.DataFrame(columns=cols)
    act = clr.set_index("ts")["energy_price_sgd_mwh"]
    first = pd.Timestamp(act.index.min()).normalize()
    if start is not None:
        first = max(first, pd.Timestamp(start).normalize())
    last = cut.normalize() - pd.Timedelta(days=1)
    nodes = np.zeros(1) if nodes is None else np.asarray(nodes, float)
    eng = ClearingEngine(fc.cfg, warm_start=False)
    rows = []
    with fc.backcast():
        for D in pd.date_range(first, last, freq="D"):
            Ts = pd.date_range(D, periods=48, freq="30min")
            if not np.asarray(fc.calendar.in_event(Ts.to_numpy(dtype="datetime64[ns]")), bool).any():
                continue
            if not Ts.isin(act.index).all() or Ts[-1] + PERIOD > cut:
                continue
            dec = D - fc.opts["da_lead"]
            try:
                inp = fc.da_inputs(Ts, dec)
                stacks = [fc.stacks.forecast_stack(T, dec, float(inp.at[T, "outage"])) for T in Ts]
            except ValueError:  # no published stack yet for some period of day (early July)
                continue
            for T, sf in zip(Ts, stacks):
                st = PeriodStack(*[np.asarray(a, dtype=float) for a in sf.lp_args()])
                req = fc.calendar.requirements(T)
                nd_eff = float(inp.at[T, "nd"]) + float(inp.at[T, "outage"])
                e = np.mean([eng.clear(st, nd_eff - x, req, None, None).E for x in nodes])
                a = float(act.at[T])
                rows.append((T, e, a, a - e, D))
    if not rows:
        return pd.DataFrame(columns=cols)
    out = pd.DataFrame(rows, columns=["ts"] + cols).set_index("ts")
    info.record("eventbias.fit", cut, out.index.max() + PERIOD, len(out))
    return out


def fit_event_bias(fc, cut, *, block_hours: float = 3.0, clip: float = 500.0, shrink: float = 0.0,
                   nodes=None, residuals: pd.DataFrame | None = None) -> EventBias:
    """Learn the bucket corrections at ``cut`` from :func:`backcast_residuals` (or the given
    ``residuals``)."""
    cut = pd.Timestamp(cut)
    r = backcast_residuals(fc, cut, nodes=nodes) if residuals is None else residuals
    eb = EventBias(cut, float(block_hours))
    if r is None or r.empty:
        return eb
    keys = event_keys(fc.calendar, r.index, block_hours)
    d = pd.DataFrame({"key": keys, "resid": np.clip(r["resid"].to_numpy(float), -clip, clip),
                      "day": pd.DatetimeIndex(r["day"])}, index=r.index)
    d = d[(d["key"] != "") & (d["key"] != OVERLAP)]
    if d.empty:
        return eb
    g = d.groupby("key")
    n = g.size()
    mean = g["resid"].mean()
    tab = pd.DataFrame({"delta": mean * n / (n + float(shrink)), "n": n, "mean": mean,
                        "days": g["day"].nunique()})
    eb.table = tab.sort_index()
    return eb
