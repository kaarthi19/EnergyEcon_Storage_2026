"""Backtest evaluator on a small synthetic market (made-up numbers): settlement signs, SOC
accounting under the three efficiency conventions, SOC policies, maker vs taker."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from bessbid.backtest import MarketData, bid_arrays, simulate, soc_path, window_periods
from bessbid.bids import energy_fixed, regulation_only, to_table
from bessbid.config import Config
from bessbid.engine import ClearingEngine, StackArrays

CFG = Config()
START = pd.Timestamp("2026-07-01")


def synthetic_market(days=2, seed=0) -> MarketData:
    rng = np.random.default_rng(seed)
    ts = pd.date_range(START, periods=48 * days, freq="30min")
    facs = [f"U{i}" for i in range(8)]
    cap = rng.uniform(250, 450, len(facs))
    base = rng.uniform(60, 200, len(facs))
    rows = []
    for t in ts:
        for f, c, b0 in zip(facs, cap, base):
            av = 0.0 if (f == "U0" and t.hour < 2) else c
            pr = b0 + np.cumsum([5, 20, 40, 80, 150])
            for b, s in enumerate((0.25, 0.25, 0.20, 0.17, 0.13), start=1):
                rows.append((t, f, "Energy", b, av * s, pr[b - 1], av))
            rows += [(t, f, "Primary", 1, 0.30 * av, 3.0, av), (t, f, "Contingency", 1, 0.35 * av, 11.0, av),
                     (t, f, "Regulation", 1, 0.22 * av, 15.0 + b0 / 100, av)]
    stacks = pd.DataFrame(rows, columns=["ts", "facility_id", "product", "band", "offered_mw", "price", "available_mw"])
    S = StackArrays.from_frame(stacks)
    hod = ts.hour.to_numpy() + ts.minute.to_numpy() / 60
    nd = 1500 + 600 * np.sin((hod - 6) / 24 * 2 * np.pi) + rng.normal(0, 20, len(ts))
    eng = ClearingEngine(CFG)
    prev = None
    recs = []
    for i, t in enumerate(ts):
        r = eng.clear(S.period(i), nd[i], (110, 300, 105), prev)
        prev = r.gen_e
        recs.append((t, r.E, r.P, r.C, r.R, nd[i], r.sP, r.sC, r.sR))
    cl = pd.DataFrame(recs, columns=["ts", "energy_price_sgd_mwh", "primary_price_sgd_mwh_equiv",
                                     "contingency_price_sgd_mwh_equiv", "regulation_price_sgd_mwh_equiv",
                                     "net_demand_mw", "unmet_primary_mw", "unmet_contingency_mw",
                                     "unmet_regulation_mw"])
    cl["primary_requirement_mw"] = 110.0
    cl["contingency_requirement_mw"] = 300.0
    cl["regulation_requirement_mw"] = 105.0
    return MarketData(S, cl)


@pytest.fixture(scope="module")
def md():
    return synthetic_market()


def table(fn, days=1):
    out = []
    for t in window_periods(start=START, days=days):
        p = t.hour * 2 + t.minute // 30 + 1
        out.append(fn(t.date(), p))
    return to_table(out)


REG = lambda d, p: regulation_only(d, p, CFG)  # noqa: E731


def test_regulation_only_taker_and_maker(md):
    tbl = table(REG)
    t = simulate(tbl, data=md, mode="taker", start=START, days=1)
    f = t.frame
    exp = (md.clearing["regulation_price_sgd_mwh_equiv"].iloc[:48] * 100 * 0.5).sum()
    assert t.summary["revenue"] == pytest.approx(exp)
    assert t.summary["rev_E"] == 0 and (f["soc_end"] == 50.0).all()
    m = simulate(tbl, data=md, mode="maker", start=START, days=1)
    assert np.allclose(m.frame["aw_R"], 100.0)
    assert m.summary["revenue"] <= t.summary["revenue"] + 1e-6  # own offer can only lower R
    assert m.summary["breach_periods"] == 0


def test_maker_without_offers_reproduces_prices(md):
    empty = table(lambda d, p: regulation_only(d, p, CFG, mw=0), 2)
    r = simulate(empty, data=md, mode="maker", start=START + pd.Timedelta(days=1), days=1)
    for k in "EPCR":
        assert np.allclose(r.frame[k], r.frame[k + "_act"], atol=1e-7), k
    assert r.summary["revenue"] == 0


def test_revenue_signs(md):
    def fn(d, p):
        if p == 10:
            return energy_fixed(d, p, -40, CFG)
        if p == 36:
            return energy_fixed(d, p, 30, CFG)
        return REG(d, p)

    r = simulate(table(fn), data=md, mode="taker", start=START, days=1)
    f = r.frame.set_index("period")
    assert f.loc[10, "chg"] == pytest.approx(40) and f.loc[10, "dis"] == 0
    assert f.loc[10, "rev_E"] == pytest.approx(-f.loc[10, "E"] * 40 * 0.5) and f.loc[10, "rev_E"] < 0
    assert f.loc[36, "rev_E"] == pytest.approx(f.loc[36, "E"] * 30 * 0.5) and f.loc[36, "rev_E"] > 0
    assert f.loc[10, "rev_R"] == pytest.approx(f.loc[10, "R"] * 60 * 0.5)
    assert f.loc[36, "aw_R"] == pytest.approx(70)
    m = simulate(table(fn), data=md, mode="maker", start=START, days=1).frame.set_index("period")
    # price impact: charging raises, discharging lowers the energy price
    assert m.loc[10, "E"] >= m.loc[10, "E_act"] - 1e-9
    assert m.loc[36, "E"] <= m.loc[36, "E_act"] + 1e-9
    assert m.loc[10, "rev_E"] < 0 < m.loc[36, "rev_E"]


@pytest.mark.parametrize("conv", [(1.0, 0.85), (math.sqrt(0.85), math.sqrt(0.85)), (0.85, 1.0)])
def test_soc_conventions(md, conv):
    ec, ed = conv

    def fn(d, p):
        if p in (5, 6):
            return energy_fixed(d, p, -20, CFG)
        if p == 30:
            return energy_fixed(d, p, 10, CFG)
        return REG(d, p)

    r = simulate(table(fn), data=md, mode="taker", start=START, days=1, convention=conv)
    f = r.frame.set_index("period")
    assert f.loc[5, "soc_end"] == pytest.approx(50 + 0.5 * 20 * ec)
    assert f.loc[6, "soc_end"] == pytest.approx(50 + 2 * 0.5 * 20 * ec)
    assert f.loc[30, "soc_end"] == pytest.approx(50 + 20 * ec - 0.5 * 10 / ed)
    assert f["soc_end"].iloc[-1] == pytest.approx(50 + 20 * ec - 5 / ed)
    # round trip loses 15 % whichever convention: store x, get back 0.85 x
    assert ec * ed == pytest.approx(0.85)
    # per-convention columns exist for every configured convention and agree with soc_path
    for cv in CFG.efficiency_conventions:
        col = f"soc_c{cv[0]:.4f}_d{cv[1]:.4f}"
        assert np.allclose(r.frame[col], soc_path(r.frame["dis"], r.frame["chg"], cv, CFG))


def test_soc_policies(md):
    # charge 80 MW for 4 periods: +160 MWh * eta_c from 50 -> breach above 95
    def fn(d, p):
        if 3 <= p <= 6:
            return energy_fixed(d, p, -80, CFG)
        if 20 <= p <= 29:
            return energy_fixed(d, p, 80, CFG)
        return REG(d, p)

    tbl = table(fn)
    conv = (math.sqrt(0.85),) * 2
    none = simulate(tbl, data=md, mode="taker", start=START, days=1, convention=conv, soc_policy="none")
    assert none.summary["breach_periods"] > 0 and none.summary["soc_max"] > 95
    clip = simulate(tbl, data=md, mode="taker", start=START, days=1, convention=conv, soc_policy="clip")
    assert clip.summary["breach_periods"] == 0
    assert clip.summary["soc_max"] == pytest.approx(95.0) and clip.summary["soc_min"] == pytest.approx(5.0)
    assert clip.summary["clipped_periods"] > 0 and clip.summary["clipped_mwh"] > 0
    rej = simulate(tbl, data=md, mode="taker", start=START, days=1, convention=conv, soc_policy="reject")
    assert rej.summary["breach_periods"] == 0 and rej.summary["rejected_periods"] > 0
    f = rej.frame
    assert (f.loc[f["rejected"], ["dis", "chg"]].to_numpy() == 0).all()
    # reserves survive a rejected energy offer
    assert (f.loc[f["rejected"], "aw_R"] > 0).all()
    # maker + clip also never breaches
    mk = simulate(tbl, data=md, mode="maker", start=START, days=1, convention=conv, soc_policy="clip")
    assert mk.summary["breach_periods"] == 0


def test_taker_tie_modes(md):
    lam = md.clearing["energy_price_sgd_mwh"].iloc[0]

    def fn(d, p):
        from bessbid.bids import make_period_bid
        if p == 1:
            return make_period_bid(d, p, CFG, energy=[(round(lam, 2), 10.0)], regulation=[(0.0, 90.0)])
        return REG(d, p)

    md2 = MarketData(md.stacks, md.clearing.assign(
        energy_price_sgd_mwh=np.where(np.arange(len(md.clearing)) == 0, round(lam, 2), md.clearing["energy_price_sgd_mwh"])))
    s = simulate(table(fn), data=md2, mode="taker", start=START, days=1, tie="strict").frame
    i = simulate(table(fn), data=md2, mode="taker", start=START, days=1, tie="inclusive").frame
    assert s["dis"].iloc[0] == 0 and i["dis"].iloc[0] == pytest.approx(10.0)


def test_ramp_modes_and_warmup(md):
    tbl = table(REG, 2)
    s = START + pd.Timedelta(days=1)
    a = simulate(tbl, data=md, mode="maker", start=s, days=1)
    b = simulate(tbl, data=md, mode="maker", start=s, days=1, ramp="baseline")
    c = simulate(tbl, data=md, mode="maker", start=s, days=1, warmup_days=0.5)
    assert a.summary["revenue"] == pytest.approx(b.summary["revenue"], rel=1e-9)
    assert a.summary["revenue"] == pytest.approx(c.summary["revenue"], rel=1e-6)
    assert len(tbl) == 2 * 4 * 48


def test_input_errors(md):
    tbl = table(REG)
    with pytest.raises(ValueError, match="no rows"):
        simulate(tbl.iloc[:-4], data=md, mode="taker", start=START, days=1)
    r = simulate(tbl.iloc[:-4], data=md, mode="taker", start=START, days=1, allow_missing=True)
    assert r.frame["aw_R"].iloc[-1] == 0
    with pytest.raises(ValueError, match="no clearing data"):
        simulate(tbl, data=md, mode="taker", start=START, days=5, allow_missing=True)
    with pytest.raises(ValueError):
        simulate(tbl, data=md, mode="both", start=START, days=1)
    with pytest.raises(ValueError):
        simulate(tbl, data=md, soc_policy="ignore", start=START, days=1)
    with pytest.raises(ValueError, match="no rows in the window"):
        bid_arrays(tbl, window_periods(start=START + pd.Timedelta(days=1), days=1), allow_missing=True)
    dup = pd.concat([tbl, tbl.iloc[:1]])
    with pytest.raises(ValueError, match="duplicate"):
        bid_arrays(dup, window_periods(start=START, days=1))


def test_window_periods():
    assert len(window_periods("2026-10")) == 31 * 48
    assert len(window_periods("2026-11")) == 30 * 48
    w = window_periods(start="2026-08-02", days=30)
    assert len(w) == 1440 and w[-1] == pd.Timestamp("2026-08-31 23:30")
    assert len(window_periods(start="2026-08-02", end="2026-08-02 23:30")) == 48
