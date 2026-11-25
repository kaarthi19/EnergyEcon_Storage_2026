"""Offer-stack model: dense stack arrays, a factor model of offer prices, and the legal
lag-blend stack forecast.

Phase A found that generator offers have a fixed quantity structure (energy bands and
reserve offers are fixed fractions of available capacity; outaged units offer nothing) and
that log offer prices follow

    log p[t, f, b] = base(f, b, period-of-day) + factor(t) + noise,
    factor(t)      = a + beta * outage_mw(t) + w * weekend(t)   (one fit per product).

The stack for a future period T is forecast from the latest *published* stacks for the same
period of day (stack publication lags >= 29 days, enforced per row through the InfoSet),
normalised by the factor model to zero outage and weekday, averaged in logs over up to
``n_lags`` days, and re-scaled to T's expected outage and day type. Outaged units are
restored to their typical (non-outage) offer quantities; the expected outage for T is then
applied by the forecast module as a demand shift (Phase A's B7 recipe).

Data access goes only through :meth:`InfoSet.stack_index_known_at`, which returns the rows
published by a decision time and records the access in the audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import Config

__all__ = ["StackArrays", "FactorParams", "StackForecast", "StackModel", "STATUS_CODES"]

STATUS_CODES = {"none": 0, "planned": 1, "forced": 2, "derated": 3}
_PRODUCTS = ("Energy", "Primary", "Contingency", "Regulation")
_KEYS = ("E", "P", "C", "R")


@dataclass
class StackArrays:
    """Dense (period x facility x band) view of the offer stacks.

    Energy arrays are (n, F, B); reserve arrays are (n, F) (one band each); ``avail`` is
    the time each period's stack becomes usable (latest row of that period)."""

    ts: np.ndarray  # datetime64[ns], sorted, unique
    avail: np.ndarray  # datetime64[ns]
    facilities: list
    Eq: np.ndarray
    Ep: np.ndarray
    Pq: np.ndarray
    Pp: np.ndarray
    Cq: np.ndarray
    Cp: np.ndarray
    Rq: np.ndarray
    Rp: np.ndarray
    av: np.ndarray
    stat: np.ndarray
    pod: np.ndarray = field(init=False)
    weekend: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        t = pd.DatetimeIndex(self.ts)
        self.pod = np.asarray(t.hour * 2 + t.minute // 30, dtype=np.int64)
        self.weekend = np.asarray(t.dayofweek >= 5, dtype=float)

    @property
    def n(self) -> int:
        return len(self.ts)

    @property
    def F(self) -> int:
        return len(self.facilities)

    def prices(self, key: str) -> np.ndarray:
        return getattr(self, key + "p")

    def quantities(self, key: str) -> np.ndarray:
        return getattr(self, key + "q")

    @classmethod
    def from_frame(cls, stacks: pd.DataFrame, avail_col: str = "avail_time") -> "StackArrays":
        """Build from the tidy frame of :func:`bessbid.io.load_stacks` (``avail_col`` falls
        back to ``pub`` when absent)."""
        s = stacks
        ts = np.sort(s["ts"].unique().astype("datetime64[ns]"))
        facs = sorted(pd.Series(s["facility_id"].astype(str)).unique())
        fi = pd.Series(range(len(facs)), index=facs)
        ti = np.searchsorted(ts, s["ts"].to_numpy(dtype="datetime64[ns]"))
        fx = fi.reindex(s["facility_id"].astype(str).to_numpy()).to_numpy()
        prod = s["product"].astype(str).to_numpy()
        band = s["band"].to_numpy(dtype=np.int64)
        n, F = len(ts), len(facs)
        B = int(band[prod == "Energy"].max())
        out = {}
        for p, k in zip(_PRODUCTS, _KEYS):
            m = prod == p
            nb = B if p == "Energy" else 1
            if p != "Energy" and m.any() and band[m].max() > 1:
                raise ValueError(f"{p} stacks have more than one band; not supported")
            q = np.zeros((n, F, nb))
            pr = np.full((n, F, nb), np.nan)
            q[ti[m], fx[m], band[m] - 1] = s["offered_mw"].to_numpy(float)[m]
            pr[ti[m], fx[m], band[m] - 1] = s["price"].to_numpy(float)[m]
            if p != "Energy":
                q, pr = q[:, :, 0], pr[:, :, 0]
            out[k + "q"], out[k + "p"] = q, pr
        e = (prod == "Energy") & (band == 1)
        av = np.zeros((n, F))
        av[ti[e], fx[e]] = s["available_mw"].to_numpy(float)[e]
        stat = np.zeros((n, F), np.int8)
        stat[ti[e], fx[e]] = (
            pd.Series(s["outage_status"].astype(str).to_numpy()[e]).map(STATUS_CODES).fillna(0).to_numpy(np.int8)
        )
        col = avail_col if avail_col in s.columns else "pub"
        av_t = pd.Series(s[col].to_numpy(dtype="datetime64[ns]")).groupby(ti).max()
        avail = np.full(n, np.datetime64("NaT", "ns"), dtype="datetime64[ns]")
        avail[av_t.index.to_numpy()] = av_t.to_numpy(dtype="datetime64[ns]")
        # A missing price can only belong to a band that offers nothing; give it a harmless
        # price so logs stay finite.
        for k in _KEYS:
            pr = out[k + "p"]
            bad = np.isnan(pr)
            if bad.any():
                if (out[k + "q"][bad] > 0).any():
                    raise ValueError(f"{k} stack has offered MW without a price")
                pr[bad] = np.nanmedian(pr) if np.isfinite(pr).any() else 1.0
        return cls(ts=ts, avail=avail, facilities=facs, av=av, stat=stat, **out)


@dataclass(frozen=True)
class FactorParams:
    beta_out: float  # log-price change per MW of outage
    beta_we: float  # weekend effect (log)
    resid_sd: float
    base: np.ndarray  # (48, F, B) or (48, F): mean log price by period of day


@dataclass
class StackForecast:
    T: pd.Timestamp
    decision_time: pd.Timestamp
    outage_mw: float
    Eq: np.ndarray
    Ep: np.ndarray
    Pq: np.ndarray
    Pp: np.ndarray
    Cq: np.ndarray
    Cp: np.ndarray
    Rq: np.ndarray
    Rp: np.ndarray
    av: np.ndarray
    lag_ts: list  # stack periods blended (most recent first)
    newest_avail: pd.Timestamp | None = None  # publication time of the newest stack used

    def lp_args(self) -> tuple:
        return (self.Eq, self.Ep, self.Pq, self.Pp, self.Cq, self.Cp, self.Rq, self.Rp, self.av)


class StackModel:
    """Factor model + legal lag-blend stack forecast.

    ``fit(cut)`` uses only stacks published by ``cut``; every later forecast must have a
    decision time >= ``cut`` (checked)."""

    def __init__(self, infoset, cfg: Config | None = None, n_lags: int = 7, max_lag_days: int = 120,
                 copy_lag_days: int = 28):
        self.info = infoset
        self.cfg = cfg or infoset.cfg
        self.n_lags = int(n_lags)
        self.max_lag_days = int(max_lag_days)
        self.copy_lag_days = int(copy_lag_days)
        self.arr: StackArrays = infoset.stack_arrays()
        self.cut: pd.Timestamp | None = None
        self.params: dict[str, FactorParams] = {}
        self.nom: np.ndarray | None = None
        self.typ: dict[str, np.ndarray] = {}
        self.fit_index: np.ndarray | None = None
        self.copied: np.ndarray | None = None

    # ---- fitting -------------------------------------------------------------------------
    def _copies(self, idx: np.ndarray) -> np.ndarray:
        """Legal periods whose stack is an exact copy of a legal period ``copy_lag_days``
        later (EMA's June stacks repeat July). Only rows already published are compared."""
        a = self.arr
        ts = a.ts[idx]
        later = ts + np.timedelta64(self.copy_lag_days, "D")
        pos = np.searchsorted(ts, later)
        ok = (pos < len(ts))
        ok[ok] = ts[pos[ok]] == later[ok]
        out = np.zeros(len(idx), bool)
        for j in np.flatnonzero(ok):
            i0, i1 = idx[j], idx[pos[j]]
            if (np.array_equal(a.Ep[i0], a.Ep[i1]) and np.array_equal(a.Eq[i0], a.Eq[i1])
                    and np.array_equal(a.Rp[i0], a.Rp[i1]) and np.array_equal(a.av[i0], a.av[i1])):
                out[j] = True
        return idx[out]

    def outage_of(self, i) -> np.ndarray:
        """Stack outage (MW below nominal) of stack period(s) ``i``."""
        return (self.nom[None, :] - self.arr.av[np.atleast_1d(i)]).clip(min=0).sum(1)

    def fit(self, cut) -> "StackModel":
        cut = pd.Timestamp(cut)
        a = self.arr
        idx = self.info.stack_index_known_at(cut)
        if len(idx) < 48:
            raise ValueError(f"fewer than 48 stack periods published by {cut}")
        copied = self._copies(idx)
        fit_idx = np.setdiff1d(idx, copied)
        self.cut, self.fit_index, self.copied = cut, fit_idx, copied
        self.nom = a.av[idx].max(0)
        ok = a.stat[fit_idx] == 0
        for k in _KEYS:
            q = a.quantities(k)[fit_idx]
            typ = np.zeros(q.shape[1:])
            for f in range(a.F):
                rows = q[ok[:, f], f]
                typ[f] = np.median(rows, 0) if len(rows) else np.median(q[:, f], 0)
            self.typ[k] = typ
        out = self.outage_of(fit_idx)
        we = a.weekend[fit_idx]
        pod = a.pod[fit_idx]
        X = np.c_[np.ones(len(fit_idx)), out, we]
        for k in _KEYS:
            L = np.log(np.maximum(a.prices(k)[fit_idx], 0.01))
            base = np.zeros((48,) + L.shape[1:])
            for p in range(48):
                m = pod == p
                base[p] = L[m].mean(0) if m.any() else L.mean(0)
            dev = (L - base[pod]).reshape(len(fit_idx), -1)
            fac = np.median(dev, 1)
            b = np.linalg.lstsq(X, fac, rcond=None)[0]
            self.params[k] = FactorParams(float(b[1]), float(b[2]), float(np.std(fac - X @ b)), base)
        return self

    # ---- forecasting ---------------------------------------------------------------------
    def _check(self, decision_time: pd.Timestamp) -> None:
        if self.cut is None:
            raise RuntimeError("StackModel.fit() first")
        if decision_time < self.cut:
            raise ValueError(f"decision time {decision_time} is before the fit cut {self.cut}")

    def lag_indices(self, T, decision_time) -> np.ndarray:
        """Most recent published stack periods with T's period of day (up to n_lags)."""
        T, d = pd.Timestamp(T), pd.Timestamp(decision_time)
        self._check(d)
        a = self.arr
        idx = self.info.stack_index_known_at(d, for_period=T)
        pod = T.hour * 2 + T.minute // 30
        lo = np.datetime64(T - pd.Timedelta(days=self.max_lag_days), "ns")
        sel = idx[(a.pod[idx] == pod) & (a.ts[idx] < np.datetime64(T, "ns")) & (a.ts[idx] >= lo)]
        return sel[::-1][: self.n_lags]

    def forecast_stack(self, T, decision_time, outage_mw: float = 0.0) -> StackForecast:
        """Forecast stack for period T (all units restored) with prices scaled to
        ``outage_mw`` of expected outage and T's day type."""
        T, d = pd.Timestamp(T), pd.Timestamp(decision_time)
        idx = self.lag_indices(T, d)
        if len(idx) == 0:
            raise ValueError(f"no published stack for {T:%H:%M} by {d}")
        a = self.arr
        out_l = self.outage_of(idx)
        we_l = a.weekend[idx]
        we_T = float(T.dayofweek >= 5)
        prices = {}
        for k in _KEYS:
            par = self.params[k]
            L = np.log(np.maximum(a.prices(k)[idx], 0.01))
            shp = (len(idx),) + (1,) * (L.ndim - 1)
            norm = L - (par.beta_out * out_l + par.beta_we * we_l).reshape(shp)
            prices[k] = np.exp(norm.mean(0) + par.beta_out * float(outage_mw) + par.beta_we * we_T)
        return StackForecast(
            T=T, decision_time=d, outage_mw=float(outage_mw),
            Eq=self.typ["E"].copy(), Ep=prices["E"],
            Pq=self.typ["P"].copy(), Pp=prices["P"],
            Cq=self.typ["C"].copy(), Cp=prices["C"],
            Rq=self.typ["R"].copy(), Rp=prices["R"],
            av=self.nom.copy(),
            lag_ts=[pd.Timestamp(x) for x in a.ts[idx]],
            newest_avail=pd.Timestamp(a.avail[idx].max()),
        )

    def summary(self) -> dict:
        a = self.arr
        return {
            "cut": str(self.cut),
            "n_fit_periods": int(len(self.fit_index)),
            "n_copied_excluded": int(len(self.copied)),
            "fit_span": [str(pd.Timestamp(a.ts[self.fit_index].min())), str(pd.Timestamp(a.ts[self.fit_index].max()))],
            "nominal_mw": float(self.nom.sum()),
            "params": {k: {"beta_out": p.beta_out, "beta_we": p.beta_we, "resid_sd": p.resid_sd}
                       for k, p in self.params.items()},
        }
