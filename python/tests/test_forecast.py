"""Forecast pipeline on a synthetic market (no EMA data): clearing LP, calendar learning,
stack model, net demand, Forecaster, scenarios and the no-leak property."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import synth_market as sm
from perturb import frames_equal, perturb_clearing, perturb_predispatch, perturb_stacks

from bessbid import _clearlp
from bessbid.calendar import learn_calendar, outage_states
from bessbid.config import Config
from bessbid.forecast import KEYS, Forecaster, evaluate, walk_forward
from bessbid.infoset import InfoSet, LeakError
from bessbid.netdemand import NetDemandModel
from bessbid.stackmodel import StackModel

T = pd.Timestamp
CUT = T("2026-08-25 00:00")


@pytest.fixture(scope="module")
def world():
    return sm.build()


@pytest.fixture
def info(world):
    return sm.info_from(world)


@pytest.fixture(scope="module")
def fitted(world):
    info = sm.info_from(world)
    return Forecaster(info).fit(CUT, residual_start="2026-08-12")


# ---- clearing LP -------------------------------------------------------------------------
def _two_units():
    Eq = np.array([[100.0, 100.0], [100.0, 100.0]])
    Ep = np.array([[10.0, 20.0], [15.0, 30.0]])
    z = np.zeros(2)
    return Eq, Ep, z


def test_clearlp_merit_order_price():
    Eq, Ep, z = _two_units()
    av = np.array([200.0, 200.0])
    r = _clearlp.clear(Eq, Ep, z, z, z, z, z, z, av, 150.0, (0, 0, 0))
    assert r.E == pytest.approx(15.0)
    r = _clearlp.clear(Eq, Ep, z, z, z, z, z, z, av, 250.0, (0, 0, 0))
    assert r.E == pytest.approx(20.0)
    assert r.gen.sum() == pytest.approx(250.0)


def test_clearlp_shortfall_prices_and_ramp():
    Eq, Ep, z = _two_units()
    av = np.array([200.0, 200.0])
    pq = np.array([20.0, 20.0])
    pp = np.array([2.0, 3.0])
    # 50 MW primary needed, only 40 offered -> shortfall priced at the penalty
    r = _clearlp.clear(Eq, Ep, pq, pp, z, z, z, z, av, 100.0, (50.0, 0, 0))
    assert r.P == pytest.approx(750.0) and r.sP == pytest.approx(10.0)
    # ramp: unit 0 was at 0 MW, may rise by 0.42*200 = 84 MW only
    q = _clearlp.trim_ramp(Eq, av, np.array([0.0, 200.0]), 0.42)
    assert q[0].sum() == pytest.approx(84.0) and q[1].sum() == pytest.approx(200.0)
    # demand above all supply -> VoLL
    r = _clearlp.clear(Eq, Ep, z, z, z, z, z, z, av, 450.0, (0, 0, 0))
    assert r.E == pytest.approx(4500.0) and r.unserved == pytest.approx(50.0)


def test_clearlp_agrees_with_engine(world):
    engine = pytest.importorskip("bessbid.engine")
    s = sm.info_from(world).stack_arrays()
    c = world["clearing"]
    for i in range(0, 3000, 157):
        args = (s.Eq[i], s.Ep[i], s.Pq[i], s.Pp[i], s.Cq[i], s.Cp[i], s.Rq[i], s.Rp[i], s.av[i])
        req = (110.0, float(c["contingency_requirement_mw"].iloc[i]), 105.0)
        a = _clearlp.clear(*args, float(world["nd"][i]), req)
        b = engine.clear_period(*args, float(world["nd"][i]), req)
        assert np.allclose(a.prices(), b.price_tuple(), atol=1e-6)


# ---- calendar ----------------------------------------------------------------------------
def test_calendar_learns_cycles(info):
    cal = learn_calendar(info, CUT)
    assert [(c.period_days, c.duration_periods, c.mw) for c in cal.cycles] == [(5, 20, 900.0), (7, 60, 800.0)]
    a5, a7 = cal.cycles[0].anchor, cal.cycles[1].anchor
    assert ((a5 - (sm.START + sm.CYC_A[1])) / pd.Timedelta(days=1)) % 5 == 0
    assert ((a7 - (sm.START + sm.CYC_B[1])) / pd.Timedelta(days=1)) % 7 == 0
    w = cal.contingency
    assert (w.cycle, w.offset_periods, w.duration_periods, w.value, w.base) == ("cycle7d", 24, 36, 550.0, 480.0)
    assert (cal.surge.offset_periods, cal.surge.duration_periods) == (24, 36)
    assert cal.surge.value == pytest.approx(sm.SURGE, abs=30)
    assert cal.checks["outage_class_exceptions"] == 0
    assert cal.checks["contingency_rule_agreement"] == 1.0
    assert (cal.primary_req, cal.regulation_req) == (110.0, 105.0)


def test_calendar_classifies_all_states_and_future(world, info):
    cal = learn_calendar(info, T("2026-08-01"))
    states = outage_states(world["predispatch"])  # includes rows after the cut
    assert len(cal.exceptions(states)) == 0
    tbl = cal.events_table("2026-09-01", "2026-09-08")
    assert set(tbl["cycle"]) == {"cycle5d", "cycle7d"}
    assert tbl["surge_start"].notna().sum() >= 1


def test_expected_outage_semantics(info):
    cal = learn_calendar(info, CUT)
    c5 = cal.cycle("cycle5d")
    on = c5.onsets_between("2026-09-01", "2026-09-10")[0]
    # no event at issue time, event at T: known outage + learned event MW
    tau = on - pd.Timedelta(hours=2)
    if not cal.in_event(tau):
        assert cal.expected_outage(on, 10.0, tau) == pytest.approx(10.0 + 900.0)
    # event ongoing at issue and at T: keep the observed MW
    assert cal.expected_outage(on + pd.Timedelta(hours=3), 950.0, on + pd.Timedelta(hours=1)) == pytest.approx(950.0)
    # event over by T: outage drops
    end = on + c5.duration_periods * pd.Timedelta(minutes=30)
    if not cal.in_event(end):
        assert cal.expected_outage(end, 950.0, end - pd.Timedelta(hours=2)) == pytest.approx(0.0)
    assert cal.contingency_req(T("2026-09-01")) in (480.0, 550.0)


def test_calendar_uses_only_data_by_cut(world):
    t = T("2026-08-10 13:05")
    a = learn_calendar(sm.info_from(world), t)
    w2 = dict(world)
    w2["predispatch"] = perturb_predispatch(world["predispatch"], t)
    w2["clearing"] = perturb_clearing(world["clearing"], t)
    b = learn_calendar(sm.info_from(w2), t)
    assert a.to_dict() == b.to_dict()


# ---- stack model -------------------------------------------------------------------------
def test_stack_lags_are_published_same_pod(info):
    m = StackModel(info, n_lags=7).fit(CUT)
    rng = np.random.default_rng(1)
    for _ in range(20):
        Tt = CUT + pd.Timedelta(minutes=30 * int(rng.integers(48, 48 * 14)))
        d = info.cutoff(Tt)
        idx = m.lag_indices(Tt, d)
        a = m.arr
        assert 1 <= len(idx) <= 7
        assert (a.avail[idx] <= np.datetime64(d, "ns")).all()
        assert (a.pod[idx] == Tt.hour * 2 + Tt.minute // 30).all()
        assert (np.diff(a.ts[idx].astype("int64")) < 0).all()  # most recent first
        assert (np.datetime64(Tt, "ns") - a.ts[idx] >= np.timedelta64(29, "D")).all()
    with pytest.raises(ValueError):
        m.lag_indices(CUT + pd.Timedelta(days=2), CUT - pd.Timedelta(minutes=1))


def test_stack_factor_and_forecast(info):
    m = StackModel(info).fit(CUT)
    p = m.params["E"]
    assert 0.0002 < p.beta_out < 0.0006
    assert -0.12 < p.beta_we < -0.03
    assert m.nom.sum() == pytest.approx(sm.NOM.sum())
    sf0 = m.forecast_stack(T("2026-09-02 10:00"), T("2026-09-02 08:55"), 0.0)
    sf1 = m.forecast_stack(T("2026-09-02 10:00"), T("2026-09-02 08:55"), 800.0)
    assert np.allclose(sf1.Ep / sf0.Ep, np.exp(p.beta_out * 800.0))
    assert np.allclose(sf0.av, sm.NOM) and (sf0.Eq > 0).all()  # outaged units restored


def test_stack_copy_detection(world):
    s = world["stacks"]
    src = s[(s["ts"] >= T("2026-07-29")) & (s["ts"] < T("2026-07-31"))].copy()
    cp = src.copy()
    cp["ts"] = cp["ts"] - pd.Timedelta(days=28)
    cp["date"] = cp["ts"].dt.normalize()
    cp["pub"] = cp["ts"] + sm.PUB
    keep = s[(s["ts"] < T("2026-07-01")) | (s["ts"] >= T("2026-07-03"))]
    st = pd.concat([cp, keep], ignore_index=True)
    st["product"] = st["product"].astype("category")
    info = InfoSet(Config(), world["clearing"], world["predispatch"], None, st)
    m = StackModel(info).fit(T("2026-09-01"))
    assert len(m.copied) == 96  # Jul 1-2 copies of Jul 29-30 are left out of the fit
    m2 = StackModel(info).fit(T("2026-08-01"))  # Jul 29-30 not yet published
    assert len(m2.copied) == 0


# ---- net demand --------------------------------------------------------------------------
def test_netdemand_night_solar_and_profile(info, world):
    cal = learn_calendar(info, CUT)
    nd = NetDemandModel(info, cal, source="clearing").fit(CUT)
    night = np.flatnonzero(nd.night)
    assert set(range(0, 14)) <= set(night) and set(range(36, 48)) <= set(night)
    assert not nd.night[24]
    Tt = T("2026-09-03 02:00")
    f = nd.lead(Tt)
    row = world["predispatch"]
    r = row[(row["ts"] == Tt) & (row["lead_hours"] == 2.0)].iloc[0]
    assert f.lead_hours == 2.0 and f.nd == pytest.approx(r.forecast_load_mw)  # night solar zeroed
    # profile: surge MW added inside the window
    day = pd.date_range("2026-09-01", "2026-09-07 23:30", freq="30min")
    prof = nd.profile(day, T("2026-08-31"))
    surge = cal.is_surge(day.to_numpy(dtype="datetime64[ns]"))
    assert surge.any()
    act = world["clearing"].set_index("ts")["net_demand_mw"].reindex(day).to_numpy()
    assert np.abs(prof - act)[surge].mean() < 120
    assert np.abs(prof - act).mean() < 120
    with pytest.raises(ValueError):
        nd.profile(day, CUT - pd.Timedelta(days=1))


# ---- forecaster ----------------------------------------------------------------------------
def test_forecaster_accuracy(world):
    info = sm.info_from(world)
    wf = walk_forward(info, None, "2026-08-20", "2026-09-05 23:30")
    ev = evaluate(wf).set_index(["kind", "product"])
    mean_e = wf["actual_E"].mean()
    for kind in ("lead2", "day_ahead"):
        assert ev.loc[(kind, "E"), "MAE"] < 0.15 * mean_e
        # synthetic scarcity is knife-edge (a 50 MW demand error flips it): loose bounds
        assert ev.loc[(kind, "E"), "spike_recall"] >= 0.5
        assert ev.loc[(kind, "E"), "spike_precision"] >= 0.5
    info.assert_no_leak()


def test_lead2_single_equals_batch(fitted):
    Ts = pd.date_range("2026-09-01 10:00", periods=6, freq="30min")
    many = fitted.lead2_many(Ts)
    for t in Ts:
        one = fitted.lead2(t)
        assert [one[k] for k in KEYS] == [many.at[t, k] for k in KEYS]


def test_forecaster_rejects_early_decisions(fitted):
    with pytest.raises(ValueError):
        fitted.day_ahead("2026-08-25", decision_time=CUT - pd.Timedelta(hours=1))
    with pytest.raises(ValueError):
        fitted.lead2(CUT)  # cutoff before the fit cut
    with pytest.raises(ValueError):  # decision after a target's cutoff
        fitted.day_ahead("2026-09-01", decision_time=T("2026-09-01 00:00"))


def test_scenarios(fitted):
    s = fitted.scenarios(day="2026-08-31", kind="day_ahead")
    assert s.values.shape[1:] == (48, 4)
    assert s.values.shape[0] >= 9
    sm_ = s.summary
    for k in KEYS:
        assert (sm_[f"{k}_q10"] <= sm_[f"{k}_q50"] + 1e-9).all()
        assert (sm_[f"{k}_q50"] <= sm_[f"{k}_q90"] + 1e-9).all()
        assert sm_[f"{k}_p_spike"].between(0, 1).all()
    s2 = fitted.scenarios(day="2026-08-31", kind="day_ahead")
    assert np.array_equal(s.values, s2.values)
    l2 = fitted.scenarios(pd.date_range("2026-08-31 12:00", periods=4, freq="30min"), kind="lead2")
    assert l2.values.shape[1:] == (4, 4)
    assert fitted.residuals["date"].max() + pd.Timedelta(days=1) <= CUT


def test_audit_trail(world):
    info = sm.info_from(world)
    f = Forecaster(info).fit(CUT)
    info.clear_audit()
    f.lead2(T("2026-09-02 12:00"))
    f.day_ahead("2026-09-03")
    df = info.audit_frame()
    assert {"forecast.lead2", "forecast.day_ahead", "stack_index_known_at", "predispatch_for"} <= set(df.accessor)
    assert (df["max_time_used"].dropna() <= df.loc[df["max_time_used"].notna(), "decision_time"]).all()
    info.assert_no_leak()
    with pytest.raises(LeakError):
        info.record("test", T("2026-09-01"), T("2026-09-02"), 1)


# ---- no-leak property --------------------------------------------------------------------
def _perturbed_info(world, t):
    return InfoSet(
        Config(), perturb_clearing(world["clearing"], t), perturb_predispatch(world["predispatch"], t),
        None, perturb_stacks(world["stacks"], t),
    )


def test_no_leak_property(world):
    rng = np.random.default_rng(2026)
    cols = list(KEYS) + ["nd", "outage", "sP", "sC", "sR"]
    for _ in range(4):
        Tt = T("2026-08-28") + pd.Timedelta(minutes=30 * int(rng.integers(0, 48 * 10)))
        d = sm.info_from(world).cutoff(Tt)
        cut = d.normalize() - pd.Timedelta(days=2)
        a = Forecaster(sm.info_from(world)).fit(cut, residual_start="2026-08-15")
        b = Forecaster(_perturbed_info(world, d)).fit(cut, residual_start="2026-08-15")
        ra, rb = a.lead2(Tt), b.lead2(Tt)
        assert all(ra[c] == rb[c] for c in cols)
        sa = a.scenarios([Tt], kind="lead2")
        sb = b.scenarios([Tt], kind="lead2")
        assert np.array_equal(sa.values, sb.values)
        # day-ahead for the next day, decided at d
        day = (Tt + pd.Timedelta(days=1)).normalize()
        da, db = a.day_ahead(day, d), b.day_ahead(day, d)
        assert frames_equal(da, db, cols)
        ea = a.scenarios(day=day, kind="day_ahead", decision_time=d)
        eb = b.scenarios(day=day, kind="day_ahead", decision_time=d)
        assert np.array_equal(ea.values, eb.values)


def test_no_leak_test_is_sensitive(world):
    """Changing *legal* stack rows must change the forecast (the property test bites)."""
    Tt = T("2026-09-04 18:00")
    d = sm.info_from(world).cutoff(Tt)
    a = Forecaster(sm.info_from(world)).fit(d - pd.Timedelta(days=1)).lead2(Tt)
    st = perturb_stacks(world["stacks"], d, legal=True)
    info = InfoSet(Config(), world["clearing"], world["predispatch"], None, st)
    b = Forecaster(info).fit(d - pd.Timedelta(days=1)).lead2(Tt)
    assert a["E"] != b["E"]
