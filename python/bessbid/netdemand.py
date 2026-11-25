"""Net-demand forecasts for price forecasting.

* **Lead forecast** for period T: T's own legal pre-dispatch vintage (lead 2 h, else
  lead 3 h; strict reading), ``load - solar`` with forecast solar set to zero in periods of
  the day where actual solar was always zero in training (Phase A: lead-2 MAE 81.9 -> 75.4),
  optionally minus an hour-of-day bias fitted on training rows whose period has ended.
* **Day-ahead profile** for horizons beyond the available vintages: the mean of the same
  period of day over the last ``n_days`` days of the same day type (weekday / weekend),
  outside demand-surge windows, plus the learned surge MW when T is in a surge window.
  The history comes from

  - ``"clearing"``: actual net demand of training periods that ended by the decision time
    (training data; legal under every reading of the rules), or
  - ``"predispatch"``: the lead-0.5 h forecast of past periods issued by the decision time
    (Phase A's DA proxy; uses other periods' pre-dispatch rows, i.e. the broad reading).

  ``"auto"`` picks ``"clearing"`` under the strict pre-dispatch reading and
  ``"predispatch"`` otherwise. If T's own vintage was issued by the decision time it is used
  instead of the profile.

All data goes through the :class:`~bessbid.infoset.InfoSet` accessors.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import Config
from .periods import PERIOD

__all__ = ["NetDemandModel", "NDForecast", "ND_SOURCES"]

ND_SOURCES = ("auto", "clearing", "predispatch")
NQ = 9


def _pod(t) -> np.ndarray:
    t = pd.DatetimeIndex(t)
    return np.asarray(t.hour * 2 + t.minute // 30, dtype=np.int64)


@dataclass(frozen=True)
class NDForecast:
    nd: float
    known_outage_mw: float | None
    issue_time: pd.Timestamp | None
    lead_hours: float | None
    source: str  # 'vintage' | 'profile:<src>'


class NetDemandModel:
    def __init__(self, infoset, calendar, cfg: Config | None = None, *, source: str = "auto",
                 bias_correction: bool = False, n_days: int = 4, zero_night_solar: bool = True,
                 da_lead: pd.Timedelta = pd.Timedelta(days=1), node_days: int = 60,
                 history_cap: bool = False):
        if source not in ND_SOURCES:
            raise ValueError(f"source must be one of {ND_SOURCES}")
        self.info = infoset
        self.cal = calendar
        self.cfg = cfg or infoset.cfg
        if source == "auto":
            source = "clearing" if self.cfg.strict_predispatch else "predispatch"
        self.source = source
        self.bias_correction = bias_correction
        self.n_days = n_days
        self.zero_night_solar = zero_night_solar
        self.da_lead = pd.Timedelta(da_lead)
        self.node_days = node_days
        #: Evaluation aid: limit the profile history to data known at the fit cut, as in
        #: November where no actuals exist after Oct 31 (no effect on November runs).
        self.history_cap = history_cap
        self.cut: pd.Timestamp | None = None
        self.night = np.zeros(48, bool)
        self.bias: dict[float, np.ndarray] = {}
        self.nodes_lead: np.ndarray = np.zeros(NQ)
        self.nodes_da: np.ndarray = np.zeros(NQ)
        self.stats: dict = {}

    # ---- fit ---------------------------------------------------------------------------------
    def fit(self, cut) -> "NetDemandModel":
        cut = pd.Timestamp(cut)
        self.cut = None  # historical decision times are allowed while fitting
        p = self.info.predispatch_known_at(cut)
        act = p.dropna(subset=["synthetic_actual_load_mw", "synthetic_actual_solar_mw"]) if \
            "synthetic_actual_solar_mw" in p.columns else p.iloc[:0]
        if len(act):
            pods = _pod(act["ts"])
            mx = pd.Series(act["synthetic_actual_solar_mw"].to_numpy()).groupby(pods).max()
            n = pd.Series(pods).value_counts()
            night = np.zeros(48, bool)
            for k in range(48):
                night[k] = (k in mx.index) and mx[k] <= 0 and n.get(k, 0) >= 7
            self.night = night
        actual = (act["synthetic_actual_load_mw"] - act["synthetic_actual_solar_mw"]).to_numpy() if len(act) else np.array([])
        if len(act):
            fc = self._nd_from_rows(act)
            err = fc - actual
            leads = act["lead_hours"].to_numpy()
            pods = _pod(act["ts"])
            for L in np.unique(leads):
                m = leads == L
                b = pd.Series(err[m]).groupby(pods[m]).mean().reindex(range(48)).fillna(0.0).to_numpy()
                self.bias[float(L)] = b
            # lead-forecast error nodes (forecast - actual), preferred lead, after bias
            L0 = float(self.cfg.predispatch_leads[0])
            m = leads == L0
            e = err[m] - (self.bias[L0][pods[m]] if self.bias_correction else 0.0)
            self.nodes_lead = np.quantile(e, (np.arange(NQ) + 0.5) / NQ) if m.any() else np.zeros(NQ)
            self.stats["lead_mae"] = float(np.abs(e).mean()) if m.any() else None
        # day-ahead error nodes from the profile over the last node_days ended days
        self.nodes_da = self._da_nodes(cut)
        self.cut = cut
        self.info.record("netdemand_fit", cut, (act["ts"].max() + PERIOD) if len(act) else None, len(act))
        return self

    def _da_nodes(self, cut: pd.Timestamp) -> np.ndarray:
        clr = self.info.clearing_known_at(cut)
        if clr.empty:
            return np.zeros(NQ)
        last_day = (pd.Timestamp(clr["ts"].max()) + PERIOD).normalize() - pd.Timedelta(days=1)
        days = pd.date_range(last_day - pd.Timedelta(days=self.node_days - 1), last_day, freq="D")
        act = clr.set_index("ts")["net_demand_mw"]
        errs = []
        for d in days:
            dec = d - self.da_lead
            if dec < act.index.min() + pd.Timedelta(days=7):
                continue
            Ts = pd.date_range(d, periods=48, freq="30min")
            fc = self.profile(Ts, dec, use_vintage=False)
            a = act.reindex(Ts).to_numpy()
            errs.append(fc - a)
        if not errs:
            return np.zeros(NQ)
        e = np.concatenate(errs)
        e = e[np.isfinite(e)]
        self.stats["da_mae"] = float(np.abs(e).mean())
        return np.quantile(e, (np.arange(NQ) + 0.5) / NQ)

    # ---- helpers -----------------------------------------------------------------------------
    def _nd_from_rows(self, rows: pd.DataFrame) -> np.ndarray:
        solar = rows["forecast_solar_mw"].to_numpy(float).copy()
        if self.zero_night_solar:
            solar[self.night[_pod(rows["ts"])]] = 0.0
        return rows["forecast_load_mw"].to_numpy(float) - solar

    def _debias(self, nd: np.ndarray, ts, lead) -> np.ndarray:
        if not self.bias_correction:
            return nd
        pods = _pod(ts)
        lead = np.broadcast_to(np.asarray(lead, float), nd.shape)
        out = nd.copy()
        for L in np.unique(lead):
            b = self.bias.get(float(L))
            if b is not None:
                m = lead == L
                out[m] = nd[m] - b[pods[m]]
        return out

    # ---- lead forecasts ------------------------------------------------------------------------
    def lead_many(self, Ts) -> pd.DataFrame:
        """T's own legal vintage for each T (indexed by ts): nd, known outage, issue, lead."""
        rows = self.info.predispatch_for_many(Ts)
        if rows.empty:
            return pd.DataFrame(columns=["nd", "known_outage_mw", "issue_time", "lead_hours"])
        r = rows.reset_index()
        nd = self._debias(self._nd_from_rows(r), r["ts"], r["lead_hours"].to_numpy())
        return pd.DataFrame({
            "nd": nd, "known_outage_mw": r["known_outage_mw_at_issue"].to_numpy(float),
            "issue_time": r["issue_time"].to_numpy(), "lead_hours": r["lead_hours"].to_numpy(float),
        }, index=pd.DatetimeIndex(r["ts"], name="ts"))

    def lead(self, T) -> NDForecast | None:
        d = self.lead_many([pd.Timestamp(T)])
        if d.empty:
            return None
        r = d.iloc[0]
        return NDForecast(float(r.nd), float(r.known_outage_mw), pd.Timestamp(r.issue_time), float(r.lead_hours), "vintage")

    # ---- day-ahead profile -----------------------------------------------------------------------
    def _history(self, decision_time: pd.Timestamp) -> pd.Series:
        """Net-demand history usable at the decision time, indexed by period start."""
        d = decision_time
        if self.history_cap and self.cut is not None:
            d = min(d, self.cut)
        if self.source == "clearing":
            c = self.info.clearing_known_at(d)
            return pd.Series(c["net_demand_mw"].to_numpy(float), index=pd.DatetimeIndex(c["ts"]))
        p = self.info.predispatch_known_at(d)
        p = p[p["lead_hours"] == p["lead_hours"].min()]
        p = p.drop_duplicates("ts", keep="last")
        return pd.Series(self._nd_from_rows(p), index=pd.DatetimeIndex(p["ts"])).sort_index()

    def profile(self, Ts, decision_time, use_vintage: bool = True) -> np.ndarray:
        """Day-ahead net-demand forecast for periods ``Ts`` made at ``decision_time``."""
        Ts = pd.DatetimeIndex(Ts)
        d = pd.Timestamp(decision_time)
        if self.cut is not None and d < self.cut:
            raise ValueError(f"decision time {d} is before the fit cut {self.cut}")
        h = self._history(d)
        h = h[~self.cal.is_surge(h.index.to_numpy(dtype="datetime64[ns]"))]
        hp = _pod(h.index)
        hwe = np.asarray(h.index.dayofweek >= 5)
        out = np.full(len(Ts), np.nan)
        pods = _pod(Ts)
        we = np.asarray(Ts.dayofweek >= 5)
        vals = h.to_numpy()
        for k in range(len(Ts)):
            m = (hp == pods[k]) & (hwe == we[k])
            v = vals[m][-self.n_days:]
            if len(v) == 0:
                m = hp == pods[k]
                v = vals[m][-self.n_days:]
            out[k] = v.mean() if len(v) else np.nan
        out = out + self.cal.surge_mw(Ts.to_numpy(dtype="datetime64[ns]"))
        if use_vintage:
            # T's own vintage when it was already issued by the decision time
            vin = self._own_vintages_by(Ts, d)
            for k, T in enumerate(Ts):
                if T in vin.index:
                    out[k] = vin.at[T, "nd"]
        return out

    def _own_vintages_by(self, Ts: pd.DatetimeIndex, d: pd.Timestamp) -> pd.DataFrame:
        """T's own strict vintages issued by ``d`` (none when ``d`` is a day or more ahead)."""
        early = Ts[Ts - pd.Timedelta(hours=max(self.cfg.predispatch_leads)) <= d]
        if len(early) == 0:
            return pd.DataFrame(columns=["nd"])
        r = self.info.predispatch_for_many(early)
        if r.empty:
            return pd.DataFrame(columns=["nd"])
        r = r[r["issue_time"] <= d].reset_index()
        if r.empty:
            return pd.DataFrame(columns=["nd"])
        nd = self._debias(self._nd_from_rows(r), r["ts"], r["lead_hours"].to_numpy())
        return pd.DataFrame({"nd": nd}, index=pd.DatetimeIndex(r["ts"]))

    def known_outage_at(self, decision_time) -> tuple[float, pd.Timestamp] | None:
        """Latest known-outage state issued by the decision time (broad reading: other
        periods' rows). Returns (MW, issue time) or None."""
        p = self.info.predispatch_known_at(pd.Timestamp(decision_time))
        if p.empty:
            return None
        r = p.iloc[-1]
        return float(r["known_outage_mw_at_issue"]), pd.Timestamp(r["issue_time"])

    def summary(self) -> dict:
        return {
            "cut": str(self.cut), "source": self.source, "bias_correction": self.bias_correction,
            "night_periods_of_day": [int(k) for k in np.flatnonzero(self.night)],
            "nodes_lead": [round(float(x), 1) for x in self.nodes_lead],
            "nodes_da": [round(float(x), 1) for x in self.nodes_da],
            **{k: v for k, v in self.stats.items()},
        }
