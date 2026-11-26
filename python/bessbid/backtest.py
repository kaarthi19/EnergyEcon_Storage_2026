"""EMA-replica evaluator: clear a bid table period by period and settle it.

:func:`simulate` takes a bid table (the canonical frame of :func:`bessbid.bids.to_table`, or a
list of :class:`~bessbid.bids.PeriodBid`) for a window of training periods and returns a
per-period frame plus a summary.

Modes
-----
``maker``  Our offers enter the exact clearing LP (:mod:`bessbid.engine`) together with the
           actual generator stacks, actual net demand and actual reserve requirements. The
           generator ramp state is chained period to period. ``ramp='own'`` (default) chains
           the dispatch of the market *with* our offers, as EMA's simulator would;
           ``ramp='baseline'`` chains the no-BESS dispatch (the Phase A convention; costs a
           second solve per period). Before the window the market is re-cleared without the
           BESS from a cold start at the first clearing period (Jul 1, as EMA did), or
           ``warmup_days`` before the window.
``taker``  Prices are the actual training clearing prices; each band is awarded by
           :func:`bessbid.engine.price_taker_clear` (``tie`` = 'strict' | 'inclusive').

SOC
---
SOC(t) = SOC(t-1) + h * (eta_c * charge_MW - discharge_MW / eta_d), h = 0.5, starting at
``cfg.initial_soc_mwh``; limits ``cfg.soc_min_mwh`` .. ``cfg.soc_max_mwh``. Reserve and
regulation activation is 0 MWh. ``soc_policy`` decides what happens when a period's award
would leave the limits (evaluated under ``convention``):

``none``    nothing; the SOC path is reported as is and breaches are counted.
``clip``    before clearing, energy offers are trimmed to what SOC allows (discharge bands
            from the highest price down, charge bands from the lowest price up), so the
            award never breaches. Reports the trimmed MWh.
``reject``  the period is cleared as offered; if the award would breach, the period is
            re-cleared with our energy offer removed (reserve offers kept).

Every run also reports the SOC path of the realised dispatch under each of
``cfg.efficiency_conventions`` (no feedback), so a single run shows breach risk under all
three conventions.

Settlement
----------
Energy: price_E * (discharge - charge) * h (charging costs money); reserves: price_k * MW * h.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
import pandas as pd

from .bids import PRODUCTS, TABLE_COLUMNS, to_table
from .config import Config
from .engine import BessOffer, ClearingEngine, StackArrays, price_taker_clear
from .periods import PERIOD, month_slots

__all__ = [
    "MarketData",
    "BacktestResult",
    "simulate",
    "bid_arrays",
    "soc_path",
    "window_periods",
    "PSEUDO_MONTHS",
    "convention_label",
    "run_months",
]

#: Training months used as pseudo-Novembers.
PSEUDO_MONTHS = ("2026-08", "2026-09", "2026-10")
PRICE_COLS = {"E": "energy_price_sgd_mwh", "P": "primary_price_sgd_mwh_equiv",
              "C": "contingency_price_sgd_mwh_equiv", "R": "regulation_price_sgd_mwh_equiv"}
REQ_COLS = ["primary_requirement_mw", "contingency_requirement_mw", "regulation_requirement_mw"]
_TOL = 1e-6


def convention_label(conv: tuple[float, float]) -> str:
    ec, ed = conv
    return f"c{ec:.4f}_d{ed:.4f}"


# --------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------


@dataclass
class MarketData:
    """Actual market inputs for the training period: stacks (dense) and the clearing frame."""

    stacks: StackArrays
    clearing: pd.DataFrame  # sorted by ts, from io.load_clearing

    @classmethod
    def from_dataset(cls, ds, start: str | None = None) -> "MarketData":
        st = ds.stacks
        if start is not None:
            st = st[st["ts"] >= pd.Timestamp(start)]
        return cls(StackArrays.from_frame(st), ds.clearing.sort_values("ts").reset_index(drop=True))

    @classmethod
    def from_frames(cls, stacks: pd.DataFrame, clearing: pd.DataFrame) -> "MarketData":
        return cls(StackArrays.from_frame(stacks), clearing.sort_values("ts").reset_index(drop=True))


def window_periods(month: str | None = None, start=None, days: int | None = None,
                   end=None) -> pd.DatetimeIndex:
    """Period start times: a calendar month ('2026-10'), or ``start`` plus ``days`` (or up to
    ``end`` inclusive)."""
    if month is not None:
        return pd.DatetimeIndex([s.start for s in month_slots(month)])
    if start is None:
        raise ValueError("give month or start")
    s = pd.Timestamp(start)
    if days is not None:
        n = int(days) * 48
        return pd.DatetimeIndex(s + PERIOD * np.arange(n))
    if end is None:
        raise ValueError("give days or end")
    return pd.date_range(s, pd.Timestamp(end), freq=PERIOD)


def bid_arrays(bids, ts: pd.DatetimeIndex, allow_missing: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """(N, 4, 10) price and quantity arrays (NaN = blank) for the periods ``ts``.

    ``bids``: canonical bid table or an iterable of PeriodBid. Missing periods raise unless
    ``allow_missing`` (then they carry no offer)."""
    tbl = bids if isinstance(bids, pd.DataFrame) else to_table(list(bids))
    miss = [c for c in TABLE_COLUMNS if c not in tbl.columns]
    if miss:
        raise ValueError(f"bid table lacks columns {miss[:3]}")
    t = pd.to_datetime(tbl["trading_date"]).dt.normalize() + (tbl["trading_period"].astype(int) - 1) * PERIOD
    prod = tbl["product"].map({p: i for i, p in enumerate(PRODUCTS)})
    if prod.isna().any():
        raise ValueError(f"unknown product {tbl['product'][prod.isna()].iloc[0]!r}")
    pos = ts.get_indexer(pd.DatetimeIndex(t))
    keep = pos >= 0
    N = len(ts)
    P = np.full((N, 4, 10), np.nan)
    Q = np.full((N, 4, 10), np.nan)
    pc = [f"price_{i}" for i in range(1, 11)]
    qc = [f"quantity_{i}" for i in range(1, 11)]
    pv = tbl[pc].to_numpy(dtype=float)[keep]
    qv = tbl[qc].to_numpy(dtype=float)[keep]
    pi = pos[keep]
    ki = prod.to_numpy()[keep].astype(int)
    if len(np.unique(pi * 4 + ki)) != len(pi):
        raise ValueError("bid table has duplicate (period, product) rows")
    P[pi, ki] = pv
    Q[pi, ki] = qv
    have = np.zeros(N, bool)
    have[pi] = True
    if N and not have.any():
        raise ValueError(f"bid table has no rows in the window {ts[0]} .. {ts[-1]}")
    if not allow_missing and not have.all():
        raise ValueError(f"bid table has no rows for {int((~have).sum())} periods, e.g. {ts[~have][0]}")
    return P, Q


def soc_path(dis: np.ndarray, chg: np.ndarray, conv: tuple[float, float], cfg: Config) -> np.ndarray:
    """End-of-period SOC (MWh) for given discharge/charge MW (no limits applied)."""
    ec, ed = conv
    h = cfg.period_hours
    return cfg.initial_soc_mwh + np.cumsum(h * (ec * np.asarray(chg) - np.asarray(dis) / ed))


def _breach(soc: np.ndarray, cfg: Config) -> np.ndarray:
    lo, hi = cfg.soc_min_mwh, cfg.soc_max_mwh
    return np.maximum(lo - soc, 0.0) + np.maximum(soc - hi, 0.0)


def _trim(bands: tuple[tuple[float, float], ...], limit: float, keep_low_price: bool):
    """Trim (price, MW>0) bands to total <= limit, cutting the bands that clear last first."""
    if sum(q for _, q in bands) <= limit + _TOL:
        return bands, 0.0
    order = sorted(range(len(bands)), key=lambda j: bands[j][0], reverse=not keep_low_price)
    left = max(limit, 0.0)
    out = [None] * len(bands)
    for j in order:
        p, q = bands[j]
        a = min(q, left)
        left -= a
        out[j] = (p, a)
    cut = sum(q for _, q in bands) - sum(q for _, q in out)
    return tuple(b for b in out if b[1] > _TOL), cut


def _clip_offer(off: BessOffer, soc: float, conv, cfg: Config) -> tuple[BessOffer, float]:
    ec, ed = conv
    h = cfg.period_hours
    dmax = max(soc - cfg.soc_min_mwh, 0.0) * ed / h
    cmax = max(cfg.soc_max_mwh - soc, 0.0) / (ec * h)
    dis, cut_d = _trim(off.discharge, dmax, keep_low_price=True)  # low-priced discharge clears first
    chg, cut_c = _trim(off.charge, cmax, keep_low_price=False)  # high-priced charge clears first
    if cut_d <= _TOL and cut_c <= _TOL:
        return off, 0.0
    energy = tuple(sorted(dis, key=lambda b: b[0])) + tuple((p, -q) for p, q in sorted(chg, key=lambda b: b[0]))
    energy = tuple(sorted(energy, key=lambda b: b[0]))
    new = BessOffer(energy, off.primary, off.contingency, off.regulation)
    return new, cut_d * h / ed + cut_c * h * ec  # MWh of SOC change removed


def _no_energy(off: BessOffer) -> BessOffer:
    return BessOffer((), off.primary, off.contingency, off.regulation)


# --------------------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------------------


@dataclass
class BacktestResult:
    frame: pd.DataFrame
    summary: dict
    params: dict = field(default_factory=dict)


# --------------------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------------------


def _resolve_conv(convention, cfg: Config) -> tuple[float, float]:
    if convention is None or convention == "sqrt":
        s = float(np.sqrt(cfg.round_trip_eff))
        return (s, s)
    if convention == "charge":  # all losses on charge
        return (cfg.round_trip_eff, 1.0)
    if convention == "discharge":
        return (1.0, cfg.round_trip_eff)
    ec, ed = convention
    return (float(ec), float(ed))


def simulate(bids, month: str | None = None, data: MarketData | None = None, mode: str = "maker",
             convention=None, cfg: Config | None = None, soc_policy: str = "none",
             tie: str = "strict", ramp: str = "own", warmup_days: float | None = None,
             start=None, days: int | None = None, end=None, allow_missing: bool = False,
             engine: ClearingEngine | None = None) -> BacktestResult:
    """Clear and settle ``bids`` over a window of training periods.

    ``month`` ('2026-10') or ``start`` + ``days``/``end`` pick the window; every period in it
    must have actual clearing data. ``convention``: (eta_c, eta_d), or 'sqrt' (default),
    'charge' (all loss on charge), 'discharge' (all loss on discharge). See the module doc for
    ``mode``, ``soc_policy``, ``tie`` and ``ramp``.
    """
    t0 = time.perf_counter()
    if data is None:
        raise ValueError("data (MarketData) is required")
    cfg = cfg or Config()
    if mode not in ("maker", "taker"):
        raise ValueError("mode must be 'maker' or 'taker'")
    if soc_policy not in ("none", "clip", "reject"):
        raise ValueError("soc_policy must be 'none', 'clip' or 'reject'")
    if ramp not in ("own", "baseline"):
        raise ValueError("ramp must be 'own' or 'baseline'")
    conv = _resolve_conv(convention, cfg)
    ts = window_periods(month, start, days, end)
    cl = data.clearing.set_index("ts")
    missing = ts.difference(cl.index)
    if len(missing):
        raise ValueError(f"no clearing data for {len(missing)} periods, e.g. {missing[0]}")
    cw = cl.loc[ts]
    act = {k: cw[c].to_numpy(float) for k, c in PRICE_COLS.items()}
    nd = cw["net_demand_mw"].to_numpy(float)
    rq = cw[REQ_COLS].to_numpy(float)
    Pb, Qb = bid_arrays(bids, ts, allow_missing)
    N = len(ts)
    h = cfg.period_hours
    eng = engine or ClearingEngine(cfg)

    # ---- ramp warm-up (maker): no-BESS clearing from a cold start up to the window -------
    prev_own = prev_base = None
    if mode == "maker":
        sidx = data.stacks.index_of(ts.to_numpy())
        first = data.clearing["ts"].iloc[0]
        w0 = first if warmup_days is None else max(first, ts[0] - pd.Timedelta(days=warmup_days))
        wts = data.clearing["ts"][(data.clearing["ts"] >= w0) & (data.clearing["ts"] < ts[0])]
        if len(wts):
            wc = cl.loc[pd.DatetimeIndex(wts)]
            widx = data.stacks.index_of(wts.to_numpy())
            wnd = wc["net_demand_mw"].to_numpy(float)
            wrq = wc[REQ_COLS].to_numpy(float)
            st = None
            for n, i in enumerate(np.atleast_1d(widx)):
                st = eng.clear(data.stacks.period(int(i)), wnd[n], wrq[n], st).gen_e
            prev_own = prev_base = st

    cols = {k: np.zeros(N) for k in (
        "E", "P", "C", "R", "dis", "chg", "aw_P", "aw_C", "aw_R", "off_dis", "off_chg",
        "off_P", "off_C", "off_R", "soc_start", "soc_end", "clip_mwh", "breach_mwh", "rej_dis", "rej_chg")}
    rejected = np.zeros(N, bool)
    sP = np.zeros(N); sC = np.zeros(N); sR = np.zeros(N)
    soc = cfg.initial_soc_mwh
    ec, ed = conv

    def award_of(off: BessOffer, i: int):
        nonlocal prev_own
        if mode == "taker":
            pr = {k: act[k][i] for k in "EPCR"}
            a = price_taker_clear(off, pr, tie=tie, pmax=cfg.power_mw)
            return pr, a.awards, None
        r = eng.clear(data.stacks.period(int(sidx[i])), nd[i], rq[i],
                      prev_own if ramp == "own" else prev_base, off)
        return r.prices, r.awards, r

    for i in range(N):
        off = BessOffer.from_arrays(Pb[i], Qb[i])
        cols["off_dis"][i] = sum(q for _, q in off.discharge)
        cols["off_chg"][i] = sum(q for _, q in off.charge)
        for k in "PCR":
            cols["off_" + k][i] = sum(q for _, q in off.reserve(k))
        cols["soc_start"][i] = soc
        if soc_policy == "clip":
            off, cut = _clip_offer(off, soc, conv, cfg)
            cols["clip_mwh"][i] = cut
        pr, aw, res = award_of(off, i)
        d_soc = h * (ec * aw["chg"] - aw["dis"] / ed)
        if soc_policy == "reject":
            new = soc + d_soc
            if new < cfg.soc_min_mwh - _TOL or new > cfg.soc_max_mwh + _TOL:
                rejected[i] = True
                cols["rej_dis"][i], cols["rej_chg"][i] = aw["dis"], aw["chg"]
                pr, aw, res = award_of(_no_energy(off), i)
                d_soc = h * (ec * aw["chg"] - aw["dis"] / ed)
        soc = soc + d_soc
        cols["soc_end"][i] = soc
        cols["breach_mwh"][i] = float(_breach(np.array([soc]), cfg)[0])
        for k in "EPCR":
            cols[k][i] = pr[k]
        cols["dis"][i], cols["chg"][i] = aw["dis"], aw["chg"]
        for k in "PCR":
            cols["aw_" + k][i] = aw[k]
        if res is not None:
            sP[i], sC[i], sR[i] = res.sP, res.sC, res.sR
            prev_own = res.gen_e
            if ramp == "baseline":
                prev_base = eng.clear(data.stacks.period(int(sidx[i])), nd[i], rq[i], prev_base).gen_e

    f = pd.DataFrame({"ts": ts})
    f["date"] = f["ts"].dt.normalize()
    f["period"] = (f["ts"].dt.hour * 2 + f["ts"].dt.minute // 30 + 1).astype(int)
    for k in ("E", "P", "C", "R"):
        f[k] = cols[k]
        f[k + "_act"] = act[k]
    for k in ("dis", "chg"):
        f[k] = cols[k]
    f["net"] = f["dis"] - f["chg"]
    for k in "PCR":
        f["aw_" + k] = cols["aw_" + k]
    for k in ("off_dis", "off_chg", "off_P", "off_C", "off_R"):
        f[k] = cols[k]
    f["rev_E"] = f["E"] * f["net"] * h
    for k in "PCR":
        f["rev_" + k] = f[k] * f["aw_" + k] * h
    f["rev"] = f[["rev_E", "rev_P", "rev_C", "rev_R"]].sum(axis=1)
    f["soc_start"] = cols["soc_start"]
    f["soc_end"] = cols["soc_end"]
    f["breach_mwh"] = cols["breach_mwh"]
    f["clip_mwh"] = cols["clip_mwh"]
    f["rejected"] = rejected
    f["rej_dis"] = cols["rej_dis"]
    f["rej_chg"] = cols["rej_chg"]
    if mode == "maker":
        f["sP"], f["sC"], f["sR"] = sP, sC, sR
    # SOC of the realised dispatch under every configured convention (no feedback)
    conv_stats = {}
    for cv in cfg.efficiency_conventions:
        s = soc_path(f["dis"].to_numpy(), f["chg"].to_numpy(), cv, cfg)
        b = _breach(s, cfg)
        lab = convention_label(cv)
        f["soc_" + lab] = s
        conv_stats[lab] = dict(min=float(s.min()), max=float(s.max()), final=float(s[-1]),
                               breach_periods=int((b > _TOL).sum()), breach_max_mwh=float(b.max()))
    n_days = N / 48.0
    tot = float(f["rev"].sum())
    summary = dict(
        mode=mode, convention=list(conv), soc_policy=soc_policy, tie=tie if mode == "taker" else None,
        ramp=ramp if mode == "maker" else None,
        start=str(ts[0]), end=str(ts[-1]), periods=N, days=n_days,
        revenue=tot, revenue_per_30d=tot / n_days * 30.0,
        rev_E=float(f["rev_E"].sum()), rev_P=float(f["rev_P"].sum()),
        rev_C=float(f["rev_C"].sum()), rev_R=float(f["rev_R"].sum()),
        dis_mwh=float(f["dis"].sum() * h), chg_mwh=float(f["chg"].sum() * h),
        energy_periods=int(((f["dis"] > _TOL) | (f["chg"] > _TOL)).sum()),
        soc_min=float(f["soc_end"].min()), soc_max=float(f["soc_end"].max()),
        soc_final=float(f["soc_end"].iloc[-1]),
        breach_periods=int((f["breach_mwh"] > _TOL).sum()), breach_max_mwh=float(f["breach_mwh"].max()),
        breach_mwh_sum=float(f["breach_mwh"].sum()),
        clipped_periods=int((f["clip_mwh"] > _TOL).sum()), clipped_mwh=float(f["clip_mwh"].sum()),
        rejected_periods=int(rejected.sum()),
        rejected_mwh=float((f["rej_dis"] / ed + f["rej_chg"] * ec).sum() * h),
        price_impact_E_mean=float((f["E"] - f["E_act"]).mean()),
        price_impact_R_mean=float((f["R"] - f["R_act"]).mean()),
        conventions=conv_stats,
        runtime_s=time.perf_counter() - t0,
    )
    return BacktestResult(f, summary, dict(month=month, mode=mode, convention=conv, soc_policy=soc_policy,
                                           tie=tie, ramp=ramp, warmup_days=warmup_days))


def run_months(bids_for_month, data: MarketData, months: Sequence[str] = PSEUDO_MONTHS,
               modes: Sequence[str] = ("maker", "taker"), conventions=None, cfg: Config | None = None,
               **kw) -> pd.DataFrame:
    """Backtest one bid generator over several (pseudo-)months.

    ``bids_for_month(month) -> bid table | list[PeriodBid]``. ``conventions``: list of
    conventions (default: just ``sqrt``). Extra keyword arguments go to :func:`simulate`.
    Returns one summary row per (month, mode, convention) plus an ``ALL`` row per (mode,
    convention) with revenue per 30 days over all months."""
    cfg = cfg or Config()
    convs = list(conventions) if conventions is not None else ["sqrt"]
    rows = []
    eng = kw.pop("engine", None) or ClearingEngine(cfg)
    for m in months:
        bids = bids_for_month(m)
        for mode in modes:
            for cv in convs:
                r = simulate(bids, m, data, mode=mode, convention=cv, cfg=cfg, engine=eng, **kw)
                s = {k: v for k, v in r.summary.items() if not isinstance(v, (dict, list))}
                s.update(month=m, convention=convention_label(_resolve_conv(cv, cfg)))
                rows.append(s)
    out = pd.DataFrame(rows)
    agg = []
    for (mode, cv), g in out.groupby(["mode", "convention"], sort=False):
        d = {"month": "ALL", "mode": mode, "convention": cv, "days": g["days"].sum(),
             "periods": g["periods"].sum()}
        for k in ("revenue", "rev_E", "rev_P", "rev_C", "rev_R", "dis_mwh", "chg_mwh", "breach_periods",
                  "clipped_periods", "clipped_mwh", "rejected_periods", "runtime_s"):
            d[k] = g[k].sum()
        d["revenue_per_30d"] = d["revenue"] / d["days"] * 30.0
        agg.append(d)
    return pd.concat([out, pd.DataFrame(agg)], ignore_index=True)
