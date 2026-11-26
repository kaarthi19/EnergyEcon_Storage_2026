"""Walk-forward evaluation harness and the perfect-foresight bound on the synthetic market."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import synth_market as sm

from bessbid.backtest import MarketData, simulate
from bessbid.bids import offered_capacity, to_table
from bessbid.config import Config
from bessbid.evaluate import evaluate, perfect_foresight_plan, summarize
from bessbid.soc import check_soc
from bessbid.strategy import check_complete
from bessbid.strategy_full import FullParams

MONTH = "2026-08"
CFG = Config()
PARAMS = FullParams(levels=(-50.0, 0.0, 50.0, 100.0), use_nodes=False)


@pytest.fixture(scope="module")
def world():
    return sm.build(days=70)


@pytest.fixture(scope="module")
def md(world):
    return MarketData.from_frames(world["stacks"], world["clearing"])


def test_perfect_foresight_bound(md):
    bids, val = perfect_foresight_plan(MONTH, md, CFG, cap_mw=50.0, levels=(-50.0, 0.0, 50.0))
    check_complete(bids, MONTH)
    e = np.array([sum(b.mw for b in pb.energy) for pb in bids])
    assert np.abs(e).max() <= 50 + 1e-9 and (e != 0).sum() > 0
    assert all(offered_capacity(pb, "strict") <= 100 + 1e-9 for pb in bids)
    c = check_soc(np.clip(e, 0, None), np.clip(-e, 0, None), CFG, margin=CFG.soc_margin_mwh - 1e-6,
                  terminal_mwh=50.0)
    assert c.ok
    tb = to_table(bids)
    s = simulate(tb, MONTH, md, mode="maker", cfg=CFG).summary
    reg = simulate(to_table([pb for pb in _reg(MONTH)]), MONTH, md, mode="maker", cfg=CFG).summary
    # hindsight with the actual market and our own price impact: at least as good as Regulation only
    assert s["revenue"] >= reg["revenue"] - 1e-6
    # the programme's value is the maker revenue up to the ramp-state approximation
    assert val == pytest.approx(s["revenue"], rel=0.02)


def _reg(month):
    from bessbid.bids import regulation_only
    from bessbid.periods import month_slots

    return [regulation_only(s.date, s.period, CFG) for s in month_slots(month)]


def test_evaluate_rows_and_uplift(world, md):
    res, plans = evaluate(None, CFG, months=[MONTH], params=PARAMS, conventions=("discharge", "sqrt", "charge"),
                          perfect_foresight=(50.0,), data=md, info_factory=lambda c: sm.info_from(world, c))
    assert set(plans) == {MONTH} and len(plans[MONTH]) == 31 * 48
    one = res[res["month"] == MONTH]
    # regulation x 2 modes, full x 2 modes x 3 conventions, pf x 2 modes
    assert len(one) == 2 + 6 + 2
    assert (one.loc[one.policy == "regulation", "uplift"].abs() < 1e-9).all()
    full = one[one.policy == "full"]
    assert (full["breach_periods"] == 0).all()
    # always-clearing bids: revenue does not depend on the efficiency convention
    for mode, g in full.groupby("mode"):
        assert g["revenue"].nunique() == 1
        assert g["soc_min"].min() >= CFG.soc_min_mwh and g["soc_max"].max() <= CFG.soc_max_mwh
    allr = res[res["month"] == "ALL"]
    assert set(allr["policy"]) == {"regulation", "full", "pf_cap50"}
    assert (allr["months_positive"] <= 1).all()


def test_summarize_all_rows():
    df = pd.DataFrame([
        dict(policy="regulation", month="m1", mode="maker", convention="c", revenue=100.0, days=30.0),
        dict(policy="full", month="m1", mode="maker", convention="c", revenue=130.0, days=30.0),
        dict(policy="regulation", month="m2", mode="maker", convention="c", revenue=100.0, days=31.0),
        dict(policy="full", month="m2", mode="maker", convention="c", revenue=90.0, days=31.0),
    ])
    for k in ("rev_E", "rev_P", "rev_C", "rev_R", "dis_mwh", "chg_mwh", "energy_periods", "breach_periods",
              "runtime_s", "soc_min", "soc_max", "breach_max_mwh"):
        df[k] = 0.0
    s = summarize(df)
    a = s[(s.month == "ALL") & (s.policy == "full")].iloc[0]
    assert a["uplift"] == pytest.approx(20.0)
    assert a["uplift_per_30d"] == pytest.approx(20.0 / 61 * 30)
    assert a["months_positive"] == 1
