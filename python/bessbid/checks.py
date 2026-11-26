"""Diagnostics on the inputs of a month's plan. They are reported (manifest, stderr) and
never change a bid.

:func:`calendar_consistency`
    Compares the event calendar learned at the fit cut with each period's own legal
    pre-dispatch vintage (lead 2 h, else 3 h, issued by T - 65 min):

    * outage events: is the known outage at the vintage's issue time above the calendar's
      threshold exactly when the calendar says an event is active at that time?
    * demand surges: in every predicted surge window, is the vintage net demand clearly
      above the same time of day one and two weeks earlier and later (outside surge
      windows; if there is no such period, the month's median at that time of day and day
      type)? Elsewhere, is there a run of periods with such an excess that the calendar
      does not explain?

    The plan relies on the calendar (it holds the battery full for the predicted surge
    days), so a disagreement is a warning that the calendar may be off for this month.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["calendar_consistency"]

#: a predicted surge window is confirmed when its mean excess net demand reaches this
#: fraction of the learned surge MW
SURGE_CONFIRM_FRAC = 0.5
#: an unexplained surge is a run of at least this many periods above that excess
UNEXPLAINED_RUN = 4


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) index pairs of the True runs of ``mask``."""
    m = np.concatenate([[False], np.asarray(mask, bool), [False]])
    d = np.diff(m.astype(int))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


def _fmt(t) -> str:
    return pd.Timestamp(t).strftime("%Y-%m-%d %H:%M")


def calendar_consistency(fc, Ts) -> dict:
    """Check the fitted calendar of ``fc`` (a fitted :class:`bessbid.forecast.Forecaster`)
    against each period's legal pre-dispatch vintage for the periods ``Ts``. Returns a dict
    with ``ok``, ``messages`` and the numbers behind them. Reads data only through the
    forecaster's InfoSet (each period at its own gate)."""
    cal = fc.calendar
    Ts = pd.DatetimeIndex(Ts)
    out: dict = {"periods": int(len(Ts)), "ok": True, "messages": []}
    vint = fc.nd.lead_many(Ts)
    vint = vint[~vint.index.duplicated(keep="last")].reindex(Ts)
    have = vint["nd"].notna().to_numpy()
    out["periods_with_vintage"] = int(have.sum())
    if not have.any():
        out["ok"] = False
        out["messages"].append("no legal pre-dispatch vintage for any period: calendar not checked")
        return out

    # ---- outage events --------------------------------------------------------------------
    v = vint[have]
    issue = pd.DatetimeIndex(pd.to_datetime(v["issue_time"])).floor("30min")
    obs = v["known_outage_mw"].to_numpy(float) > cal.threshold_mw
    pred = cal.n_events(issue.to_numpy(dtype="datetime64[ns]")) > 0
    mism = obs != pred
    out["outage"] = {
        "threshold_mw": float(cal.threshold_mw),
        "periods_event_observed": int(obs.sum()),
        "periods_event_predicted": int(pred.sum()),
        "mismatch_periods": int(mism.sum()),
        "mismatch_days": sorted({str(t.date()) for t in v.index[mism]}),
    }
    ev_cal = np.asarray(cal.calendar_outage(issue.to_numpy(dtype="datetime64[ns]")), float)
    both = obs & pred
    if both.any():
        out["outage"]["median_known_minus_calendar_mw"] = float(
            np.median(v["known_outage_mw"].to_numpy(float)[both] - ev_cal[both]))
    if mism.any():
        out["ok"] = False
        out["messages"].append(
            f"outage events disagree with the calendar in {int(mism.sum())} of {int(have.sum())} periods "
            f"(days {', '.join(out['outage']['mismatch_days'][:6])})")

    # ---- demand surges --------------------------------------------------------------------
    nd = vint["nd"].to_numpy(float)
    surge = np.asarray(cal.is_surge(Ts.to_numpy(dtype="datetime64[ns]")), bool)
    s_mw = float(cal.surge.value) if cal.surge is not None else 0.0
    thr = SURGE_CONFIRM_FRAC * s_mw if s_mw > 0 else np.inf
    pos = pd.Series(np.arange(len(Ts)), index=Ts)
    usable = have & ~surge
    # fallback baseline: same period of day and day type (weekday / weekend), whole month
    pod = np.asarray(Ts.hour * 2 + Ts.minute // 30)
    wkend = np.asarray(Ts.dayofweek >= 5)
    fallback = {}
    for key in set(zip(pod, wkend)):
        m = usable & (pod == key[0]) & (wkend == key[1])
        fallback[key] = float(np.median(nd[m])) if m.any() else np.nan
    base = np.full(len(Ts), np.nan)
    for k, T in enumerate(Ts):
        vals = []
        for dd in (-14, -7, 7, 14):
            j = pos.get(T + pd.Timedelta(days=dd))
            if j is not None and usable[j]:
                vals.append(nd[j])
        base[k] = float(np.median(vals)) if vals else fallback[(pod[k], wkend[k])]
    excess = nd - base
    windows = []
    for a, b in _runs(surge):
        e = excess[a:b]
        e = e[np.isfinite(e)]
        mean_ex = float(e.mean()) if len(e) else None
        confirmed = mean_ex is not None and mean_ex >= thr
        windows.append({"start": _fmt(Ts[a]), "last": _fmt(Ts[b - 1]), "mean_excess_mw": mean_ex,
                        "confirmed": bool(confirmed)})
        if not confirmed:
            out["ok"] = False
            out["messages"].append(
                f"predicted surge {_fmt(Ts[a])} - {_fmt(Ts[b - 1])} not seen in the lead-2h net demand "
                f"(mean excess {'n/a' if mean_ex is None else f'{mean_ex:+.0f}'} MW, expected about "
                f"+{s_mw:.0f})")
    high = np.isfinite(excess) & (excess >= thr) & ~surge
    unexplained = [{"start": _fmt(Ts[a]), "last": _fmt(Ts[b - 1]), "max_excess_mw": float(np.nanmax(excess[a:b]))}
                   for a, b in _runs(high) if b - a >= UNEXPLAINED_RUN]
    for u in unexplained:
        out["ok"] = False
        out["messages"].append(f"unexplained net-demand surge {u['start']} - {u['last']} "
                               f"(up to {u['max_excess_mw']:+.0f} MW over the same time in other weeks)")
    out["surge"] = {"learned_mw": s_mw, "confirm_threshold_mw": float(thr) if np.isfinite(thr) else None,
                    "predicted_windows": windows, "unexplained_windows": unexplained}
    if out["ok"]:
        out["messages"].append(
            f"calendar consistent with the pre-dispatch vintages: outage events agree in all "
            f"{int(have.sum())} periods; {len(windows)} predicted surge window(s) confirmed "
            + ", ".join(f"{w['start']} ({w['mean_excess_mw']:+.0f} MW)" for w in windows))
    return out
