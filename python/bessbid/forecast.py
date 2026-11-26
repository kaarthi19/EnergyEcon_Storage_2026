"""Structural price forecasts: forecast stack x forecast net demand x calendar -> clearing LP.

Phase A's best models, rebuilt on the audited InfoSet:

* ``lead2`` (Phase A "B7_CYC"): for period T, decision time ``T - 65 min``. Net demand from
  T's own lead-2 h vintage (lead 3 h fallback), expected outage from the vintage's known
  outage moved forward with the event calendar, requirements from the calendar, the legal
  7-lag factor-normalised stack, solved with the clearing LP.
* ``day_ahead`` (Phase A "DA_B7_CYC_SURGE"): one decision time for a whole day (default
  00:00 of the previous day). Net demand from the day-ahead profile plus the learned surge,
  expected outage from the calendar (strict reading) or from the latest known outage state
  moved forward with the calendar (broad reading), same stack/LP.
* ``scenarios``: stage 1 propagates net-demand error nodes (and, day-ahead in event
  windows, the event MW x {0.87, 1, 1.13}) through the LP, which keeps the convexity of
  scarcity pricing; stage 2 multiplies by whole-day empirical log-residual blocks of the
  same regime from a walk-forward residual library, which adds stack error and keeps the
  intraday autocorrelation and cross-product correlation of errors. Gives means,
  quantiles, P(spike) and equally weighted price paths.

Every forecast checks that its decision time is not before the fit cut and adds an audit
entry (``forecast.lead2`` / ``forecast.day_ahead`` / ``forecast.scenarios``) holding the
latest data time it used; the underlying InfoSet accessors log their own entries too.

:func:`walk_forward` re-fits monthly (cut = 00:00 of the day before the month starts, the
earliest day-ahead decision of the month) and returns forecasts next to actual prices;
:func:`evaluate` computes the accuracy table.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import _clearlp
from .calendar import EventCalendar, learn_calendar
from .config import Config
from .infoset import InfoSet
from .netdemand import NetDemandModel
from .periods import PERIOD
from .stackmodel import StackModel

__all__ = [
    "Forecaster", "walk_forward", "evaluate", "PRICE_COLUMNS", "KEYS", "SPIKE_MW",
    "ScenarioSet",
]

KEYS = ("E", "P", "C", "R")
PRICE_COLUMNS = {
    "E": "energy_price_sgd_mwh",
    "P": "primary_price_sgd_mwh_equiv",
    "C": "contingency_price_sgd_mwh_equiv",
    "R": "regulation_price_sgd_mwh_equiv",
}
SPIKE_MW = 400.0
NQ_LEVELS = (0.1, 0.5, 0.9)


def _ts(x) -> pd.Timestamp:
    return pd.Timestamp(x)


def _max_time(*xs):
    v = [pd.Timestamp(x) for x in xs if x is not None and not pd.isna(x)]
    return max(v) if v else None


@dataclass
class ScenarioSet:
    """Equally weighted price scenarios for consecutive periods."""

    periods: pd.DatetimeIndex
    values: np.ndarray  # (n_scenarios, n_periods, 4) in KEYS order
    summary: pd.DataFrame  # per period: mean / q10 / q50 / q90 / p_spike per product
    decision_time: pd.Timestamp
    kind: str


class Forecaster:
    """Structural forecaster. ``Forecaster(data, cfg).fit(train_cut)`` then
    ``.lead2(T)``, ``.lead2_many(Ts)``, ``.day_ahead(day, decision_time)``,
    ``.scenarios(...)``.

    ``data`` is an :class:`InfoSet` or a :class:`bessbid.io.Dataset`."""

    def __init__(
        self,
        data,
        cfg: Config | None = None,
        *,
        n_lags: int = 7,
        nd_source: str = "auto",
        da_outage: str = "auto",
        bias_correction: bool = False,
        chain_ramp: bool = False,
        da_lead: pd.Timedelta = pd.Timedelta(days=1),
        event_mw_factors: tuple[float, ...] = (0.87, 1.0, 1.13),
        spike_mw: float = SPIKE_MW,
        nd_history_cap: bool = False,
        solver: str = "auto",
    ):
        if isinstance(data, InfoSet):
            self.info = data
            self.cfg = cfg or data.cfg
        else:
            self.cfg = cfg or Config()
            self.info = InfoSet.from_dataset(data, self.cfg)
        if da_outage not in ("auto", "calendar", "latest"):
            raise ValueError("da_outage must be 'auto', 'calendar' or 'latest'")
        self.opts = dict(n_lags=n_lags, nd_source=nd_source, da_outage=da_outage,
                         bias_correction=bias_correction, chain_ramp=chain_ramp,
                         da_lead=pd.Timedelta(da_lead), event_mw_factors=tuple(event_mw_factors),
                         spike_mw=spike_mw, nd_history_cap=nd_history_cap)
        self.da_outage = ("calendar" if self.cfg.strict_predispatch else "latest") if da_outage == "auto" else da_outage
        self.cut: pd.Timestamp | None = None
        self.calendar: EventCalendar | None = None
        self.nd: NetDemandModel | None = None
        self.stacks: StackModel | None = None
        self.residuals: pd.DataFrame | None = None
        self._solver_name, self._solve = _make_solver(solver, self.cfg)

    # ---- fitting ---------------------------------------------------------------------------
    def fit(self, train_cut, residual_start=None) -> "Forecaster":
        """Fit calendar, net-demand and stack models on data available at ``train_cut``.

        With ``residual_start`` also build the scenario residual library from a monthly
        walk-forward over [residual_start, train_cut)."""
        cut = _ts(train_cut)
        self.cut = cut
        o = self.opts
        self.calendar = learn_calendar(self.info, cut)
        self.nd = NetDemandModel(self.info, self.calendar, self.cfg, source=o["nd_source"],
                                 bias_correction=o["bias_correction"], da_lead=o["da_lead"],
                                 history_cap=o["nd_history_cap"]).fit(cut)
        self.stacks = StackModel(self.info, self.cfg, n_lags=o["n_lags"]).fit(cut)
        self.residuals = None
        if residual_start is not None:
            self.build_residual_library(residual_start)
        return self

    def _check(self, decision_time: pd.Timestamp) -> None:
        if self.cut is None:
            raise RuntimeError("Forecaster.fit() first")
        if decision_time < self.cut:
            raise ValueError(f"decision time {decision_time} is before the fit cut {self.cut}")

    def summary(self) -> dict:
        return {
            "cut": str(self.cut),
            "options": {k: (str(v) if isinstance(v, pd.Timedelta) else v) for k, v in self.opts.items()},
            "da_outage": self.da_outage,
            "solver": self._solver_name,
            "calendar": self.calendar.to_dict() if self.calendar else None,
            "netdemand": self.nd.summary() if self.nd else None,
            "stacks": self.stacks.summary() if self.stacks else None,
            "residual_days": int(self.residuals["date"].nunique()) if self.residuals is not None else 0,
        }

    # ---- core --------------------------------------------------------------------------------
    def _clear(self, sf, nd_eff: float, req, prev):
        return self._solve(sf.lp_args(), nd_eff, req, prev)

    def _period(self, T, dec, nd, K, prev, kind, issue_time=None, lead=None, src=""):
        sf = self.stacks.forecast_stack(T, dec, K)
        req = self.calendar.requirements(T)
        r = self._clear(sf, nd + K, req, prev)
        used = _max_time(issue_time, sf.newest_avail)
        self.info.record(f"forecast.{kind}", dec, used, 1, T)
        row = dict(
            ts=T, decision_time=dec, E=r.E, P=r.P, C=r.C, R=r.R, sP=r.sP, sC=r.sC, sR=r.sR,
            unserved=r.unserved, nd=nd, outage=K, nd_eff=nd + K, req_P=req[0], req_C=req[1],
            req_R=req[2], surge=bool(self.calendar.is_surge(T)), event=bool(self.calendar.in_event(T)),
            lead_hours=lead, issue_time=issue_time, nd_source=src, n_lags=len(sf.lag_ts),
            newest_stack=sf.lag_ts[0],
        )
        return row, r.gen

    # ---- lead-2h -----------------------------------------------------------------------------
    def lead2_many(self, Ts, chain: bool | None = None) -> pd.DataFrame:
        """Lead-2 h point forecasts for periods ``Ts`` (each at its own cutoff). With
        ``chain`` the ramp state is carried from the previous period's forecast (which used
        an earlier cutoff) when the periods are consecutive."""
        chain = self.opts["chain_ramp"] if chain is None else chain
        Ts = pd.DatetimeIndex(sorted(set(pd.DatetimeIndex(Ts))))
        for T in Ts:
            self._check(self.info.cutoff(T))
        vint = self.nd.lead_many(Ts)
        rows, prev, prevT = [], None, None
        for T in Ts:
            dec = self.info.cutoff(T)
            if T in vint.index:
                v = vint.loc[T]
                nd, K0, it, lead = float(v.nd), float(v.known_outage_mw), pd.Timestamp(v.issue_time), float(v.lead_hours)
                K = self.calendar.expected_outage(T, K0, it)
                src = "vintage"
            else:  # no legal vintage: fall back to the day-ahead inputs at this cutoff
                nd = float(self.nd.profile([T], dec)[0])
                K, it, lead = self._da_outage([T], dec)[0], None, None
                src = "profile:" + self.nd.source
            p = prev if (chain and prevT is not None and T - prevT == PERIOD) else None
            row, gen = self._period(T, dec, nd, K, p, "lead2", it, lead, src)
            rows.append(row)
            prev, prevT = gen, T
        return pd.DataFrame(rows).set_index("ts")

    def lead2(self, T, prev_gen=None) -> pd.Series:
        """Lead-2 h point forecast for one period (no ramp chaining unless ``prev_gen``)."""
        T = _ts(T)
        dec = self.info.cutoff(T)
        self._check(dec)
        vint = self.nd.lead_many([T])
        if T in vint.index:
            v = vint.loc[T]
            nd, it, lead = float(v.nd), pd.Timestamp(v.issue_time), float(v.lead_hours)
            K = self.calendar.expected_outage(T, float(v.known_outage_mw), it)
            src = "vintage"
        else:
            nd = float(self.nd.profile([T], dec)[0])
            K, it, lead, src = self._da_outage([T], dec)[0], None, None, "profile:" + self.nd.source
        row, _ = self._period(T, dec, nd, K, prev_gen, "lead2", it, lead, src)
        return pd.Series(row)

    # ---- day-ahead ---------------------------------------------------------------------------
    def _da_outage(self, Ts, dec) -> np.ndarray:
        if self.da_outage == "latest":
            st = self.nd.known_outage_at(dec)
            if st is not None:
                return np.array([self.calendar.expected_outage(T, st[0], st[1]) for T in Ts])
        return self.calendar.calendar_outage(pd.DatetimeIndex(Ts).to_numpy(dtype="datetime64[ns]"))

    def da_inputs(self, Ts, decision_time) -> pd.DataFrame:
        """Net demand and expected outage used by the day-ahead forecast, plus
        ``input_time``: the issue time of T's own pre-dispatch vintage when one was used
        (NaT otherwise; the history behind the profile is audited at the decision time)."""
        Ts = pd.DatetimeIndex(Ts)
        dec = _ts(decision_time)
        nd = self.nd.profile(Ts, dec)
        K = self._da_outage(Ts, dec)
        itime = np.full(len(Ts), np.datetime64("NaT", "ns"), dtype="datetime64[ns]")
        # T's own vintage issued by the decision time carries a better outage too
        vin = self.nd._own_vintages_by(Ts, dec)
        if len(vin):
            rows = self.info.predispatch_for_many(vin.index)
            rows = rows[rows["issue_time"] <= dec]
            for T, r in rows.iterrows():
                k = Ts.get_loc(T)
                K[k] = self.calendar.expected_outage(T, float(r.known_outage_mw_at_issue), r.issue_time)
                itime[k] = np.datetime64(pd.Timestamp(r.issue_time), "ns")
            for T in vin.index:  # the profile's own vintage (same rows, same filter)
                k = Ts.get_loc(T)
                if pd.isna(itime[k]):
                    itime[k] = np.datetime64(dec, "ns")
        return pd.DataFrame({"nd": nd, "outage": K, "input_time": itime}, index=Ts)

    def day_ahead_periods(self, Ts, decision_time, chain: bool | None = None) -> pd.DataFrame:
        """Forecasts for any periods ``Ts`` (all after ``decision_time``) made at one
        decision time."""
        chain = self.opts["chain_ramp"] if chain is None else chain
        dec = _ts(decision_time)
        self._check(dec)
        Ts = pd.DatetimeIndex(sorted(set(pd.DatetimeIndex(Ts))))
        if (Ts - self.info.gate < dec).any():
            raise ValueError("day-ahead decision time is after the cutoff of a target period")
        inp = self.da_inputs(Ts, dec)
        rows, prev, prevT = [], None, None
        for T in Ts:
            p = prev if (chain and prevT is not None and T - prevT == PERIOD) else None
            row, gen = self._period(T, dec, float(inp.at[T, "nd"]), float(inp.at[T, "outage"]), p,
                                    "day_ahead", None, None, "profile:" + self.nd.source)
            rows.append(row)
            prev, prevT = gen, T
        return pd.DataFrame(rows).set_index("ts")

    def day_ahead(self, day, decision_time=None, chain: bool | None = None) -> pd.DataFrame:
        """48 forecasts for ``day`` made at ``decision_time`` (default: 00:00 the day before)."""
        day = _ts(day).normalize()
        dec = day - self.opts["da_lead"] if decision_time is None else _ts(decision_time)
        return self.day_ahead_periods(pd.date_range(day, periods=48, freq="30min"), dec, chain)

    # ---- residual library and scenarios --------------------------------------------------------
    def day_regime(self, day) -> str:
        """'cycle19d'-style name of the longest-period cycle active on the day, else 'normal'."""
        Ts = pd.date_range(_ts(day).normalize(), periods=48, freq="30min").to_numpy(dtype="datetime64[ns]")
        act = [c for c in sorted(self.calendar.cycles, key=lambda c: -c.period_days) if c.active(Ts).any()]
        return act[0].name if act else "normal"

    def build_residual_library(self, start) -> pd.DataFrame:
        """Walk-forward log residuals log(actual / forecast) for whole days in
        [start, cut) (monthly refits with the same options; actuals known at the cut)."""
        start = _ts(start).normalize()
        end_day = (self.cut - pd.Timedelta(days=1)).normalize()
        if end_day < start:
            self.residuals = pd.DataFrame()
            return self.residuals
        wf = walk_forward(self.info, self.cfg, start, end_day + pd.Timedelta(hours=23, minutes=30),
                          kinds=("lead2", "day_ahead"), last_cut=self.cut, **self.opts)
        act = self.info.clearing_known_at(self.cut).set_index("ts")
        rows = []
        for kind in ("lead2", "day_ahead"):
            d = pd.DataFrame(index=wf.index)
            for k in KEYS:
                a = act[PRICE_COLUMNS[k]].reindex(wf.index)
                d[k] = np.log(np.maximum(a, 0.01)) - np.log(np.maximum(wf[f"{kind}_{k}"], 0.01))
            d["kind"] = kind
            rows.append(d)
        lib = pd.concat(rows)
        lib["date"] = lib.index.normalize()
        lib["pod"] = lib.index.hour * 2 + lib.index.minute // 30
        full = lib.groupby(["kind", "date"])["E"].transform(lambda x: x.notna().sum()) == 48
        lib = lib[full].copy()
        lib["regime"] = lib["date"].map({d: self.day_regime(d) for d in lib["date"].unique()})
        self.residuals = lib
        return lib

    def _blocks(self, kind: str, day, max_blocks: int | None):
        if self.residuals is None or self.residuals.empty:
            return np.zeros((1, 48, 4)), ["none"]
        lib = self.residuals[self.residuals["kind"] == kind]
        reg = self.day_regime(day)
        sub = lib[lib["regime"] == reg]
        if sub["date"].nunique() < 3:
            sub = lib
        dates = sorted(sub["date"].unique())
        if max_blocks is not None and len(dates) > max_blocks:
            dates = dates[-max_blocks:]
        arr = np.zeros((len(dates), 48, 4))
        for i, dt in enumerate(dates):
            b = sub[sub["date"] == dt].sort_values("pod")
            arr[i] = np.nan_to_num(b[list(KEYS)].to_numpy(float))
        return arr, [str(pd.Timestamp(d).date()) for d in dates]

    def scenarios(self, Ts=None, *, day=None, kind: str = "day_ahead", decision_time=None,
                  max_blocks: int | None = None, use_residuals: bool = True) -> ScenarioSet:
        """Scenario ensemble for periods ``Ts`` (or a whole ``day``).

        ``kind='lead2'``: each period at its own cutoff, lead-2 net-demand error nodes.
        ``kind='day_ahead'``: one ``decision_time`` (default 00:00 of the previous day),
        day-ahead nodes and event-MW factors in event windows."""
        if day is not None:
            Ts = pd.date_range(_ts(day).normalize(), periods=48, freq="30min")
        Ts = pd.DatetimeIndex(sorted(set(pd.DatetimeIndex(Ts))))
        if kind == "day_ahead":
            dec = (Ts[0].normalize() - self.opts["da_lead"]) if decision_time is None else _ts(decision_time)
            self._check(dec)
            base = self.da_inputs(Ts, dec)
            nodes = self.nd.nodes_da
            decs = [dec] * len(Ts)
        elif kind == "lead2":
            decs = [self.info.cutoff(T) for T in Ts]
            for d in decs:
                self._check(d)
            vint = self.nd.lead_many(Ts)
            nd_l, K_l = [], []
            for T, d in zip(Ts, decs):
                if T in vint.index:
                    v = vint.loc[T]
                    nd_l.append(float(v.nd))
                    K_l.append(self.calendar.expected_outage(T, float(v.known_outage_mw), pd.Timestamp(v.issue_time)))
                else:
                    nd_l.append(float(self.nd.profile([T], d)[0]))
                    K_l.append(float(self._da_outage([T], d)[0]))
            base = pd.DataFrame({"nd": nd_l, "outage": K_l}, index=Ts)
            nodes = self.nd.nodes_lead
            dec = max(decs)
        else:
            raise ValueError("kind must be 'lead2' or 'day_ahead'")

        # stage 1: LP over net-demand nodes (x event-MW factors), no ramp state
        stage1 = []
        for T, d in zip(Ts, decs):
            nd0, K0 = float(base.at[T, "nd"]), float(base.at[T, "outage"])
            kf = self.opts["event_mw_factors"] if (kind == "day_ahead" and self.calendar.in_event(T)) else (1.0,)
            vals = []
            for f in kf:
                K = K0 * f
                sf = self.stacks.forecast_stack(T, d, K)
                req = self.calendar.requirements(T)
                for e in nodes:
                    r = self._clear(sf, nd0 - e + K, req, None)
                    vals.append((r.E, r.P, r.C, r.R))
            stage1.append(np.array(vals))
        n1 = max(len(v) for v in stage1)
        # pad periods without event factors by repeating their node set (equal weights kept)
        S1 = np.stack([np.resize(v, (n1, 4)) if len(v) != n1 else v for v in stage1], axis=1)  # (n1, nT, 4)

        # stage 2: whole-day residual blocks of the same regime
        days = Ts.normalize()
        pods = np.asarray(Ts.hour * 2 + Ts.minute // 30)
        if use_residuals:
            blocks = {}
            for dday in days.unique():
                blocks[dday] = self._blocks(kind, dday, max_blocks)[0]
            nb = min(len(b) for b in blocks.values())
            R = np.zeros((nb, len(Ts), 4))
            for j, (dday, pod) in enumerate(zip(days, pods)):
                R[:, j, :] = blocks[dday][-nb:, pod, :]
        else:
            R = np.zeros((1, len(Ts), 4))
        vals = (S1[:, None, :, :] * np.exp(R[None, :, :, :])).reshape(-1, len(Ts), 4)
        self.info.record(f"forecast.scenarios.{kind}", dec, None, len(Ts), Ts[0])
        summ = {}
        thr = self.opts["spike_mw"]
        for i, k in enumerate(KEYS):
            v = vals[:, :, i]
            summ[f"{k}_mean"] = v.mean(0)
            for q in NQ_LEVELS:
                summ[f"{k}_q{int(q * 100)}"] = np.quantile(v, q, axis=0)
            summ[f"{k}_p_spike"] = (v > thr).mean(0)
            s1 = S1[:, :, i]
            summ[f"{k}_lp_mean"] = s1.mean(0)
        summary = pd.DataFrame(summ, index=Ts)
        return ScenarioSet(Ts, vals, summary, dec, kind)


def _make_solver(name: str, cfg: Config):
    """Clearing function ``f(stack_arrays, nd, req, prev) -> result with E, P, C, R, sP,
    sC, sR, unserved, gen``.

    ``engine`` uses :class:`bessbid.engine.ClearingEngine` with cold starts, so a result
    never depends on earlier solves; ``private`` uses :mod:`bessbid._clearlp`; ``auto``
    prefers the engine when it is importable."""
    if name not in ("auto", "engine", "private"):
        raise ValueError("solver must be 'auto', 'engine' or 'private'")
    if name in ("auto", "engine"):
        try:
            from . import engine as _engine
        except ImportError:
            if name == "engine":
                raise
        else:
            eng = _engine.ClearingEngine(cfg, warm_start=False)

            def solve_engine(args, nd, req, prev):
                st = _engine.PeriodStack(*[np.asarray(a, dtype=float) for a in args])
                return eng.clear(st, float(nd), tuple(float(x) for x in req), prev)

            return "engine", solve_engine
    pen = (cfg.shortfall_price_primary, cfg.shortfall_price_contingency, cfg.shortfall_price_regulation)

    def solve_private(args, nd, req, prev):
        return _clearlp.clear(*args, nd, req, prev, ramp=cfg.ramp_up_frac, penalties=pen, voll=cfg.voll)

    return "private", solve_private


# ------------------------------------------------------------------------------------------
# walk-forward evaluation
# ------------------------------------------------------------------------------------------
def _month_cuts(start: pd.Timestamp, end: pd.Timestamp, lead: pd.Timedelta):
    """(cut, first target, last target) per calendar month in [start, end]."""
    out = []
    m = start.normalize().replace(day=1)
    while m <= end:
        nxt = (m + pd.offsets.MonthBegin(1)).normalize()
        a = max(start, m)
        b = min(end, nxt - PERIOD)
        if a <= b:
            out.append((a.normalize() - lead, a, b))
        m = nxt
    return out


def walk_forward(data, cfg: Config | None = None, start="2026-08-01", end="2026-10-31 23:30",
                 kinds=("lead2", "day_ahead"), last_cut=None, scenarios: bool = False,
                 residual_start=None, max_blocks=None, progress=None, **opts) -> pd.DataFrame:
    """Monthly re-fitted forecasts over [start, end] next to actual prices.

    Each month's forecaster is fitted at 00:00 of the day before the month's first target
    day (``da_lead`` before it), so every forecast's decision time is after its fit cut.
    ``last_cut`` caps the fit cuts (used when building a residual library at a cut).
    With ``scenarios`` also adds day-ahead and lead-2 scenario summaries (needs
    ``residual_start`` for the residual library)."""
    info = data if isinstance(data, InfoSet) else InfoSet.from_dataset(data, cfg)
    cfg = cfg or info.cfg
    start, end = _ts(start), _ts(end)
    lead = pd.Timedelta(opts.get("da_lead", pd.Timedelta(days=1)))
    parts = []
    for cut, a, b in _month_cuts(start, end, lead):
        if last_cut is not None:
            cut = min(cut, _ts(last_cut))
        fc = Forecaster(info, cfg, **opts).fit(cut)
        if scenarios and residual_start is not None:
            fc.build_residual_library(residual_start)
        Ts = pd.date_range(a, b, freq="30min")
        cols = {}
        if "lead2" in kinds:
            l2 = fc.lead2_many(Ts)
            for k in KEYS + ("nd", "outage", "sP", "sC", "sR"):
                cols[f"lead2_{k}"] = l2[k]
        if "day_ahead" in kinds:
            da = pd.concat([fc.day_ahead(d) for d in pd.date_range(a.normalize(), b.normalize(), freq="D")])
            da = da.loc[(da.index >= a) & (da.index <= b)]
            for k in KEYS + ("nd", "outage", "sP", "sC", "sR"):
                cols[f"day_ahead_{k}"] = da[k]
        part = pd.DataFrame(cols, index=Ts)
        if scenarios:
            for kind in kinds:
                sm = []
                for d in pd.date_range(a.normalize(), b.normalize(), freq="D"):
                    Td = pd.date_range(d, periods=48, freq="30min")
                    Td = Td[(Td >= a) & (Td <= b)]
                    sm.append(fc.scenarios(Td, kind=kind, max_blocks=max_blocks).summary)
                s = pd.concat(sm)
                s.columns = [f"{kind}_ens_{c}" for c in s.columns]
                part = part.join(s)
        parts.append(part)
        if progress:
            progress(f"walk-forward {a:%Y-%m-%d}..{b:%Y-%m-%d} (cut {cut}) done")
    out = pd.concat(parts)
    # actual prices for scoring only (never fed back into forecasts)
    clr = info.clearing_known_at(end + pd.Timedelta(days=1) if last_cut is None else _ts(last_cut))
    act = clr.set_index("ts")
    for k in KEYS:
        out[f"actual_{k}"] = act[PRICE_COLUMNS[k]].reindex(out.index)
    out["actual_nd"] = act["net_demand_mw"].reindex(out.index)
    return out


def _pr(pred: np.ndarray, obs: np.ndarray):
    tp = int((pred & obs).sum())
    return (tp / max(int(pred.sum()), 1), tp / max(int(obs.sum()), 1), int(obs.sum()), int(pred.sum()))


def evaluate(wf: pd.DataFrame, kinds=("lead2", "day_ahead"), spike_mw: float = SPIKE_MW) -> pd.DataFrame:
    """Accuracy table per kind and product: MAE, RMSE, bias, spike precision/recall
    (point forecast > spike_mw), within-day Spearman (energy), and, when scenario columns
    are present, quantile coverage and P(spike) >= 0.5 precision/recall."""
    rows = []
    for kind in kinds:
        for k in KEYS:
            col = f"{kind}_{k}"
            if col not in wf.columns:
                continue
            d = wf[[col, f"actual_{k}"]].dropna()
            p, a = d[col].to_numpy(), d[f"actual_{k}"].to_numpy()
            e = p - a
            prec, rec, n_obs, n_pred = _pr(p > spike_mw, a > spike_mw)
            r = dict(kind=kind, product=k, n=len(d), MAE=np.abs(e).mean(), RMSE=np.sqrt((e ** 2).mean()),
                     bias=e.mean(), n_spikes=n_obs, n_pred_spikes=n_pred, spike_precision=prec, spike_recall=rec)
            if k == "E":
                dd = d.copy()
                dd["day"] = dd.index.normalize()
                r["day_spearman"] = float(dd.groupby("day")[[col, f"actual_{k}"]].apply(
                    lambda g: g[col].rank().corr(g[f"actual_{k}"].rank())).mean())
            q10, q50, q90 = (f"{kind}_ens_{k}_q10", f"{kind}_ens_{k}_q50", f"{kind}_ens_{k}_q90")
            if q90 in wf.columns:
                dq = wf[[q10, q50, q90, f"{kind}_ens_{k}_p_spike", f"{kind}_ens_{k}_mean", f"actual_{k}"]].dropna()
                aa = dq[f"actual_{k}"].to_numpy()
                r["cov10"] = float((aa < dq[q10]).mean())
                r["cov50"] = float((aa < dq[q50]).mean())
                r["cov90"] = float((aa < dq[q90]).mean())
                r["ens_mean_MAE"] = float(np.abs(dq[f"{kind}_ens_{k}_mean"].to_numpy() - aa).mean())
                pp, rr, _, _ = _pr(dq[f"{kind}_ens_{k}_p_spike"].to_numpy() >= 0.5, aa > spike_mw)
                r["ens_spike_precision"], r["ens_spike_recall"] = pp, rr
                ps = dq[f"{kind}_ens_{k}_p_spike"].to_numpy()
                r["brier"] = float(np.mean((ps - (aa > spike_mw)) ** 2))
            rows.append(r)
        # reserves pooled (any of P/C/R above the spike level)
        cols = [f"{kind}_{k}" for k in "PCR"]
        if all(c in wf.columns for c in cols):
            d = wf[cols + [f"actual_{k}" for k in "PCR"]].dropna()
            p = (d[cols].to_numpy() > spike_mw).ravel()
            a = (d[[f"actual_{k}" for k in "PCR"]].to_numpy() > spike_mw).ravel()
            prec, rec, n_obs, n_pred = _pr(p, a)
            rows.append(dict(kind=kind, product="reserves", n=len(d), n_spikes=n_obs, n_pred_spikes=n_pred,
                             spike_precision=prec, spike_recall=rec))
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------------------
# month forecasts (November run)
# ------------------------------------------------------------------------------------------
def month_forecasts(data, cfg: Config | None = None, month: str | None = None, residual_start="2026-08-01",
                    scenarios: bool = True, progress=None, **opts) -> tuple[pd.DataFrame, "Forecaster"]:
    """All forecasts for a month, each with its own legal decision time.

    Columns ``lead2_*`` (decision T - 65 min), ``day_ahead_*`` (decision 00:00 of D-1),
    ``month_ahead_*`` (one decision for the whole month: the first period's cutoff),
    and with ``scenarios`` the ensemble summaries ``lead2_ens_*`` / ``day_ahead_ens_*``.
    The forecaster is fitted at the earliest of these decision times."""
    info = data if isinstance(data, InfoSet) else InfoSet.from_dataset(data, cfg)
    cfg = cfg or info.cfg
    from .periods import parse_month

    y, m = parse_month(month or cfg.month)
    first = pd.Timestamp(year=y, month=m, day=1)
    Ts = pd.date_range(first, first + pd.offsets.MonthBegin(1) - PERIOD, freq="30min")
    fc = Forecaster(info, cfg, **opts)
    cut = first - fc.opts["da_lead"]
    fc.fit(cut, residual_start=residual_start if scenarios else None)
    if progress:
        progress(f"fitted at {cut} ({fc._solver_name} solver)")
    l2 = fc.lead2_many(Ts)
    da = pd.concat([fc.day_ahead(d) for d in pd.date_range(first, Ts[-1].normalize(), freq="D")])
    ma = fc.day_ahead_periods(Ts, info.cutoff(Ts[0]))
    cols = list(KEYS) + ["sP", "sC", "sR", "unserved", "nd", "outage", "req_C"]
    out = pd.DataFrame(index=Ts)
    out.index.name = "ts"
    for name, d in (("lead2", l2), ("day_ahead", da), ("month_ahead", ma)):
        for c in cols:
            out[f"{name}_{c}"] = d[c].reindex(Ts).to_numpy()
    out["lead2_issue_time"] = l2["issue_time"].reindex(Ts).to_numpy()
    out["lead2_lead_hours"] = l2["lead_hours"].reindex(Ts).to_numpy()
    out["day_ahead_decision"] = da["decision_time"].reindex(Ts).to_numpy()
    out["surge"] = l2["surge"].reindex(Ts).to_numpy()
    out["event"] = l2["event"].reindex(Ts).to_numpy()
    if progress:
        progress("point forecasts done")
    if scenarios:
        for kind in ("lead2", "day_ahead"):
            parts = [fc.scenarios(day=d, kind=kind).summary for d in pd.date_range(first, Ts[-1].normalize(), freq="D")]
            s = pd.concat(parts)
            s.columns = [f"{kind}_ens_{c}" for c in s.columns]
            out = out.join(s)
            if progress:
                progress(f"{kind} scenarios done")
    return out, fc


def summarize_month(fc_frame: pd.DataFrame, spike_mw: float = SPIKE_MW) -> dict:
    """Daily table and headline numbers of :func:`month_forecasts` output."""
    d = fc_frame
    day = d.index.normalize()
    daily = pd.DataFrame({
        "dow": [x.strftime("%a") for x in day.unique()],
    }, index=day.unique())
    for kind in ("day_ahead", "lead2"):
        g = d[f"{kind}_E"].groupby(day)
        daily[f"{kind}_E_mean"] = g.mean()
        daily[f"{kind}_E_max"] = g.max()
        daily[f"{kind}_E_spread"] = g.max() - g.min()
        daily[f"{kind}_R_mean"] = d[f"{kind}_R"].groupby(day).mean()
        daily[f"{kind}_n_E_gt_{int(spike_mw)}"] = (d[f"{kind}_E"] > spike_mw).groupby(day).sum()
        sc = (d[[f"{kind}_sP", f"{kind}_sC", f"{kind}_sR"]].sum(1) > 1e-6)
        daily[f"{kind}_n_reserve_short"] = sc.groupby(day).sum()
        if f"{kind}_ens_E_p_spike" in d.columns:
            daily[f"{kind}_ens_n_p_spike_ge_0.5"] = (d[f"{kind}_ens_E_p_spike"] >= 0.5).groupby(day).sum()
            daily[f"{kind}_ens_E_q90_max"] = d[f"{kind}_ens_E_q90"].groupby(day).max()
    daily["surge_periods"] = d["surge"].groupby(day).sum()
    daily["event_periods"] = d["event"].groupby(day).sum()
    we = np.asarray(daily.index.dayofweek >= 5)
    normal = (daily["event_periods"] == 0).to_numpy()
    head = {
        "periods": int(len(d)),
        "day_ahead_E_mean": float(d["day_ahead_E"].mean()),
        "lead2_E_mean": float(d["lead2_E"].mean()),
        "day_ahead_R_mean": float(d["day_ahead_R"].mean()),
        "weekend_day_ahead_E_mean": float(daily.loc[we, "day_ahead_E_mean"].mean()) if we.any() else None,
        "normal_weekday_day_ahead_E_mean": float(daily.loc[~we & normal, "day_ahead_E_mean"].mean()),
        "normal_weekday_day_ahead_E_spread": float(daily.loc[~we & normal, "day_ahead_E_spread"].mean()),
        "spike_days": [str(x.date()) for x in daily.index[daily["day_ahead_n_E_gt_400"] > 0]],
        "surge_days": [str(x.date()) for x in daily.index[daily["surge_periods"] > 0]],
    }
    return {"headline": head, "daily": daily}


def _main(argv=None) -> int:
    import argparse
    import json
    from pathlib import Path

    from .config import load_config
    from .io import load_all

    ap = argparse.ArgumentParser(prog="python -m bessbid.forecast",
                                 description="Write a month of price forecasts to the work dir.")
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--month", default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--residual-start", default="2026-08-01")
    ap.add_argument("--no-scenarios", action="store_true")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    month = a.month or cfg.month
    work = Path(a.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    ds = load_all(a.data_dir, work)
    info = InfoSet.from_dataset(ds, cfg)
    frame, fc = month_forecasts(info, cfg, month, a.residual_start, scenarios=not a.no_scenarios,
                                progress=lambda s: print(s, flush=True))
    info.assert_no_leak()
    out = work / f"forecasts_{month}.parquet"
    frame.to_parquet(out)
    summ = summarize_month(frame)
    summ["daily"].round(2).to_csv(work / f"forecasts_{month}_daily.csv")
    meta = {"headline": summ["headline"], "forecaster": fc.summary(), "audit": info.audit_summary()}
    (work / f"forecasts_{month}_summary.json").write_text(json.dumps(meta, indent=1, default=str))
    print(json.dumps(summ["headline"], indent=1))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
