"""The overnight recharge exception to the event charging ban (``event_recharge``).

Unit tests of the eligibility mask and the allowed-action matrix on a hand-made calendar,
then the full strategy on the synthetic market: charging inside event windows happens only
in eligible night periods, every rule still holds, the information cut is kept, and an
exception whose price condition can never hold reproduces the default bids exactly."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import synth_market as sm
from perturb import perturb_clearing, perturb_predispatch, perturb_stacks

from bessbid.bids import offered_capacity, to_table
from bessbid.calendar import Cycle, EventCalendar, Window
from bessbid.config import Config
from bessbid.infoset import InfoSet
from bessbid.planner import make_action_grid
from bessbid.soc import check_soc
from bessbid.strategy_full import FullParams, _allowed, _recharge_mask, plan_month

MONTH = "2026-08"
CFG = Config()
BASE = dict(levels=(-50.0, 0.0, 50.0, 100.0), use_nodes=False)


def _calendar() -> EventCalendar:
    """A 19-day stress cycle (onset Aug 10 12:00, 60 h) with a surge window, and an 11-day
    cycle (onset Aug 11 15:00, 16.5 h) that overlaps the second stress night."""
    stress = Cycle("cycle19d", 19, pd.Timestamp("2026-08-10 12:00"), 120, 700.0, 50.0, 5)
    short = Cycle("cycle11d", 11, pd.Timestamp("2026-08-11 15:00"), 33, 670.0, 50.0, 8)
    surge = Window("cycle19d", 24, 72, 670.0)
    return EventCalendar(pd.Timestamp("2026-07-31"), [short, stress], None, surge, 110.0, 480.0, 105.0, 400.0)


TS = pd.date_range("2026-08-10 00:00", "2026-08-13 23:30", freq="30min")


def test_params_validate_and_read_from_config():
    with pytest.raises(ValueError):
        FullParams(event_recharge="always")
    with pytest.raises(ValueError):
        FullParams(event_recharge_hours=(0.0, 25.0))
    with pytest.raises(ValueError):
        FullParams(event_recharge_hours=(1.0,))
    with pytest.raises(ValueError):
        FullParams(event_recharge_hurdle=-1.0)
    p = FullParams.from_mapping({"event_recharge": "overnight", "event_recharge_hours": [22, 6],
                                 "event_recharge_max_price": 300})
    assert p.event_recharge_hours == (22.0, 6.0) and p.event_recharge_max_price == 300.0
    assert FullParams().event_recharge == "none"


def test_recharge_mask_none_is_empty():
    assert not _recharge_mask(_calendar(), TS, FullParams()).any()


def test_recharge_mask_overnight_only_stress_nights_without_overlap():
    cal = _calendar()
    m = pd.Series(_recharge_mask(cal, TS, FullParams(event_recharge="overnight")), index=TS)
    on = m[m].index
    assert len(on) > 0
    assert (on.hour < 7).all()  # night hours [0, 7)
    t = on.to_numpy(dtype="datetime64[ns]")
    assert cal.cycle("cycle19d").active(t).all()
    assert not cal.cycle("cycle11d").active(t).any()  # the 11-day overlap stays banned
    # stress event Aug 10 12:00 - Aug 12 23:30; 11-day event Aug 11 15:00 - Aug 12 07:00.
    # Aug 11 night: stress only -> eligible; Aug 12 night: both overlap -> banned; Aug 13:
    # no event.
    assert m["2026-08-11 00:00":"2026-08-11 06:30"].all()
    assert not m["2026-08-11 07:00":"2026-08-11 23:30"].any()
    assert not m["2026-08-12 00:00":"2026-08-12 06:30"].any()
    assert not m["2026-08-10"].any() and not m["2026-08-13"].any()


def test_recharge_mask_overnight_all_adds_the_short_cycle_and_wraps_hours():
    cal = _calendar()
    m_all = pd.Series(_recharge_mask(cal, TS, FullParams(event_recharge="overnight_all")), index=TS)
    assert m_all["2026-08-11 00:00":"2026-08-11 06:30"].all()
    assert m_all["2026-08-12 00:00":"2026-08-12 06:30"].all()  # the overlap night is included
    m_w = pd.Series(_recharge_mask(cal, TS, FullParams(event_recharge="overnight_all",
                                                        event_recharge_hours=(22.0, 2.0))), index=TS)
    h = m_w[m_w].index.hour
    assert set(h) <= {22, 23, 0, 1} and {22, 23, 0, 1} <= set(h)


def test_recharge_mask_needs_a_stress_cycle_for_overnight():
    cal = _calendar()
    cal.surge = None
    assert not _recharge_mask(cal, TS, FullParams(event_recharge="overnight")).any()
    assert _recharge_mask(cal, TS, FullParams(event_recharge="overnight_all")).any()


def test_allowed_lifts_the_ban_only_where_cheap():
    grid = make_action_grid(CFG, max_charge_mw=50.0, max_discharge_mw=100.0)
    chg = grid.mw < 0
    ev = np.array([True, True, True, False])
    rech = np.array([True, True, False, False])
    price = np.array([300.0, 400.0, 100.0, 900.0])
    p = FullParams(event_recharge="overnight", event_recharge_max_price=350.0)
    A = _allowed(grid, ev, np.full(4, 1000.0), p, rech, price)
    assert A[0, chg].all()  # eligible and cheap
    assert not A[1, chg].any()  # eligible but too expensive
    assert not A[2, chg].any()  # not eligible
    assert A[3, chg].all()  # outside events: unchanged
    # the margin ban still applies inside the exception
    A2 = _allowed(grid, ev, np.array([200.0, 1000.0, 1000.0, 1000.0]), p, rech, price)
    assert not A2[0, chg].any()
    # without the price input the exception is off
    assert not _allowed(grid, ev, None, p, rech, None)[0, chg].any()


@pytest.fixture(scope="module")
def world():
    return sm.build(days=70)


@pytest.fixture(scope="module")
def default_plan(world):
    info = sm.info_from(world)
    bids, frame, inf = plan_month(MONTH, info, CFG, FullParams(**BASE))
    return bids, frame, inf


@pytest.fixture(scope="module")
def recharge_plan(world):
    info = sm.info_from(world)
    p = FullParams(**BASE, event_recharge="overnight_all", event_recharge_hours=(18.0, 9.0),
                   event_recharge_max_price=1e6)
    bids, frame, inf = plan_month(MONTH, info, CFG, p)
    info.assert_no_leak()
    return bids, frame, inf, p


def test_default_plan_never_charges_in_events(default_plan):
    _, frame, inf = default_plan
    assert not frame["event_recharge_ok"].any()
    assert ((frame["energy_mw"] >= 0) | ~frame["event"]).all()
    assert inf["event_charge_periods"] == 0 and inf["event_charge_mwh"] == 0.0


def test_recharge_plan_charges_only_where_eligible_and_keeps_every_rule(recharge_plan):
    bids, frame, inf, p = recharge_plan
    mw = frame["energy_mw"].to_numpy()
    ev = frame["event"].to_numpy()
    ok = frame["event_recharge_ok"].to_numpy()
    assert ok.any() and not (ok & ~ev).any()
    assert ((mw >= 0) | ~ev | ok).all()  # event charging only in eligible periods
    assert inf["event_charge_periods"] == int(((mw < 0) & ev).sum()) > 0  # the exception is used
    assert mw.min() >= -p.charge_cap_mw
    for pb in bids:
        assert offered_capacity(pb, "strict") <= CFG.power_mw + 1e-9
    steps = np.abs(np.diff(np.concatenate([[0.0], mw])))
    assert steps.max() <= CFG.bess_ramp_mw + 1e-9
    c = check_soc(np.clip(mw, 0, None), np.clip(-mw, 0, None), CFG, margin=CFG.soc_margin_mwh,
                  terminal_mwh=CFG.terminal_soc_frac * CFG.energy_mwh)
    assert c.ok, c.to_dict()


@pytest.mark.parametrize("kw", [dict(event_recharge_max_price=-1e9), dict(event_recharge_hurdle=1e9)],
                         ids=["price-never-low", "prohibitive-hurdle"])
def test_exception_that_never_pays_reproduces_the_default(world, default_plan, kw):
    info = sm.info_from(world)
    p = FullParams(**BASE, event_recharge="overnight_all", event_recharge_hours=(18.0, 9.0), **kw)
    bids, frame, inf = plan_month(MONTH, info, CFG, p)
    assert frame["event_recharge_ok"].any()
    assert inf["event_charge_periods"] == 0
    pd.testing.assert_frame_equal(to_table(bids), to_table(default_plan[0]))


def test_recharge_bids_up_to_T_ignore_data_not_legal_at_T(world, recharge_plan):
    bids, _, _, p = recharge_plan
    T = pd.Timestamp("2026-08-12 03:30")
    t = T - pd.Timedelta(minutes=CFG.gate_closure_min)
    info2 = InfoSet(CFG, perturb_clearing(world["clearing"], t), perturb_predispatch(world["predispatch"], t),
                    None, perturb_stacks(world["stacks"], t))
    bids2, _, _ = plan_month(MONTH, info2, CFG, p)
    a = [pb for pb in bids if pb.start <= T]
    b = [pb for pb in bids2 if pb.start <= T]
    assert len(a) == len(b) > 0
    pd.testing.assert_frame_equal(to_table(a), to_table(b))
