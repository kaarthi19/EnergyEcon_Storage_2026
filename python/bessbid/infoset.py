"""The information set: the only door through which strategies may read market data.

EMA's rules (violations can disqualify):

* pre-dispatch rows for a period starting at ``T`` may be used only if
  ``issue_time <= T - 65 min``. The strict reading (default) also limits them to period T's
  own rows: lead 2 h, else lead 3 h (lead 1 h and 0.5 h are issued after the gate);
* an offer-stack row may be used only once published: ``pub <= T - 65 min``
  (``pub`` = assumed_publication_time_sgt = ts + 28 d 8 h, so stacks lag >= 29 days);
* a training clearing row may be used only once its period has ended
  (``ts + 30 min <= decision time``).

Every accessor checks the data it hands out against its decision time, raises
:class:`LeakError` if anything is newer, and appends an :class:`AuditEntry` holding the
latest timestamp it used. ``InfoSet.assert_no_leak()`` re-checks the whole log, and
``audit_frame()`` returns it for the run manifest.

Compute every per-period feature through the accessors with that period's own cutoff, never
from a month-wide frame.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from .config import Config
from .periods import PERIOD

__all__ = ["InfoSet", "AuditEntry", "LeakError"]


class LeakError(AssertionError):
    """Data newer than the decision time was about to be used."""


@dataclass(frozen=True)
class AuditEntry:
    accessor: str
    decision_time: pd.Timestamp
    max_time_used: pd.Timestamp | None  # None when no row was returned
    n_rows: int
    for_period: pd.Timestamp | None = None

    @property
    def ok(self) -> bool:
        return self.max_time_used is None or self.max_time_used <= self.decision_time


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is not None:
        t = t.tz_convert("Asia/Singapore").tz_localize(None)
    return t


class InfoSet:
    """Time-gated access to clearing prices, pre-dispatch forecasts and offer stacks.

    Parameters are the frames returned by :mod:`bessbid.io` (any may be ``None``). The
    pre-dispatch frames are concatenated; a ``source`` column says which file a row came
    from. Returned frames must be treated as read-only.
    """

    def __init__(
        self,
        cfg: Config | None = None,
        clearing: pd.DataFrame | None = None,
        predispatch_train: pd.DataFrame | None = None,
        predispatch_test: pd.DataFrame | None = None,
        stacks: pd.DataFrame | None = None,
    ) -> None:
        self.cfg = cfg or Config()
        self.gate = pd.Timedelta(minutes=self.cfg.gate_closure_min)
        self._audit: list[AuditEntry] = []

        # Clearing: sorted by period end.
        if clearing is not None:
            c = clearing.sort_values("ts", kind="mergesort").reset_index(drop=True)
            self._clr = c
            self._clr_end = (c["ts"] + PERIOD).to_numpy()
        else:
            self._clr, self._clr_end = None, None

        # Pre-dispatch: one frame, sorted by issue time (for known_at) with a ts index.
        parts = []
        for name, df in (("train", predispatch_train), ("test", predispatch_test)):
            if df is not None and len(df):
                d = df.copy()
                d["source"] = name
                parts.append(d)
        if parts:
            p = pd.concat(parts, ignore_index=True)
            dup = p.duplicated(["ts", "issue_time"], keep=False)
            if dup.any():
                # Same vintage in both files (e.g. an overlap): keep the first, but only if
                # the forecast values agree.
                cols = ["ts", "issue_time", "forecast_load_mw", "forecast_solar_mw", "known_outage_mw_at_issue"]
                if p.loc[dup, cols].drop_duplicates().duplicated(["ts", "issue_time"]).any():
                    raise ValueError("train and test pre-dispatch disagree on the same (ts, issue_time)")
                p = p.drop_duplicates(["ts", "issue_time"], keep="first")
            p = p.sort_values(["issue_time", "ts"], kind="mergesort").reset_index(drop=True)
            self._pd = p
            self._pd_issue = p["issue_time"].to_numpy()
            self._pd_actual_cols = [c for c in p.columns if c.startswith("synthetic_actual_")]
        else:
            self._pd, self._pd_issue, self._pd_actual_cols = None, None, []

        # Stacks: sorted by the time each row becomes available under the configured rule.
        if stacks is not None:
            avail = self._stack_avail(stacks)
            order = np.argsort(avail, kind="mergesort")
            s = stacks.iloc[order].reset_index(drop=True)
            s["avail_time"] = avail[order]
            self._stk = s
            self._stk_avail = s["avail_time"].to_numpy()
        else:
            self._stk, self._stk_avail = None, None
        self._stk_arrays = None

    # ---- construction helpers ------------------------------------------------------------
    @classmethod
    def from_dataset(cls, ds, cfg: Config | None = None) -> "InfoSet":
        return cls(cfg, ds.clearing, ds.predispatch_train, ds.predispatch_test, ds.stacks)

    def _stack_avail(self, stacks: pd.DataFrame) -> np.ndarray:
        rule = self.cfg.stack_pub_rule
        pub = stacks["pub"].to_numpy(dtype="datetime64[ns]")
        alt = (stacks["ts"] + pd.Timedelta(days=28)).to_numpy(dtype="datetime64[ns]")
        if rule == "column":
            return pub
        if rule == "ts_plus_28d":
            return alt
        return np.maximum(pub, alt)

    # ---- audit -------------------------------------------------------------------------
    def _record(self, accessor: str, t: pd.Timestamp, max_used, n: int, for_period=None) -> None:
        mu = None if max_used is None or pd.isna(max_used) else pd.Timestamp(max_used)
        e = AuditEntry(accessor, t, mu, int(n), for_period)
        if not e.ok:
            raise LeakError(f"{accessor}: data up to {mu} used for decision time {t}")
        self._audit.append(e)

    def record(self, accessor: str, decision_time, max_time_used, n_rows: int, for_period=None) -> None:
        """Append an audit entry for data a caller derived from earlier accessor calls
        (raises :class:`LeakError` if ``max_time_used`` is after ``decision_time``)."""
        fp = None if for_period is None else _ts(for_period)
        self._record(accessor, _ts(decision_time), max_time_used, n_rows, fp)

    @property
    def audit(self) -> list[AuditEntry]:
        return list(self._audit)

    def audit_frame(self) -> pd.DataFrame:
        cols = ["accessor", "decision_time", "max_time_used", "n_rows", "for_period"]
        return pd.DataFrame([[getattr(e, c) for c in cols] for e in self._audit], columns=cols)

    def audit_summary(self) -> dict:
        df = self.audit_frame()
        out = {"entries": int(len(df)), "leaks": int(sum(not e.ok for e in self._audit))}
        if len(df):
            lag = (df["decision_time"] - df["max_time_used"]).dropna()
            for acc, g in df.groupby("accessor"):
                gl = (g["decision_time"] - g["max_time_used"]).dropna()
                out[acc] = {
                    "calls": int(len(g)),
                    "min_margin_min": float(gl.min().total_seconds() / 60) if len(gl) else None,
                }
            out["min_margin_min"] = float(lag.min().total_seconds() / 60) if len(lag) else None
        return out

    def assert_no_leak(self) -> None:
        bad = [e for e in self._audit if not e.ok]
        if bad:
            raise LeakError(f"{len(bad)} audit entries use data after their decision time: {bad[:3]}")

    def clear_audit(self) -> None:
        self._audit.clear()

    # ---- gate ----------------------------------------------------------------------------
    def cutoff(self, T) -> pd.Timestamp:
        """Latest information time for an offer for the period starting at ``T``."""
        return _ts(T) - self.gate

    # ---- pre-dispatch ------------------------------------------------------------------
    def _need_pd(self) -> pd.DataFrame:
        if self._pd is None:
            raise ValueError("InfoSet has no pre-dispatch data")
        return self._pd

    def predispatch_known_at(self, t) -> pd.DataFrame:
        """All pre-dispatch rows (any period) issued at or before ``t``.

        Actual load/solar (training only) are masked to NaN unless their period has ended
        by ``t``."""
        p = self._need_pd()
        t = _ts(t)
        k = int(np.searchsorted(self._pd_issue, np.datetime64(t, "ns"), side="right"))
        out = p.iloc[:k].copy()
        max_used = out["issue_time"].max() if k else None
        if self._pd_actual_cols and k:
            hidden = (out["ts"] + PERIOD) > t
            out.loc[hidden, self._pd_actual_cols] = np.nan
            shown = out.loc[~hidden, "ts"]
            if len(shown):
                max_used = max(max_used, shown.max() + PERIOD)
        self._record("predispatch_known_at", t, max_used, len(out))
        return out

    def _own_vintages(self, Ts: np.ndarray, strict: bool) -> pd.DataFrame:
        """Rows of the requested periods with issue <= their own cutoff, ranked."""
        p = self._need_pd()
        want = pd.DataFrame({"ts": Ts.astype("datetime64[ns]")})
        rows = p.merge(want.drop_duplicates(), on="ts", how="inner")
        rows = rows[rows["issue_time"] <= rows["ts"] - self.gate]
        if strict:
            leads = list(self.cfg.predispatch_leads)
            rows = rows[rows["lead_hours"].isin(leads)].copy()
            rank = {l: i for i, l in enumerate(leads)}
            rows["_rank"] = rows["lead_hours"].map(rank)
        else:
            rows = rows.copy()
            rows["_rank"] = -rows["issue_time"].astype("int64")  # latest issue first
        rows = rows.drop(columns=self._pd_actual_cols)
        return rows.sort_values(["ts", "_rank"], kind="mergesort")

    def predispatch_vintages(self, T, strict: bool | None = None) -> pd.DataFrame:
        """All of period T's own rows usable at T's cutoff, preferred first."""
        strict = self.cfg.strict_predispatch if strict is None else strict
        T = _ts(T)
        rows = self._own_vintages(np.array([np.datetime64(T, "ns")]), strict).drop(columns="_rank")
        self._record("predispatch_for", self.cutoff(T), rows["issue_time"].max() if len(rows) else None, len(rows), T)
        return rows.reset_index(drop=True)

    def predispatch_for(self, T, strict: bool | None = None) -> pd.Series | None:
        """Period T's preferred usable forecast row (lead 2 h, else lead 3 h), or None."""
        rows = self.predispatch_vintages(T, strict)
        return None if rows.empty else rows.iloc[0]

    def predispatch_for_many(self, Ts: Iterable, strict: bool | None = None) -> pd.DataFrame:
        """Vectorised :meth:`predispatch_for`: one row per period that has a usable
        forecast, indexed by ``ts``. Each period is gated by its own cutoff."""
        strict = self.cfg.strict_predispatch if strict is None else strict
        Ts = pd.DatetimeIndex([_ts(t) for t in Ts]).to_numpy(dtype="datetime64[ns]")
        rows = self._own_vintages(Ts, strict)
        best = rows.drop_duplicates("ts", keep="first").drop(columns="_rank").set_index("ts")
        for T in pd.DatetimeIndex(np.unique(Ts)):
            if T in best.index:
                self._record("predispatch_for", self.cutoff(T), best.at[T, "issue_time"], 1, T)
            else:
                self._record("predispatch_for", self.cutoff(T), None, 0, T)
        return best

    # ---- stacks ------------------------------------------------------------------------
    def stacks_known_at(self, t) -> pd.DataFrame:
        """Offer-stack rows published at or before ``t`` (column ``avail_time``)."""
        if self._stk is None:
            raise ValueError("InfoSet has no offer-stack data")
        t = _ts(t)
        k = int(np.searchsorted(self._stk_avail, np.datetime64(t, "ns"), side="right"))
        out = self._stk.iloc[:k]
        self._record("stacks_known_at", t, out["avail_time"].iloc[-1] if k else None, k)
        return out

    def stacks_for(self, T) -> pd.DataFrame:
        return self.stacks_known_at(self.cutoff(T))

    def stack_arrays(self):
        """Dense :class:`bessbid.stackmodel.StackArrays` view of all stacks (built once).

        Callers must index it only with rows from :meth:`stack_index_known_at`."""
        if self._stk is None:
            raise ValueError("InfoSet has no offer-stack data")
        if self._stk_arrays is None:
            from .stackmodel import StackArrays

            self._stk_arrays = StackArrays.from_frame(self._stk, "avail_time")
        return self._stk_arrays

    def stack_index_known_at(self, t, for_period=None) -> np.ndarray:
        """Row indices into :meth:`stack_arrays` of the stack periods fully published at or
        before ``t`` (audited)."""
        arr = self.stack_arrays()
        t = _ts(t)
        idx = np.flatnonzero(arr.avail <= np.datetime64(t, "ns"))
        mx = arr.avail[idx].max() if len(idx) else None
        self._record("stack_index_known_at", t, mx, len(idx), None if for_period is None else _ts(for_period))
        return idx

    def latest_stack_ts_known_at(self, t) -> pd.Timestamp | None:
        """Latest trading period whose stack is published by ``t`` (no data returned)."""
        if self._stk is None:
            raise ValueError("InfoSet has no offer-stack data")
        t = _ts(t)
        k = int(np.searchsorted(self._stk_avail, np.datetime64(t, "ns"), side="right"))
        if k == 0:
            return None
        return pd.Timestamp(self._stk["ts"].iloc[:k].max())

    # ---- clearing ----------------------------------------------------------------------
    def clearing_known_at(self, t) -> pd.DataFrame:
        """Training clearing rows whose period ended at or before ``t``."""
        if self._clr is None:
            raise ValueError("InfoSet has no clearing data")
        t = _ts(t)
        k = int(np.searchsorted(self._clr_end, np.datetime64(t, "ns"), side="right"))
        out = self._clr.iloc[:k]
        self._record("clearing_known_at", t, self._clr_end[k - 1] if k else None, k)
        return out

    def clearing_for(self, T) -> pd.DataFrame:
        return self.clearing_known_at(self.cutoff(T))
