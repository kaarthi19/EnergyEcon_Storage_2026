"""Clearing engine and price-taker awards on small synthetic stacks (made-up numbers)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from bessbid.bids import energy_fixed, regulation_only
from bessbid.config import Config
from bessbid.engine import (
    BessOffer,
    ClearingEngine,
    PeriodStack,
    StackArrays,
    clear,
    clear_period,
    price_taker_clear,
)

PEN = (750.0, 1000.0, 1200.0)


def rand_stack(rng, F=6, B=5, scale=1.0) -> PeriodStack:
    av = rng.uniform(150, 450, F) * scale
    av[rng.random(F) < 0.15] = 0.0
    shares = np.array([0.25, 0.25, 0.20, 0.17, 0.13])[:B]
    shares = shares / shares.sum()
    e_q = av[:, None] * shares[None, :]
    base = rng.uniform(60, 250, F)
    e_p = base[:, None] + np.cumsum(rng.uniform(5, 120, (F, B)), axis=1)
    p_q, c_q, r_q = 0.30 * av, 0.35 * av, 0.22 * av
    r_q[F // 2:] = 0.0
    p_p = rng.uniform(1.5, 8, F)
    c_p = rng.uniform(7.5, 25, F)
    r_p = rng.uniform(10, 38, F)
    return PeriodStack(e_q, e_p, p_q, p_p, c_q, c_p, r_q, r_p, av)


def scipy_reference(st: PeriodStack, nd, req, prev=None, ramp=0.42, voll=4500.0, bess=None, pmax=100.0):
    """Independent formulation with scipy: explicit ramp rows instead of band clipping."""
    sp = pytest.importorskip("scipy.optimize")
    F, B = st.e_q.shape
    nE = F * B
    dis = list(bess.discharge) if bess else []
    chg = list(bess.charge) if bess else []
    res = {k: list(bess.reserve(k)) if bess else [] for k in "PCR"}
    c = list(st.e_p.ravel()) + list(st.p_p) + list(st.c_p) + list(st.r_p) + list(PEN) + [voll]
    ub = list(st.e_q.ravel()) + list(st.p_q) + list(st.c_q) + list(st.r_q) + [None] * 4
    iD = len(c)
    c += [p for p, _ in dis]; ub += [q for _, q in dis]
    iG = len(c)
    c += [-p for p, _ in chg]; ub += [q for _, q in chg]
    iB = {}
    for k in "PCR":
        iB[k] = len(c)
        c += [p for p, _ in res[k]]; ub += [q for _, q in res[k]]
    nv = len(c)
    A, b = [], []
    for f in range(F):
        row = np.zeros(nv); row[f * B:(f + 1) * B] = 1
        row[nE + f] = row[nE + F + f] = row[nE + 2 * F + f] = 1
        A.append(row); b.append(st.av[f])
        if prev is not None:
            row = np.zeros(nv); row[f * B:(f + 1) * B] = 1
            A.append(row); b.append(prev[f] + ramp * st.av[f])
    kr = []
    for kk, k in enumerate("PCR"):
        row = np.zeros(nv); row[nE + kk * F:nE + (kk + 1) * F] = -1; row[nE + 3 * F + kk] = -1
        row[iB[k]:iB[k] + len(res[k])] = -1
        kr.append(len(A)); A.append(row); b.append(-req[kk])
    if bess:
        row = np.zeros(nv); row[iD:iD + len(dis)] = 1
        for k in "PCR":
            row[iB[k]:iB[k] + len(res[k])] = 1
        A.append(row); b.append(pmax)
        row = np.zeros(nv); row[iG:iG + len(chg)] = 1; row[iB["R"]:iB["R"] + len(res["R"])] = 1
        A.append(row); b.append(pmax)
    Aeq = np.zeros((1, nv)); Aeq[0, :nE] = 1; Aeq[0, nE + 3 * F + 3] = 1
    Aeq[0, iD:iD + len(dis)] = 1; Aeq[0, iG:iG + len(chg)] = -1
    r = sp.linprog(c, A_ub=np.array(A), b_ub=b, A_eq=Aeq, b_eq=[nd], bounds=[(0, u) for u in ub], method="highs")
    assert r.status == 0
    m = r.ineqlin.marginals
    return dict(E=r.eqlin.marginals[0], P=-m[kr[0]], C=-m[kr[1]], R=-m[kr[2]], x=r.x, cost=r.fun)


def demand_for(st: PeriodStack, frac: float) -> float:
    return float(frac * st.e_q.sum())


# --------------------------------------------------------------------------------------


def test_simple_merit_order():
    # two units, one band each; reserve offers zero
    st = PeriodStack(np.array([[100.0], [100.0]]), np.array([[50.0], [80.0]]), np.zeros(2), np.zeros(2),
                     np.zeros(2), np.zeros(2), np.zeros(2), np.zeros(2), np.array([100.0, 100.0]))
    r = ClearingEngine().clear(st, 150.0, (0, 0, 0))
    assert r.E == pytest.approx(80.0)
    assert r.gen_e == pytest.approx([100.0, 50.0])
    assert (r.P, r.C, r.R) == (0.0, 0.0, 0.0)
    # requirement with no offers -> shortfall priced at the penalty
    r = ClearingEngine().clear(st, 150.0, (10, 20, 30))
    assert (r.P, r.C, r.R) == pytest.approx(PEN)
    assert (r.sP, r.sC, r.sR) == pytest.approx((10, 20, 30))
    # demand above all supply -> unserved energy at VoLL
    r = ClearingEngine().clear(st, 250.0, (0, 0, 0))
    assert r.unserved == pytest.approx(50.0)
    assert r.E == pytest.approx(4500.0)


def test_reserve_cooptimisation_opportunity_cost():
    # one unit, energy at 100, regulation offer 10; tight headroom: av 100, demand 95, R req 10
    st = PeriodStack(np.array([[100.0]]), np.array([[100.0]]), np.zeros(1), np.zeros(1), np.zeros(1),
                     np.zeros(1), np.array([50.0]), np.array([10.0]), np.array([100.0]))
    r = ClearingEngine().clear(st, 95.0, (0, 0, 10))
    # 5 MW of regulation from headroom, 5 MW short -> R price = 1200; energy = 100 + (1200 - 10)
    assert r.sR == pytest.approx(5.0)
    assert r.R == pytest.approx(1200.0)
    assert r.E == pytest.approx(100.0 + 1200.0 - 10.0)


@pytest.mark.parametrize("seed", range(25))
def test_matches_independent_formulation(seed):
    rng = np.random.default_rng(seed)
    st = rand_stack(rng)
    nd = demand_for(st, rng.uniform(0.3, 0.95))
    req = (rng.uniform(20, 120), rng.uniform(50, 300), rng.uniform(20, 120))
    prev = st.e_q.sum(1) * rng.uniform(0.0, 0.8, st.av.shape) if seed % 2 else None
    eng = ClearingEngine()
    r = eng.clear(st, nd, req, prev)
    ref = scipy_reference(st, nd, req, prev)
    assert r.cost == pytest.approx(ref["cost"], rel=1e-9, abs=1e-6)
    for k in "EPCR":
        assert getattr(r, k) == pytest.approx(ref[k], abs=1e-6), k


@pytest.mark.parametrize("seed", range(10))
def test_bess_matches_independent_formulation(seed):
    rng = np.random.default_rng(100 + seed)
    st = rand_stack(rng)
    nd = demand_for(st, rng.uniform(0.4, 0.9))
    req = (60.0, 150.0, 60.0)
    off = BessOffer(energy=((rng.uniform(0, 300), -30.0), (rng.uniform(301, 600), 40.0)),
                    regulation=((5.0, 30.0),), contingency=((1.0, 20.0),))
    r = ClearingEngine().clear(st, nd, req, None, off)
    ref = scipy_reference(st, nd, req, None, bess=off)
    assert r.cost == pytest.approx(ref["cost"], rel=1e-9, abs=1e-6)
    for k in "EPCR":
        assert getattr(r, k) == pytest.approx(ref[k], abs=1e-6), k


def test_warm_and_cold_agree_on_sequence():
    rng = np.random.default_rng(7)
    warm, cold = ClearingEngine(warm_start=True), ClearingEngine(warm_start=False)
    pw = pc = None
    for _ in range(30):
        st = rand_stack(rng)
        nd = demand_for(st, rng.uniform(0.4, 0.9))
        a = warm.clear(st, nd, (100, 300, 100), pw)
        b = cold.clear(st, nd, (100, 300, 100), pc)
        pw, pc = a.gen_e, b.gen_e
        assert a.price_tuple() == pytest.approx(b.price_tuple(), abs=1e-7)


def test_ramp_limit_raises_price():
    st = PeriodStack(np.array([[100.0], [100.0]]), np.array([[50.0], [80.0]]), np.zeros(2), np.zeros(2),
                     np.zeros(2), np.zeros(2), np.zeros(2), np.zeros(2), np.array([100.0, 100.0]))
    # cheap unit was off last period: it can ramp only to 0 + 0.42*100 = 42 MW
    r = ClearingEngine().clear(st, 150.0, (0, 0, 0), prev=np.array([0.0, 100.0]))
    assert r.gen_e == pytest.approx([42.0, 100.0])
    assert r.unserved == pytest.approx(8.0)
    r2 = ClearingEngine().clear(st, 120.0, (0, 0, 0), prev=np.array([0.0, 100.0]))
    assert r2.E == pytest.approx(80.0) and r2.gen_e[0] == pytest.approx(42.0)


@pytest.mark.parametrize("seed", range(12))
def test_bess_monotonicity(seed):
    """More discharge never raises the energy price; more charging never lowers it."""
    rng = np.random.default_rng(200 + seed)
    st = rand_stack(rng)
    nd = demand_for(st, rng.uniform(0.5, 0.97))
    req = (110.0, 300.0, 105.0)
    eng = ClearingEngine()
    base = eng.clear(st, nd, req)
    last = base.E
    for q in (10, 25, 50, 100):
        r = eng.clear(st, nd, req, None, BessOffer(energy=((0.0, float(q)),)))
        assert r.dis == pytest.approx(q)
        assert r.E <= last + 1e-7
        last = r.E
    last = base.E
    for q in (10, 25, 50, 100):
        r = eng.clear(st, nd, req, None, BessOffer(energy=((4499.0, -float(q)),)))
        assert r.chg == pytest.approx(q)
        assert r.E >= last - 1e-7
        last = r.E
    # regulation offer never raises the regulation price
    last = base.R
    for q in (25, 50, 100):
        r = eng.clear(st, nd, req, None, BessOffer(regulation=((0.0, float(q)),)))
        assert r.R <= last + 1e-7
        last = r.R


def test_bess_price_rules_in_lp():
    rng = np.random.default_rng(3)
    st = rand_stack(rng)
    nd = demand_for(st, 0.6)
    eng = ClearingEngine()
    lam = eng.clear(st, nd, (110, 300, 105)).E
    # a small discharge band above the price does not clear; below it clears
    hi = eng.clear(st, nd, (110, 300, 105), None, BessOffer(energy=((lam + 50, 1.0),)))
    lo = eng.clear(st, nd, (110, 300, 105), None, BessOffer(energy=((lam - 50, 1.0),)))
    assert hi.dis == pytest.approx(0.0) and lo.dis == pytest.approx(1.0)
    # a charge band priced above the price clears; below it does not
    hi = eng.clear(st, nd, (110, 300, 105), None, BessOffer(energy=((lam + 50, -1.0),)))
    lo = eng.clear(st, nd, (110, 300, 105), None, BessOffer(energy=((lam - 50, -1.0),)))
    assert hi.chg == pytest.approx(1.0) and lo.chg == pytest.approx(0.0)
    # revenue sign: the LP never pays more than the offer for charge
    assert hi.E <= lam + 50 + 1e-9


def test_bess_joint_rows():
    rng = np.random.default_rng(4)
    st = rand_stack(rng)
    nd = demand_for(st, 0.6)
    eng = ClearingEngine()
    # an over-offer (200 MW total) is limited by dis + P + C + R <= 100
    off = BessOffer(energy=((0.0, 100.0),), regulation=((0.0, 100.0),))
    r = eng.clear(st, nd, (110, 300, 105), None, off)
    assert r.dis + r.bess_award("R") <= 100 + 1e-6
    # charge side: chg + R <= 100, but chg + C may exceed 100 (only R is charge-coupled)
    off = BessOffer(energy=((4000.0, -100.0),), regulation=((0.0, 100.0),), contingency=((0.0, 50.0),))
    r = eng.clear(st, nd, (110, 300, 105), None, off)
    assert r.chg + r.bess_award("R") <= 100 + 1e-6
    assert r.bess_award("C") == pytest.approx(50.0)
    # pmax override
    r = ClearingEngine(bess_pmax=60).clear(st, nd, (110, 300, 105), None, BessOffer(regulation=((0.0, 100.0),)))
    assert r.bess_award("R") == pytest.approx(60.0)


def test_from_period_bid_and_wrappers():
    cfg = Config()
    pb = energy_fixed("2026-10-01", 30, 40, cfg)
    off = BessOffer.from_period_bid(pb)
    assert off.discharge == ((0.01, 40.0),) and off.regulation == ((0.01, 60.0),)
    pb = energy_fixed("2026-10-01", 10, -40, cfg)
    off = BessOffer.from_period_bid(pb)
    assert off.charge == ((3000.0, 40.0),)
    rng = np.random.default_rng(9)
    st = rand_stack(rng)
    nd = demand_for(st, 0.6)
    a = clear(st, nd, {"P": 110, "C": 300, "R": 105}, bess=regulation_only("2026-10-01", 1, cfg))
    b = clear_period(st.e_q, st.e_p, st.p_q, st.p_p, st.c_q, st.c_p, st.r_q, st.r_p, st.av, nd,
                     (110, 300, 105), bess=off)
    assert a.bess_award("R") == pytest.approx(100.0)
    assert b.chg == pytest.approx(40.0)
    # penalties override
    c = clear_period(st.e_q, st.e_p, st.p_q, st.p_p, st.c_q, st.c_p, st.r_q, st.r_p, st.av * 0, 0.0,
                     (1, 1, 1), penalties=(1, 2, 3))
    assert c.price_tuple()[1:] == pytest.approx((1, 2, 3))


def test_stack_arrays_from_frame():
    rows = []
    for t in pd.date_range("2026-07-01", periods=3, freq="30min"):
        for f, av in (("U2", 200.0), ("U1", 100.0)):
            for b in range(1, 6):
                rows.append((t, f, "Energy", b, av / 5, 50.0 + b + (f == "U2"), av))
            rows += [(t, f, "Primary", 1, 0.3 * av, 2.0, av), (t, f, "Contingency", 1, 0.35 * av, 9.0, av),
                     (t, f, "Regulation", 1, 0.22 * av, 15.0, av)]
    df = pd.DataFrame(rows, columns=["ts", "facility_id", "product", "band", "offered_mw", "price", "available_mw"])
    S = StackArrays.from_frame(df.sample(frac=1.0, random_state=0))
    assert S.facilities == ("U1", "U2") and S.e_q.shape == (3, 2, 5)
    assert S.av[0].tolist() == [100.0, 200.0]
    assert S.e_p[1, 1, 0] == 52.0 and S.r_q[2, 0] == pytest.approx(22.0)
    assert S.index_of(pd.Timestamp("2026-07-01 00:30")) == 1
    assert list(S.index_of(pd.DatetimeIndex(["2026-07-01 01:00", "2026-07-01"]))) == [2, 0]
    with pytest.raises(KeyError):
        S.index_of(pd.Timestamp("2026-07-02"))
    p = S.period(0)
    assert isinstance(p, PeriodStack) and p.shape == (2, 5)


# --------------------------------------------------------------------------------------
# Price-taker awards
# --------------------------------------------------------------------------------------

PR = {"E": 200.0, "P": 3.0, "C": 12.0, "R": 16.0}


def test_taker_energy_rules():
    off = BessOffer(energy=((100.0, -20.0), (199.0, -10.0), (201.0, 30.0), (300.0, 40.0)))
    a = price_taker_clear(off, {**PR, "E": 250.0})
    # charge bands priced above 250 -> none; discharge bands below 250 -> 30 MW
    assert a.chg == 0 and a.dis == pytest.approx(30.0)
    a = price_taker_clear(off, {**PR, "E": 150.0})
    assert a.chg == pytest.approx(10.0) and a.dis == 0
    a = price_taker_clear(off, {**PR, "E": 50.0})
    assert a.chg == pytest.approx(30.0)
    assert list(a.chg_bands) == [20.0, 10.0]


def test_taker_ties():
    off = BessOffer(energy=((200.0, 30.0),), regulation=((16.0, 50.0),))
    s = price_taker_clear(off, PR, tie="strict")
    i = price_taker_clear(off, PR, tie="inclusive")
    assert s.dis == 0 and s.awards["R"] == 0
    assert i.dis == pytest.approx(30.0) and i.awards["R"] == pytest.approx(50.0)
    off = BessOffer(energy=((200.0, -30.0),))
    assert price_taker_clear(off, PR, tie="strict").chg == 0
    assert price_taker_clear(off, PR, tie="inclusive").chg == pytest.approx(30.0)
    with pytest.raises(ValueError):
        price_taker_clear(off, PR, tie="maybe")


def test_taker_joint_limits_by_margin():
    # 80 dis (margin 200) + 100 R (margin 16) + 50 C (margin 12): dis first, then R gets 20
    off = BessOffer(energy=((0.0, 80.0),), regulation=((0.0, 100.0),), contingency=((0.0, 50.0),))
    a = price_taker_clear(off, PR).awards
    assert a["dis"] == pytest.approx(80.0) and a["R"] == pytest.approx(20.0) and a["C"] == 0
    # charge side: chg + R <= 100, P/C only on the discharge side
    off = BessOffer(energy=((3000.0, -60.0),), regulation=((0.0, 100.0),), contingency=((0.0, 50.0),))
    a = price_taker_clear(off, PR).awards
    assert a["chg"] == pytest.approx(60.0) and a["R"] == pytest.approx(40.0) and a["C"] == pytest.approx(50.0)
    # strict-rule bids never hit the rows
    off = BessOffer.from_period_bid(energy_fixed("2026-10-01", 30, 40, Config()))
    a = price_taker_clear(off, PR).awards
    assert a["dis"] == pytest.approx(40.0) and a["R"] == pytest.approx(60.0)
