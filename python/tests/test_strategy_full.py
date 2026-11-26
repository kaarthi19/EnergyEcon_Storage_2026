"""The full strategy on the synthetic market: completeness, capacity and SOC rules under
every efficiency convention, event bans, determinism, and the per-period information cut
(bids of periods up to T must not change when data that is not yet legal at T's gate
changes)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import synth_market as sm
from perturb import perturb_clearing, perturb_predispatch, perturb_stacks

from bessbid.backtest import MarketData, simulate
from bessbid.bids import offered_capacity, to_table
from bessbid.config import Config
from bessbid.infoset import InfoSet
from bessbid.soc import check_soc
from bessbid.strategy import check_complete, get_strategy
from bessbid.strategy_full import FullParams, FullStrategy, plan_month

MONTH = "2026-08"
CFG = Config()
PARAMS = FullParams(levels=(-50.0, 0.0, 50.0, 100.0), use_nodes=False)


@pytest.fixture(scope="module")
def world():
    return sm.build(days=70)


@pytest.fixture(scope="module")
def planned(world):
    info = sm.info_from(world)
    bids, frame, inf = plan_month(MONTH, info, CFG, PARAMS)
    info.assert_no_leak()
    return bids, frame, inf, info


def test_plan_is_complete_and_within_rules(planned):
    bids, frame, inf, _ = planned
    check_complete(bids, MONTH)
    assert len(frame) == len(bids) == 31 * 48
    for pb in bids:
        assert offered_capacity(pb, "strict") <= 100 + 1e-9
        assert not pb.primary and not pb.contingency
        e = sum(b.mw for b in pb.energy)
        reg = sum(b.mw for b in pb.regulation)
        assert reg == pytest.approx(100 - abs(e))
        for b in pb.energy:  # always-clearing prices
            assert b.price == (CFG.discharge_floor_price if b.mw > 0 else CFG.charge_cap_price)
    mw = frame["energy_mw"].to_numpy()
    assert (mw != 0).sum() > 20  # the overlay trades
    assert mw.min() >= -PARAMS.charge_cap_mw
    assert (mw[~frame["event"].to_numpy()] <= PARAMS.discharge_cap_mw).all()
    assert (mw[frame["event"].to_numpy()] >= 0).all()  # no charging inside event windows


def test_plan_soc_feasible_under_every_convention(planned):
    bids, frame, inf, _ = planned
    mw = frame["energy_mw"].to_numpy()
    dis, chg = np.clip(mw, 0, None), np.clip(-mw, 0, None)
    c = check_soc(dis, chg, CFG, margin=CFG.soc_margin_mwh - 1e-6, terminal_mwh=50.0)
    assert c.ok, c.to_dict()
    for k, v in c.by_convention.items():
        assert v["final"] >= 50 - 1e-6
    socs = [col for col in frame.columns if col.startswith("soc_c")]
    assert len(socs) == 3
    assert inf["soc_check"]["ok"]


def test_plan_replays_without_breach_in_backtest(world, planned):
    bids, *_ = planned
    md = MarketData.from_frames(world["stacks"], world["clearing"])
    tbl = to_table(bids)
    for mode in ("maker", "taker"):
        for conv in ("charge", "sqrt", "discharge"):
            r = simulate(tbl, MONTH, md, mode=mode, convention=conv, cfg=CFG)
            assert r.summary["breach_periods"] == 0
            assert r.summary["dis_mwh"] == pytest.approx(inf_dis(bids))


def inf_dis(bids):
    return sum(pb.discharge_mw for pb in bids) * 0.5


def test_plan_is_deterministic(world, planned):
    bids, *_ = planned
    info = sm.info_from(world)
    bids2, _, _ = plan_month(MONTH, info, CFG, PARAMS)
    a, b = to_table(bids), to_table(bids2)
    pd.testing.assert_frame_equal(a, b)


def test_bids_up_to_T_ignore_data_not_legal_at_T(world, planned):
    """Change every row not legal at T's gate; bids for periods <= T must not move."""
    bids, *_ = planned
    T = pd.Timestamp("2026-08-12 14:00")
    t = T - pd.Timedelta(minutes=CFG.gate_closure_min)
    info = InfoSet(CFG, perturb_clearing(world["clearing"], t), perturb_predispatch(world["predispatch"], t),
                   None, perturb_stacks(world["stacks"], t))
    bids2, _, _ = plan_month(MONTH, info, CFG, PARAMS)
    info.assert_no_leak()
    a = [pb for pb in bids if pb.start <= T]
    b = [pb for pb in bids2 if pb.start <= T]
    assert len(a) == len(b) > 500
    pd.testing.assert_frame_equal(to_table(a), to_table(b))


@pytest.mark.parametrize("params", [
    FullParams(levels=(-50.0, 0.0, 50.0, 100.0), use_nodes=False, plan_time="gate"),
    FullParams(levels=(-50.0, 0.0, 50.0, 100.0), use_nodes=False, refine="lead2", lead2_scope="events"),
], ids=["gate", "lead2-events"])
def test_variants_respect_the_cut(world, params):
    """The later daily decision time and the lead-2 h refinement are legal too."""
    T = pd.Timestamp("2026-08-09 17:30")
    t = T - pd.Timedelta(minutes=CFG.gate_closure_min)
    info = sm.info_from(world)
    bids, frame, _ = plan_month(MONTH, info, CFG, params)
    info.assert_no_leak()
    mw = frame["energy_mw"].to_numpy()
    assert check_soc(np.clip(mw, 0, None), np.clip(-mw, 0, None), CFG, terminal_mwh=50.0).ok
    info2 = InfoSet(CFG, perturb_clearing(world["clearing"], t), perturb_predispatch(world["predispatch"], t),
                    None, perturb_stacks(world["stacks"], t))
    bids2, _, _ = plan_month(MONTH, info2, CFG, params)
    a = [pb for pb in bids if pb.start <= T]
    b = [pb for pb in bids2 if pb.start <= T]
    pd.testing.assert_frame_equal(to_table(a), to_table(b))


def test_registry_and_generate(world):
    s = get_strategy("full", params=PARAMS)
    assert isinstance(s, FullStrategy) and s.needs_data
    with pytest.raises(ValueError):
        s.generate(MONTH, None, CFG)
    with pytest.raises(ValueError):
        FullParams(refine="sometimes")
    with pytest.raises(ValueError):
        FullParams(plan_time="noon")
    with pytest.raises(ValueError):
        FullParams(lead2_scope="weekends")
