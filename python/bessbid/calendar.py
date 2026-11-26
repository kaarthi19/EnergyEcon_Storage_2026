"""Event calendar learned from data: periodic outage events, demand surges and the raised
contingency requirement.

Phase A found that EMA's synthetic system has two deterministic outage cycles (an 11-day
and a 19-day one), that on two days of each 19-day event net demand rises by ~680 MW and
the contingency requirement rises from 480 to 550 MW, and that every price spike in
training falls inside those windows. Nothing here is hard-coded: :func:`learn_calendar`

1. reads the known-outage state from pre-dispatch rows issued by the cut (each row's
   ``known_outage_mw_at_issue`` is the outage at its issue time),
2. finds onsets/offsets (jumps of more than ``jump_mw``),
3. explains the onsets with periodic cycles (time of day, period in days, anchor) chosen
   greedily by hits minus misses, and takes each cycle's duration and event MW as the
   mode / median over its events,
4. learns the raised-contingency window from training clearing rows (requirement above
   its usual value) as an offset and duration relative to the cycle that explains it,
5. learns the demand-surge window and MW from actual net demand (excess over the usual
   profile, folded on that cycle).

A cycle is "active" at time t when ``(t - anchor) mod period`` lies in ``[0, duration)``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from .periods import PERIOD

__all__ = ["Cycle", "Window", "EventCalendar", "learn_calendar", "outage_states", "CalendarError"]

_P = np.timedelta64(30, "m")


class CalendarError(RuntimeError):
    pass


def _as_ns(x) -> np.ndarray:
    return pd.DatetimeIndex(np.atleast_1d(np.asarray(x, dtype="datetime64[ns]"))).to_numpy(dtype="datetime64[ns]")


@dataclass(frozen=True)
class Cycle:
    name: str
    period_days: int
    anchor: pd.Timestamp  # one observed onset (period start)
    duration_periods: int
    mw: float  # median event MW (onset jump)
    mw_sd: float
    n_events: int

    def phase(self, t) -> np.ndarray:
        """Periods since the latest onset at or before t (0 .. period*48-1)."""
        t = _as_ns(t)
        n = (t - np.datetime64(self.anchor, "ns")) // _P
        return np.mod(n, self.period_days * 48).astype(np.int64)

    def active(self, t) -> np.ndarray:
        return self.phase(t) < self.duration_periods

    def onsets_between(self, start, end) -> list[pd.Timestamp]:
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        step = pd.Timedelta(days=self.period_days)
        k = int(np.floor((start - self.anchor) / step))
        out, t = [], self.anchor + k * step
        while t <= end:
            if t >= start:
                out.append(t)
            t += step
        return out


@dataclass(frozen=True)
class Window:
    """A window tied to a cycle: active when the cycle phase is in [offset, offset+duration)."""

    cycle: str
    offset_periods: int
    duration_periods: int
    value: float  # contingency MW (raised value) or surge MW
    base: float = 0.0  # value outside the window
    sd: float = 0.0
    n_obs: int = 0


@dataclass
class EventCalendar:
    cut: pd.Timestamp
    cycles: list[Cycle]
    contingency: Window | None
    surge: Window | None
    primary_req: float
    contingency_base: float
    regulation_req: float
    threshold_mw: float
    checks: dict = field(default_factory=dict)

    # ---- lookups --------------------------------------------------------------------------
    def cycle(self, name: str) -> Cycle:
        for c in self.cycles:
            if c.name == name:
                return c
        raise KeyError(name)

    def n_events(self, T) -> np.ndarray:
        return np.sum([c.active(T) for c in self.cycles], axis=0) if self.cycles else np.zeros(len(_as_ns(T)), int)

    def in_event(self, T):
        v = self.n_events(T) > 0
        return bool(v[0]) if np.ndim(T) == 0 else v

    def _window(self, w: Window | None, T) -> np.ndarray:
        if w is None:
            return np.zeros(len(_as_ns(T)), bool)
        ph = self.cycle(w.cycle).phase(T)
        return (ph >= w.offset_periods) & (ph < w.offset_periods + w.duration_periods)

    def is_surge(self, T):
        v = self._window(self.surge, T)
        return bool(v[0]) if np.ndim(T) == 0 else v

    def surge_mw(self, T):
        v = np.where(self._window(self.surge, T), self.surge.value if self.surge else 0.0, 0.0)
        return float(v[0]) if np.ndim(T) == 0 else v

    def contingency_req(self, T):
        w = self.contingency
        v = np.where(self._window(w, T), w.value if w else self.contingency_base, self.contingency_base)
        return float(v[0]) if np.ndim(T) == 0 else v

    def requirements(self, T) -> tuple[float, float, float]:
        return (self.primary_req, float(self.contingency_req(pd.Timestamp(T))), self.regulation_req)

    def expected_outage(self, T, known_outage_mw: float, issue_time) -> float:
        """Expected outage MW at period T given the outage known at ``issue_time``.

        Port of Phase A's ``k_hat``: events active at the issue time keep their observed
        MW (split equally between overlapping events); events starting by T add their
        learned median MW; residual derates persist."""
        T = pd.Timestamp(T)
        tau = pd.Timestamp(issue_time).floor("30min")
        k = float(known_outage_mw)
        a1 = [bool(c.active(T)[0]) for c in self.cycles]
        a0 = [bool(c.active(tau)[0]) for c in self.cycles]
        n0 = sum(a0)
        if n0 == 0:
            return k + sum(c.mw for c, a in zip(self.cycles, a1) if a)
        m = k / n0
        base = max(k - n0 * m, 0.0)
        return base + sum((m if a_tau else c.mw) for c, a_t, a_tau in zip(self.cycles, a1, a0) if a_t)

    def calendar_outage(self, T) -> np.ndarray:
        """Outage expected from the calendar alone (learned event MW, no derates)."""
        return np.sum([np.where(c.active(T), c.mw, 0.0) for c in self.cycles], axis=0)

    # ---- validation ---------------------------------------------------------------------
    def classify(self, states: pd.Series) -> pd.DataFrame:
        """Compare (state > threshold) with the calendar on a known-outage state series."""
        t = states.index.to_numpy(dtype="datetime64[ns]")
        pred = self.n_events(t) > 0
        obs = states.to_numpy(float) > self.threshold_mw
        return pd.DataFrame({"state": states.to_numpy(float), "observed": obs, "calendar": pred}, index=states.index)

    def exceptions(self, states: pd.Series) -> pd.DataFrame:
        d = self.classify(states)
        return d[d["observed"] != d["calendar"]]

    def events_table(self, start, end) -> pd.DataFrame:
        """Onsets, ends, surge and contingency windows of every cycle event in [start, end]."""
        rows = []
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        for c in self.cycles:
            pad = pd.Timedelta(days=c.period_days)
            for on in c.onsets_between(start - pad, end):
                last = on + (c.duration_periods - 1) * PERIOD
                if last < start:
                    continue
                r = dict(cycle=c.name, onset=on, last_period=last, mw=c.mw)
                for nm, w in (("surge", self.surge), ("contingency", self.contingency)):
                    if w is not None and w.cycle == c.name:
                        r[nm + "_start"] = on + w.offset_periods * PERIOD
                        r[nm + "_last"] = on + (w.offset_periods + w.duration_periods - 1) * PERIOD
                rows.append(r)
        return pd.DataFrame(rows).sort_values("onset").reset_index(drop=True) if rows else pd.DataFrame()

    def to_dict(self) -> dict:
        d = {
            "cut": str(self.cut),
            "cycles": [{**asdict(c), "anchor": str(c.anchor)} for c in self.cycles],
            "contingency": asdict(self.contingency) if self.contingency else None,
            "surge": asdict(self.surge) if self.surge else None,
            "requirements": {"primary": self.primary_req, "contingency_base": self.contingency_base,
                             "regulation": self.regulation_req},
            "threshold_mw": self.threshold_mw,
            "checks": self.checks,
        }
        return d


# ------------------------------------------------------------------------------------------
# learning
# ------------------------------------------------------------------------------------------
def outage_states(predispatch: pd.DataFrame) -> pd.Series:
    """Known-outage state by issue time (one value per issue time; rows must agree)."""
    g = predispatch.groupby("issue_time")["known_outage_mw_at_issue"]
    lo, hi = g.min(), g.max()
    if ((hi - lo).abs() > 1e-6).any():
        raise CalendarError("pre-dispatch rows with the same issue time disagree on known outage")
    s = hi.sort_index()
    s.index = pd.DatetimeIndex(s.index).floor("30min")
    return s[~s.index.duplicated(keep="last")]


def _steps(states: pd.Series, jump_mw: float):
    full = states.reindex(pd.date_range(states.index.min(), states.index.max(), freq="30min"))
    d = full.diff()
    on = d[d > jump_mw]
    off = d[d < -jump_mw]
    return full, on, off


def _fit_cycles(full: pd.Series, on: pd.Series, off: pd.Series, min_events: int,
                max_period_days: int) -> list[Cycle]:
    if on.empty:
        return []
    days = (on.index.normalize() - pd.Timestamp("2000-01-01")).days.to_numpy()
    tod = (on.index.hour * 60 + on.index.minute).to_numpy()
    observed = full.notna()
    obs_days = full.index.normalize()
    first_day, last_day = obs_days.min(), obs_days.max()
    remaining = np.ones(len(on), bool)
    cycles: list[Cycle] = []
    cands: dict[int, list[int]] = {}
    while True:
        best = None
        for m in np.unique(tod[remaining]):
            grp_all = tod == m
            grp = grp_all & remaining
            if grp.sum() < min_events:
                continue
            # days on which an onset at this time of day is observable (state at t and t-30m)
            cand_t = pd.date_range(first_day, last_day, freq="D") + pd.Timedelta(minutes=int(m))
            ok = observed.reindex(cand_t, fill_value=False).to_numpy() & observed.reindex(
                cand_t - PERIOD, fill_value=False).to_numpy()
            cand_days = (cand_t[ok].normalize() - pd.Timestamp("2000-01-01")).days.to_numpy()
            for P in range(2, max_period_days + 1):
                for r in np.unique(days[grp] % P):
                    hits = int((grp & (days % P == r)).sum())
                    if hits < min_events:
                        continue
                    pred = int((cand_days % P == r).sum())
                    seen = int((grp_all & (days % P == r)).sum())
                    misses = pred - seen
                    score = hits - 3 * misses
                    key = (score, hits, -P)
                    if best is None or key > best[0]:
                        best = (key, m, P, r, hits, misses)
        if best is None:
            break
        (_, m, P, r, hits, misses) = best
        if misses > max(0, int(0.1 * hits)):
            break
        sel = (tod == m) & (days % P == r) & remaining
        remaining &= ~sel
        ons = on[sel]
        # duration candidates: the first later offset of comparable size, and the offset
        # closest in size; the final choice (see _pick_durations) maximises agreement.
        durs = []
        for t, j in ons.items():
            later = off[(off.index > t) & ((off.abs() - j).abs() <= 0.4 * j)]
            if len(later):
                durs.append(int((later.index[0] - t) / PERIOD))
                k = int(np.argmin((later.abs() - j).abs().to_numpy()))
                durs.append(int((later.index[k] - t) / PERIOD))
        if not durs:
            continue
        vals, cnt = np.unique(durs, return_counts=True)
        dur = int(vals[np.argmax(cnt)])
        cands[len(cycles)] = [int(v) for v in vals[np.argsort(-cnt, kind="stable")]]
        cycles.append(Cycle(
            name="", period_days=int(P), anchor=pd.Timestamp(ons.index[0]), duration_periods=dur,
            mw=float(np.median(ons.to_numpy())), mw_sd=float(np.std(ons.to_numpy())), n_events=int(hits),
        ))
    cycles = _pick_durations(cycles, cands, full)
    cycles.sort(key=lambda c: (c.period_days, c.anchor))
    return [Cycle(f"cycle{c.period_days}d", c.period_days, c.anchor, c.duration_periods, c.mw, c.mw_sd,
                  c.n_events) for c in cycles]


def _pick_durations(cycles: list[Cycle], cands: dict[int, list[int]], full: pd.Series,
                    threshold: float | None = None) -> list[Cycle]:
    """For each cycle choose the candidate duration that best reproduces the observed
    event states (state above half the smallest event MW), others held fixed."""
    if not cycles:
        return cycles
    st = full.dropna()
    t = st.index.to_numpy(dtype="datetime64[ns]")
    thr = threshold if threshold is not None else 0.5 * min(c.mw for c in cycles)
    obs = st.to_numpy(float) > thr
    cycles = list(cycles)
    for _ in range(2):
        for i, opts in cands.items():
            best = None
            for d in opts:
                trial = [c if j != i else Cycle(c.name, c.period_days, c.anchor, d, c.mw, c.mw_sd, c.n_events)
                         for j, c in enumerate(cycles)]
                pred = np.any([c.active(t) for c in trial], axis=0)
                err = int((pred != obs).sum())
                if best is None or err < best[0]:
                    best = (err, d)
            c = cycles[i]
            cycles[i] = Cycle(c.name, c.period_days, c.anchor, best[1], c.mw, c.mw_sd, c.n_events)
    return cycles


def _runs(mask: np.ndarray):
    """(start, length) of True runs."""
    m = np.r_[False, mask, False].astype(int)
    d = np.diff(m)
    s = np.flatnonzero(d == 1)
    e = np.flatnonzero(d == -1)
    return list(zip(s, e - s))


def _fit_window(times: np.ndarray, mask: np.ndarray, cycles: list[Cycle]):
    """Explain the True runs of ``mask`` by a window relative to one cycle."""
    runs = _runs(mask)
    if not runs or not cycles:
        return None
    best = None
    for c in cycles:
        ph = c.phase(times[[s for s, _ in runs]])
        vals, cnt = np.unique(ph, return_counts=True)
        k = int(np.argmax(cnt))
        # most runs explained; ties go to the cycle whose onset is closest before the runs
        key = (int(cnt[k]), -int(vals[k]))
        if best is None or key > best[0]:
            lens = [ln for (s, ln), p in zip(runs, ph) if p == vals[k]]
            lv, lc = np.unique(lens, return_counts=True)
            best = (key, c, int(vals[k]), int(lv[np.argmax(lc)]))
    (n, _), c, off, dur = best
    return c, off, dur, n


def learn_calendar(
    infoset,
    cut,
    jump_mw: float = 300.0,
    threshold_mw: float = 400.0,
    min_events: int = 2,
    max_period_days: int = 40,
) -> EventCalendar:
    """Learn the event calendar from data published / ended by ``cut`` (audited)."""
    cut = pd.Timestamp(cut)
    p = infoset.predispatch_known_at(cut)
    states = outage_states(p)
    full, on, off = _steps(states, jump_mw)
    cycles = _fit_cycles(full, on, off, min_events, max_period_days)

    clr = infoset.clearing_known_at(cut)
    ct = clr["ts"].to_numpy(dtype="datetime64[ns]")

    def mode(x):
        v, n = np.unique(np.round(np.asarray(x, float), 6), return_counts=True)
        return float(v[np.argmax(n)])

    reqP = mode(clr["primary_requirement_mw"]) if len(clr) else 110.0
    reqR = mode(clr["regulation_requirement_mw"]) if len(clr) else 105.0
    creq = clr["contingency_requirement_mw"].to_numpy(float) if len(clr) else np.array([])
    cbase = mode(creq) if len(creq) else 480.0
    cwin = None
    if len(creq) and (creq > cbase).any():
        hi = creq > cbase
        fitted = _fit_window(ct, hi, cycles)
        if fitted:
            c, offp, dur, n = fitted
            cwin = Window(c.name, offp, dur, mode(creq[hi]), cbase, 0.0, n)

    # Surge: actual net demand over its usual profile (median by period of day and day
    # type, outside the contingency window), folded on the contingency window's cycle.
    swin = None
    if len(clr) and cwin is not None:
        t = pd.DatetimeIndex(ct)
        nd = clr["net_demand_mw"].to_numpy(float)
        cyc = next(c for c in cycles if c.name == cwin.cycle)
        ph = cyc.phase(ct)
        key = (t.hour * 2 + t.minute // 30).to_numpy() * 2 + (t.dayofweek >= 5).astype(int)
        inwin = (ph >= cwin.offset_periods) & (ph < cwin.offset_periods + cwin.duration_periods)
        base = pd.Series(nd[~inwin]).groupby(key[~inwin]).median()
        exc = nd - base.reindex(key).to_numpy()
        prof = pd.Series(exc).groupby(ph).mean()
        prof = prof.reindex(range(cyc.period_days * 48))
        peak = np.nanmax(prof.to_numpy())
        if np.isfinite(peak) and peak > 0:
            above = (prof.to_numpy() > 0.5 * peak)
            runs = _runs(np.nan_to_num(above, nan=0).astype(bool))
            s, ln = max(runs, key=lambda r: r[1])
            sel = (ph >= s) & (ph < s + ln)
            swin = Window(cyc.name, int(s), int(ln), float(np.nanmean(exc[sel])), 0.0,
                          float(np.nanstd(exc[sel])), int(sel.sum()))

    cal = EventCalendar(cut, cycles, cwin, swin, reqP, cbase, reqR, threshold_mw)
    exc = cal.exceptions(states)
    cal.checks = {
        "n_states": int(len(states)),
        "n_onsets": int(len(on)),
        "outage_class_exceptions": int(len(exc)),
        "max_outage_outside_events": float(states[cal.n_events(states.index.to_numpy()) == 0].max())
        if len(states) else 0.0,
    }
    if len(creq):
        cal.checks["contingency_rule_agreement"] = float(np.mean(cal.contingency_req(ct) == creq))
    infoset.record("calendar_fit", cut, max(states.index.max(), clr["ts"].max() + PERIOD) if len(clr) else
                   states.index.max(), len(states) + len(clr))
    return cal
