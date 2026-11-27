"""The event-window bias correction of the planning energy price (``event_bias``).

Bucket labels on a hand-made calendar, the bucket estimator (mean, clip, shrinkage, no
correction outside events, in overlaps or in unseen buckets), the backcast mode of the
forecaster, and on the synthetic market: the correction uses only clearing periods that
ended before the fit cut (perturbing everything later leaves it bit-identical), the plan
keeps every rule, a zero correction reproduces the default bids exactly, and bids up to T
ignore data not legal at T's gate."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import synth_market as sm
from perturb import perturb_clearing, perturb_predispatch, perturb_stacks

import bessbid.eventbias as ebm
from bessbid.bids import offered_capacity, to_table
from bessbid.calendar import Cycle, EventCalendar, Window
from bessbid.config import Config
from bessbid.eventbias import OVERLAP, EventBias, backcast_residuals, event_keys, fit_event_bias
from bessbid.forecast import Forecaster
from bessbid.infoset import InfoSet
from bessbid.soc import check_soc
from bessbid.strategy_full import FullParams, plan_month

MONTH = "2026-09"  # the synthetic stacks start Jul 1 and lag 28 days: July cannot be backcast
CUT = pd.Timestamp("2026-08-31")
CFG = Config()
BASE = dict(levels=(-50.0, 0.0, 50.0, 100.0), use_nodes=False)


def _calendar() -> EventCalendar:
    """19-day stress cycle (onset Aug 10 12:00, 60 h, surge Aug 11 00:00 - Aug 12 11:30) and
    an 11-day cycle (onset Aug 11 15:00, 16.5 h, to Aug 12 07:00) that overlaps it."""
    stress = Cycle("cycle19d", 19, pd.Timestamp("2026-08-10 12:00"), 120, 700.0, 50.0, 5)
    short = Cycle("cycle11d", 11, pd.Timestamp("2026-08-11 15:00"), 33, 670.0, 50.0, 8)
    surge = Window("cycle19d", 24, 72, 670.0)
    return EventCalendar(pd.Timestamp("2026-07-31"), [short, stress], None, surge, 110.0, 480.0, 105.0, 400.0)


TS = pd.date_range("2026-08-10 00:00", "2026-08-13 23:30", freq="30min")


def test_event_keys_phase_surge_hour_and_overlap():
    k = pd.Series(event_keys(_calendar(), TS, 3.0), index=TS)
    assert (k[:"2026-08-10 11:30"] == "").all() and (k["2026-08-13"] == "").all()
    assert k["2026-08-10 12:00"] == "cycle19d:d0:n:h12"
    assert k["2026-08-10 23:30"] == "cycle19d:d0:n:h21"
    assert k["2026-08-11 00:00"] == "cycle19d:d1:s:h00"
    assert k["2026-08-11 14:30"] == "cycle19d:d1:s:h12"
    assert (k["2026-08-11 15:00":"2026-08-12 07:00"] == OVERLAP).all()  # both windows active
    assert k["2026-08-12 07:30"] == "cycle19d:d2:s:h06"
    assert k["2026-08-12 12:00"] == "cycle19d:d2:n:h12"  # surge window over
    k6 = event_keys(_calendar(), pd.DatetimeIndex(["2026-08-11 05:30"]), 6.0)
    assert k6[0] == "cycle19d:d1:s:h00"
    short_only = Cycle("cycle11d", 11, pd.Timestamp("2026-08-01 15:00"), 33, 670.0, 50.0, 8)
    cal = EventCalendar(CUT, [short_only], None, None, 110.0, 480.0, 105.0, 400.0)
    assert list(event_keys(cal, pd.DatetimeIndex(["2026-08-01 15:00", "2026-08-02 07:00", "2026-08-02 07:30"]))) \
        == ["cycle11d:d0:n:h15", "cycle11d:d1:n:h06", ""]


def test_fit_from_residuals_mean_clip_shrink_and_zero_elsewhere():
    cal = _calendar()
    Ts = pd.DatetimeIndex(["2026-08-11 00:00", "2026-08-11 00:30", "2026-08-11 01:00",
                           "2026-08-10 12:00", "2026-08-11 16:00"])
    res = pd.DataFrame({"forecast": 0.0, "actual": 0.0, "resid": [40.0, 80.0, 2000.0, -12.0, 999.0],
                        "day": Ts.normalize()}, index=Ts)

    class F:  # only the calendar is used when residuals are given
        calendar = cal

    eb = fit_event_bias(F, CUT, block_hours=3.0, clip=500.0, shrink=0.0, residuals=res)
    t = eb.table
    assert t.loc["cycle19d:d1:s:h00", "delta"] == pytest.approx((40 + 80 + 500) / 3)  # clipped
    assert t.loc["cycle19d:d0:n:h12", "delta"] == pytest.approx(-12.0)
    assert OVERLAP not in t.index  # the overlap residual is dropped
    eb2 = fit_event_bias(F, CUT, block_hours=3.0, clip=500.0, shrink=3.0, residuals=res)
    assert eb2.table.loc["cycle19d:d1:s:h00", "delta"] == pytest.approx((620 / 3) * 3 / 6)
    d = pd.Series(eb.delta(cal, TS), index=TS)
    assert (d[:"2026-08-10 11:30"] == 0).all()  # outside events
    assert (d["2026-08-11 15:00":"2026-08-12 07:00"] == 0).all()  # overlap: never corrected
    assert (d["2026-08-12 12:00":] == 0).all()  # bucket without residuals
    assert np.allclose(d["2026-08-11 00:00":"2026-08-11 02:30"], 620 / 3)
    assert (d["2026-08-10 12:00":"2026-08-10 14:30"] == -12.0).all()
    assert not EventBias(CUT, 3.0).delta(cal, TS).any()


def test_params_validate_and_read_from_config():
    for kw in (dict(event_bias="quantile"), dict(event_bias_hours=5.0), dict(event_bias_hours=0.0),
               dict(event_bias_clip=0.0), dict(event_bias_shrink=-1.0)):
        with pytest.raises(ValueError):
            FullParams(**kw)
    p = FullParams.from_mapping({"event_bias": "residual", "event_bias_hours": 6, "event_bias_shrink": 12})
    assert p.event_bias == "residual" and p.event_bias_hours == 6.0 and p.event_bias_shrink == 12.0
    assert FullParams().event_bias == "none"


@pytest.fixture(scope="module")
def world():
    return sm.build(days=100)


def _fit(info):
    fc = Forecaster(info, CFG, nd_history_cap=True).fit(CUT)
    return fc, fit_event_bias(fc, CUT, nodes=fc.nd.nodes_da)


def test_backcast_mode_is_scoped(world):
    fc = Forecaster(sm.info_from(world), CFG, nd_history_cap=True).fit(CUT)
    T = pd.Timestamp("2026-08-20 10:00")
    with pytest.raises(ValueError):
        fc.stacks.forecast_stack(T, T - pd.Timedelta(days=1))
    with fc.backcast():
        fc.stacks.forecast_stack(T, T - pd.Timedelta(days=1))
        fc.nd.profile([T], T - pd.Timedelta(days=1))
    with pytest.raises(ValueError):
        fc.nd.profile([T], T - pd.Timedelta(days=1))


def test_residuals_use_only_periods_ended_before_the_cut(world):
    info = sm.info_from(world)
    fc, eb = _fit(info)
    res = backcast_residuals(fc, CUT, nodes=fc.nd.nodes_da)
    assert len(res) > 0 and len(eb.table) > 0
    assert (res.index + pd.Timedelta(minutes=30) < CUT).all()
    entries = [e for e in info.audit if e.accessor == "eventbias.fit"]
    assert len(entries) == 2  # one per fit
    assert all(e.decision_time == CUT and e.max_time_used < CUT for e in entries)
    info.assert_no_leak()
    with pytest.raises(ValueError):
        backcast_residuals(fc, CUT + pd.Timedelta(days=1))


def test_correction_ignores_every_row_not_known_at_the_cut(world):
    _, eb = _fit(sm.info_from(world))
    info2 = InfoSet(CFG, perturb_clearing(world["clearing"], CUT), perturb_predispatch(world["predispatch"], CUT),
                    None, perturb_stacks(world["stacks"], CUT))
    _, eb2 = _fit(info2)
    pd.testing.assert_frame_equal(eb.table, eb2.table)
    # control: changing a clearing price that was known at the cut changes the correction
    c = world["clearing"].copy()
    k = eb.table.index[0]
    fc0 = Forecaster(sm.info_from(world), CFG, nd_history_cap=True).fit(CUT)
    keys = event_keys(fc0.calendar, pd.DatetimeIndex(c["ts"]))
    m = (keys == k) & ((c["ts"] + pd.Timedelta(minutes=30)) < CUT).to_numpy()
    c.loc[m, "energy_price_sgd_mwh"] += 50.0
    _, eb3 = _fit(InfoSet(CFG, c, world["predispatch"], None, world["stacks"]))
    assert eb3.table.loc[k, "delta"] != pytest.approx(eb.table.loc[k, "delta"])


@pytest.fixture(scope="module")
def default_plan(world):
    info = sm.info_from(world)
    return plan_month(MONTH, info, CFG, FullParams(**BASE))


@pytest.fixture(scope="module")
def bias_plan(world):
    info = sm.info_from(world)
    p = FullParams(**BASE, event_bias="residual")
    bids, frame, inf = plan_month(MONTH, info, CFG, p)
    info.assert_no_leak()
    return bids, frame, inf, p


def test_bias_plan_keeps_every_rule_and_reports_the_correction(bias_plan):
    bids, frame, inf, p = bias_plan
    d = frame["fc_event_bias_E"].to_numpy()
    ev = frame["event"].to_numpy()
    assert np.any(d != 0) and not np.any(d[~ev])  # corrections only inside event windows
    assert inf["event_bias"]["corrected_periods"] == int(np.count_nonzero(d))
    assert inf["event_bias"]["buckets"]
    mw = frame["energy_mw"].to_numpy()
    assert ((mw >= 0) | ~ev).all()  # the event charging ban is unchanged
    for pb in bids:
        assert offered_capacity(pb, "strict") <= CFG.power_mw + 1e-9
    steps = np.abs(np.diff(np.concatenate([[0.0], mw])))
    assert steps.max() <= CFG.bess_ramp_mw + 1e-9
    c = check_soc(np.clip(mw, 0, None), np.clip(-mw, 0, None), CFG, margin=CFG.soc_margin_mwh,
                  terminal_mwh=CFG.terminal_soc_frac * CFG.energy_mwh)
    assert c.ok, c.to_dict()


def test_default_plan_has_no_correction_column(default_plan):
    _, frame, inf = default_plan
    assert "fc_event_bias_E" not in frame.columns and "event_bias" not in inf


def test_zero_correction_reproduces_the_default(world, default_plan, monkeypatch):
    monkeypatch.setattr(ebm, "fit_event_bias", lambda fc, cut, **kw: EventBias(pd.Timestamp(cut), 3.0))
    bids, frame, _ = plan_month(MONTH, sm.info_from(world), CFG, FullParams(**BASE, event_bias="residual"))
    assert not frame["fc_event_bias_E"].any()
    pd.testing.assert_frame_equal(to_table(bids), to_table(default_plan[0]))


@pytest.mark.parametrize("T", ["2026-09-01 00:00", "2026-09-12 13:30"])
def test_bias_bids_up_to_T_ignore_data_not_legal_at_T(world, bias_plan, T):
    bids, _, _, p = bias_plan
    T = pd.Timestamp(T)
    t = T - pd.Timedelta(minutes=CFG.gate_closure_min)
    info2 = InfoSet(CFG, perturb_clearing(world["clearing"], t), perturb_predispatch(world["predispatch"], t),
                    None, perturb_stacks(world["stacks"], t))
    bids2, _, _ = plan_month(MONTH, info2, CFG, p)
    a = [pb for pb in bids if pb.start <= T]
    b = [pb for pb in bids2 if pb.start <= T]
    assert len(a) == len(b) > 0
    pd.testing.assert_frame_equal(to_table(a), to_table(b))
