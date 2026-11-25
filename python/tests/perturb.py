"""Helpers for no-leak property tests: change every data row that is NOT legal at a decision
time t, so that any forecast made at t must come out bit-identical."""

from __future__ import annotations

import numpy as np
import pandas as pd

PERIOD = pd.Timedelta(minutes=30)


def perturb_stacks(stacks: pd.DataFrame, t, legal: bool = False) -> pd.DataFrame:
    """Rows published after t (or, with ``legal``, at/before t) get different numbers."""
    s = stacks.copy()
    m = (s["pub"] <= t) if legal else (s["pub"] > t)
    m = m.to_numpy()
    s.loc[m, "price"] = s.loc[m, "price"] * 1.37 + 5.0
    s.loc[m, "offered_mw"] = s.loc[m, "offered_mw"] * 0.61
    s.loc[m, "available_mw"] = s.loc[m, "available_mw"] * 0.83
    return s


def perturb_predispatch(p: pd.DataFrame | None, t) -> pd.DataFrame | None:
    """Vintages issued after t change; training actuals of periods not ended by t change."""
    if p is None:
        return None
    p = p.copy()
    m = (p["issue_time"] > t).to_numpy()
    p.loc[m, "forecast_load_mw"] += 333.0
    p.loc[m, "forecast_solar_mw"] *= 1.5
    p.loc[m, "known_outage_mw_at_issue"] += 444.0
    a = ((p["ts"] + PERIOD) > t).to_numpy()
    for c in ("synthetic_actual_load_mw", "synthetic_actual_solar_mw"):
        if c in p.columns:
            p.loc[a, c] = p.loc[a, c] * 1.2 + 77.0
    return p


def perturb_clearing(c: pd.DataFrame, t) -> pd.DataFrame:
    """Clearing rows whose period has not ended by t change (prices, demand, requirements)."""
    c = c.copy()
    m = ((c["ts"] + PERIOD) > t).to_numpy()
    for col in c.columns:
        if col in ("ts", "period"):
            continue
        if np.issubdtype(c[col].dtype, np.number):
            c.loc[m, col] = c.loc[m, col] * 1.5 + 11.0
    c.loc[m, "contingency_requirement_mw"] = 600.0
    return c


def frames_equal(a: pd.DataFrame, b: pd.DataFrame, cols) -> bool:
    """Bit-identical numeric columns."""
    return all(np.array_equal(a[c].to_numpy(dtype=float), b[c].to_numpy(dtype=float), equal_nan=True) for c in cols)
