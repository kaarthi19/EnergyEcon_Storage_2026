"""Contingency options for EMA's answers (config/scenarios/README.md): the 'physical' capacity
reading, price-responsive bands with the deviation tracker, reserve on top of charging, the
Regulation sustainability rule, state-dependent values in the SOC programme, single-miss
exposure, the evaluator's SOC policy, and the scenario config files. Every option defaults to
the submitted behaviour."""

from __future__ import annotations

import dataclasses
import glob
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import synth_market as sm
from perturb import perturb_clearing, perturb_predispatch, perturb_stacks

from bessbid.backtest import MarketData, simulate
from bessbid.bids import BidError, energy_curve, make_period_bid, offered_capacity, regulation_only, to_table
from bessbid.config import Config, load_config, load_strategy_options
from bessbid.evaluate import evaluate, single_miss_exposure
from bessbid.infoset import InfoSet
from bessbid.periods import month_slots
from bessbid.planner import dp_backward, dp_backward_ramp, make_action_grid
from bessbid.soc import DeviationTracker, check_soc, net_stored, regulation_cap, soc_paths
from bessbid.strategy import check_complete
from bessbid.strategy_full import FullParams, baseline_regulation_mw, plan_month, uses_overlay
from bessbid.validate import errors, validate_table

MONTH = "2026-08"
CFG = Config()
BASE = dict(levels=(-50.0, 0.0, 50.0, 100.0), use_nodes=False)
SCEN = Path(__file__).resolve().parents[1] / "config" / "scenarios"
D = pd.Timestamp("2026-11-01").date()


# ---- defaults ----------------------------------------------------------------------------------


def test_new_options_default_to_the_submitted_behaviour():
    p = FullParams()
    assert not uses_overlay(p)
    assert (p.pr_bands, p.reserve_fill, p.regulation_soc_rule) == ("none", "none", "none")
    assert baseline_regulation_mw(CFG, p) is None
    assert CFG.capacity_rule == "strict"
    for bad in (dict(pr_bands="always"), dict(pr_periods="nights"), dict(pr_sides="up"), dict(pr_mw=-1.0),
                dict(reserve_fill="energy"), dict(regulation_soc_rule="maybe"), dict(regulation_sustain_h=0.0),
                dict(regulation_soc_rule="planned", refine="lead2")):
        with pytest.raises(ValueError):
            FullParams(**bad)


# ---- capacity reading 'physical' ---------------------------------------------------------------


def test_physical_capacity_rule():
    phys = Config(capacity_rule="physical")
    # 50 MW charge + 50 MW Regulation + 50 MW Contingency: the engine's rows hold
    pb = make_period_bid(D, 1, phys, energy=[(3000.0, -50.0)], regulation=[(0.01, 50.0)],
                         contingency=[(0.01, 50.0)])
    assert offered_capacity(pb, "physical") == 100.0
    assert offered_capacity(pb, "strict") == 150.0 and offered_capacity(pb, "max_side") == 150.0
    with pytest.raises(BidError):
        make_period_bid(D, 1, CFG, energy=[(3000.0, -50.0)], regulation=[(0.01, 50.0)], contingency=[(0.01, 50.0)])
    with pytest.raises(BidError):  # charge + Regulation > 100
        make_period_bid(D, 1, phys, energy=[(3000.0, -50.0)], regulation=[(0.01, 60.0)])
    with pytest.raises(BidError):  # discharge + all AS > 100
        make_period_bid(D, 1, phys, energy=[(0.01, 30.0)], regulation=[(0.01, 50.0)], contingency=[(0.01, 30.0)])
    tb = to_table([pb])
    assert "capacity" not in {v.code for v in validate_table(tb, phys, None)}
    assert "capacity" in {v.code for v in validate_table(tb, CFG, None)}
    assert "capacity" in {v.code for v in validate_table(tb, Config(capacity_rule="max_side"), None)}
    # both sides at once: 30 charge + 30 discharge + 70 Regulation is fine (max of the rows)
    pb2 = energy_curve(D, 2, phys, charge=[(100.0, 30.0)], discharge=[(500.0, 30.0)], regulation=[(0.01, 70.0)])
    assert offered_capacity(pb2, "physical") == 100.0


# ---- Regulation sustainability -----------------------------------------------------------------


def test_regulation_cap():
    # at 50 % SOC with no energy: the all-loss-on-discharge convention binds, 45 x 0.85 / 0.5
    assert regulation_cap(50.0, 0.0, CFG) == pytest.approx(76.5)
    # full battery (binding Y = 93): 2 MWh of headroom sustains 4 MW of down-regulation
    assert regulation_cap(93.0, 0.0, CFG) == pytest.approx(4.0)
    # never above the strict capacity rule, never negative
    assert regulation_cap(50.0, 50.0, CFG) <= 50.0
    assert regulation_cap(5.0, 40.0, CFG) == 0.0
    # a charge frees room for up-regulation near empty
    assert regulation_cap(7.0, -40.0, CFG) > regulation_cap(7.0, 0.0, CFG)
    # brute force: SOC of activation x +/- R for the period stays inside 5-95 under each convention
    rng = np.random.default_rng(1)
    for y, x in zip(rng.uniform(7, 93, 200), rng.uniform(-50, 50, 200)):
        R = float(regulation_cap(y, x, CFG))
        if R <= 0:  # the energy schedule alone already uses all the room
            continue
        n = (y - 50.0) / 1.0
        for ec, ed in CFG.efficiency_conventions:
            soc = 50.0 + ec * n
            for out in (x + R, x - R):
                d = -0.5 * out / ed if out > 0 else -0.5 * out * ec
                assert 5.0 - 1e-6 <= soc + d <= 95.0 + 1e-6
    assert regulation_cap(50.0, 0.0, CFG, hours=0.25) == pytest.approx(100.0)  # capped by the rating
    assert baseline_regulation_mw(CFG, FullParams(regulation_soc_rule="posthoc")) == pytest.approx(76.5)


# ---- deviation tracker -------------------------------------------------------------------------


def test_deviation_tracker_keeps_the_plan_executable():
    rng = np.random.default_rng(3)
    x = np.zeros(200)
    x[20:24], x[40:44], x[60:66], x[80:82] = -20.0, 30.0, -20.0, 17.0
    dis, chg = np.clip(x, 0, None), np.clip(-x, 0, None)
    assert check_soc(dis, chg, CFG, terminal_mwh=50.0).ok
    tr = DeviationTracker(net_stored(dis, chg, CFG), CFG, margin=0.0)
    extra_d = np.zeros(200)
    extra_c = np.zeros(200)
    for t in range(200):
        qd = min(rng.uniform(0, 20), tr.max_discharge(t))
        qc = min(rng.uniform(0, 20), tr.max_charge(t))
        tr.step(t, qd, qc)
        extra_d[t], extra_c[t] = qd, qc
    with pytest.raises(ValueError):
        tr.step(199, tr.max_discharge(199) + 1.0, 0.0)
    # worst cases: every extra discharge clears and no extra charge, and the reverse
    for d_, c_ in ((dis + extra_d, chg), (dis, chg + extra_c)):
        for path in soc_paths(d_, c_, CFG).values():
            assert path.min() >= 5.0 - 1e-6 and path.max() <= 95.0 + 1e-6
            assert path[-1] >= 50.0 - 1e-6  # the month-end target holds too
    assert extra_c.sum() > 0 and extra_d.sum() > 0


# ---- state-dependent values in the SOC programme -----------------------------------------------


@pytest.mark.parametrize("ramp", [None, 30.0])
def test_state_dependent_values_match_broadcast_values(ramp):
    rng = np.random.default_rng(5)
    g = make_action_grid(CFG, max_charge_mw=20.0, max_discharge_mw=20.0, y_step=2.0, charge_step_mw=4.0)
    F = rng.normal(0, 100, (12, g.n_actions))
    F3 = np.repeat(F[:, None, :], g.n_states, axis=1)
    if ramp is None:
        V2, p2 = dp_backward(F, g)
        V3, p3 = dp_backward(F3, g)
        np.testing.assert_array_equal(V2, V3)
        np.testing.assert_array_equal(p2, p3)
    else:
        V2, p2 = dp_backward_ramp(F, g, None, None, ramp)
        V3, p3 = dp_backward_ramp(F3, g, None, None, ramp)
        np.testing.assert_array_equal(V2, V3)
        np.testing.assert_array_equal(p2, p3)
    # a state-dependent bonus changes the policy where it should
    F3b = F3.copy()
    F3b[:, : g.n_states // 2, 0] += 1e6  # idle is very valuable in the lower half of the SOC range
    V4, p4 = dp_backward(F3b, g)
    assert (p4[0, : g.n_states // 2] == 0).all()


# ---- single missed band ------------------------------------------------------------------------


def test_single_miss_exposure():
    x = np.zeros(10)
    x[1] = -40.0  # 20 MWh charged (at eta_c = 1)
    x[5] = 80.0   # 40 MW-h discharged: SOC 70 -> 70 - 40/0.85 = 22.9
    out = single_miss_exposure(x, CFG, conventions=("discharge",))
    dis = out[out.side == "discharge"].iloc[0]
    chg = out[out.side == "charge"].iloc[0]
    assert dis.cases == 1 and chg.cases == 1
    assert dis.breach_cases == 0 and dis.worst_soc_max == pytest.approx(70.0)
    # missing the charge leaves 50 - 47.06 = 2.94 MWh: a breach of 2.06 MWh
    assert chg.breach_cases == 1 and chg.breach_max_mwh == pytest.approx(5.0 - (50.0 - 40.0 / 0.85))


# ---- the options on the synthetic market -------------------------------------------------------


@pytest.fixture(scope="module")
def world():
    return sm.build(days=70)


@pytest.fixture(scope="module")
def md(world):
    return MarketData.from_frames(world["stacks"], world["clearing"])


def _plan(world, cfg, **kw):
    info = sm.info_from(world, cfg)
    bids, frame, inf = plan_month(MONTH, info, cfg, FullParams(**BASE, **kw))
    info.assert_no_leak()
    check_complete(bids, MONTH)
    assert not errors(validate_table(to_table(bids), cfg, MONTH))
    assert all(offered_capacity(pb, cfg.capacity_rule) <= cfg.power_mw + 1e-9 for pb in bids)
    return bids, frame, inf


@pytest.fixture(scope="module")
def default_plan(world):
    return _plan(world, CFG)


def test_physical_reading_without_options_gives_the_default_bids(world, default_plan):
    bids, *_ = _plan(world, Config(capacity_rule="physical"))
    pd.testing.assert_frame_equal(to_table(bids), to_table(default_plan[0]))


@pytest.mark.parametrize("planned", [False, True], ids=["overlay", "planned"])
def test_reserve_fill_on_charging(world, md, default_plan, planned):
    cfg = Config(capacity_rule="physical")
    bids, frame, inf = _plan(world, cfg, reserve_fill="contingency", reserve_value=planned)
    if not planned:  # the overlay leaves the energy plan alone
        pd.testing.assert_series_equal(frame["energy_mw"], default_plan[1]["energy_mw"])
    else:  # charging is worth more, so the programme charges at least as much
        assert inf["charge_mwh"] >= default_plan[2]["charge_mwh"] - 1e-9
    for pb, x in zip(bids, frame["energy_mw"]):
        c = sum(b.mw for b in pb.contingency)
        r = sum(b.mw for b in pb.regulation)
        assert r == pytest.approx(100 - abs(x))
        assert c == pytest.approx(max(-x, 0.0))  # Contingency exactly where the battery charges
        assert not pb.primary
    assert inf["overlay"]["reserve_fill_mwh"] > 0
    tb = to_table(bids)
    for mode in ("maker", "taker"):
        s = simulate(tb, MONTH, md, mode=mode, convention="discharge", cfg=cfg).summary
        assert s["breach_periods"] == 0 and s["rev_C"] >= 0


@pytest.mark.parametrize("rule", ["posthoc", "planned"])
def test_regulation_sustainability(world, md, rule):
    bids, frame, inf = _plan(world, CFG, regulation_soc_rule=rule)
    x = frame["energy_mw"].to_numpy()
    y_start = np.concatenate([[50.0], frame["soc_binding"].to_numpy()[:-1]])
    cap = regulation_cap(y_start, x, CFG)
    reg = np.array([sum(b.mw for b in pb.regulation) for pb in bids])
    assert (reg <= cap + 1e-9).all() and (reg >= cap - 0.1 - 1e-9).all()
    assert inf["soc_check"]["ok"]
    s = simulate(to_table(bids), MONTH, md, mode="maker", convention="discharge", cfg=CFG).summary
    assert s["breach_periods"] == 0


def test_planned_regulation_rule_beats_posthoc_in_planned_value(world, md):
    """The programme that values the capped Regulation earns at least as much Regulation MW
    as capping the default plan afterwards."""
    _, f_post, i_post = _plan(world, CFG, regulation_soc_rule="posthoc")
    _, f_plan, i_plan = _plan(world, CFG, regulation_soc_rule="planned")
    assert i_plan["overlay"]["regulation_mwh"] > i_post["overlay"]["regulation_mwh"]


@pytest.mark.parametrize("mode", ["tracker", "local"])
def test_price_responsive_bands(world, md, default_plan, mode):
    bids, frame, inf = _plan(world, CFG, pr_bands=mode, pr_periods="all")
    pd.testing.assert_series_equal(frame["energy_mw"], default_plan[1]["energy_mw"])
    x = frame["energy_mw"].to_numpy()
    n_bands = 0
    for pb, xi in zip(bids, x):
        fixed = [b for b in pb.energy if b.price in (CFG.discharge_floor_price, CFG.charge_cap_price)]
        assert sum(b.mw for b in fixed) == pytest.approx(xi)  # the planned schedule is untouched
        n_bands += len(pb.energy) - len(fixed)
        # strict rule: extra bands displace Regulation
        assert offered_capacity(pb, "strict") <= 100 + 1e-9
    assert n_bands > 0 or mode == "tracker"
    tb = to_table(bids)
    if mode == "tracker":
        # worst case is safe by construction: every convention, both modes, no SOC policy
        for m in ("maker", "taker"):
            for cv in ("discharge", "sqrt", "charge"):
                assert simulate(tb, MONTH, md, mode=m, convention=cv, cfg=CFG).summary["breach_periods"] == 0
    else:
        for pol in ("clip", "reject"):
            s = simulate(tb, MONTH, md, mode="maker", convention="discharge", cfg=CFG, soc_policy=pol).summary
            assert s["breach_periods"] == 0


def test_price_responsive_bands_respect_the_cut(world):
    """The extra bands of period T use T's lead-2 h forecast, legal at T's gate."""
    p = FullParams(**BASE, pr_bands="local", pr_periods="all")
    T = pd.Timestamp("2026-08-14 18:00")
    t = T - pd.Timedelta(minutes=CFG.gate_closure_min)
    info = sm.info_from(world)
    bids, _, _ = plan_month(MONTH, info, CFG, p)
    info2 = InfoSet(CFG, perturb_clearing(world["clearing"], t), perturb_predispatch(world["predispatch"], t),
                    None, perturb_stacks(world["stacks"], t))
    bids2, _, _ = plan_month(MONTH, info2, CFG, p)
    info2.assert_no_leak()
    a = [pb for pb in bids if pb.start <= T]
    b = [pb for pb in bids2 if pb.start <= T]
    pd.testing.assert_frame_equal(to_table(a), to_table(b))


def test_evaluate_with_soc_policy_and_capped_baseline(world, md):
    res, _ = evaluate(None, CFG, months=[MONTH], params=FullParams(**BASE, regulation_soc_rule="posthoc"),
                      conventions=("discharge",), modes=("maker",), perfect_foresight=(), data=md,
                      info_factory=lambda c: sm.info_from(world, c), soc_policy="clip")
    full = res[(res.policy == "full") & (res.month == MONTH)].iloc[0]
    assert full["soc_policy"] == "clip" and full["clipped_periods"] == 0
    reg = res[(res.policy == "regulation") & (res.month == MONTH)].iloc[0]
    # the baseline offers the 76.5 MW that 50 % SOC sustains, not 100 MW
    base100 = simulate(to_table([regulation_only(s.date, s.period, CFG) for s in month_slots(MONTH)]),
                       MONTH, md, mode="maker", cfg=CFG).summary
    assert reg["revenue"] < base100["revenue"]


# ---- scenario config files ---------------------------------------------------------------------


def test_scenario_configs_load_and_only_change_what_they_say():
    files = sorted(glob.glob(str(SCEN / "*.toml")))
    assert len(files) >= 10
    for f in files:
        cfg = load_config(f)
        opts = load_strategy_options(f)
        assert set(opts) <= {"full", "regulation"}
        p = FullParams.from_mapping(opts.get("full", {}))
        grid = make_action_grid(cfg, max_charge_mw=p.charge_cap_mw,
                                max_discharge_mw=max(p.discharge_cap_mw, p.event_discharge_cap_mw),
                                y_step=p.y_step, charge_step_mw=p.charge_step_mw)
        s = grid.nearest_state(cfg.initial_soc_mwh)
        assert abs(grid.y[s] - cfg.initial_soc_mwh) < 1e-9, f  # the initial SOC is on the grid
        assert np.allclose(np.round(grid.mw / cfg.mw_resolution), grid.mw / cfg.mw_resolution), f
    # the confirmed-convention files plan under that convention only
    for name, conv in (("s4_eff_discharge", (1.0, 0.85)), ("s4_eff_charge", (0.85, 1.0))):
        assert load_config(SCEN / f"{name}.toml").efficiency_conventions == (conv,)
    assert load_config(SCEN / "s5_broad_predispatch.toml") == dataclasses.replace(Config(), strict_predispatch=False)
    assert load_config(SCEN / "s6_no_ramp.toml") == dataclasses.replace(Config(), bess_ramp_mw=0.0)
