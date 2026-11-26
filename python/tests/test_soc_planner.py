"""SOC bookkeeping (all efficiency conventions, interval tracker) and the planner DP."""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest

from bessbid.config import Config
from bessbid.planner import (
    NEG,
    action_values,
    dp_backward,
    dp_forward,
    dp_plan,
    make_action_grid,
    terminal_values,
)
from bessbid.soc import (
    SocIntervalTracker,
    binding_soc,
    check_soc,
    eta_c_range,
    net_stored,
    soc_paths,
    y_bounds,
    y_terminal_min,
)

CFG = Config()


# ---- soc ----------------------------------------------------------------------------------
def test_soc_paths_match_formula():
    dis = np.array([0, 0, 40, 0, 10.0])
    chg = np.array([30, 20, 0, 0, 0.0])
    paths = soc_paths(dis, chg, CFG)
    assert len(paths) == 3
    for (ec, ed), s in paths.items():
        exp = 50 + np.cumsum(0.5 * (ec * chg - dis / ed))
        np.testing.assert_allclose(s, exp)
        # SOC = s0 + eta_c * N for every convention
        np.testing.assert_allclose(s, 50 + ec * net_stored(dis, chg, CFG))


def test_binding_convention_is_the_extreme():
    rng = np.random.default_rng(0)
    for _ in range(50):
        x = rng.normal(0, 40, 48)
        dis, chg = np.clip(x, 0, None), np.clip(-x, 0, None)
        Y = binding_soc(dis, chg, CFG)
        paths = np.stack(list(soc_paths(dis, chg, CFG).values()))
        hi = np.maximum(paths.max(0), 50)
        lo = np.minimum(paths.min(0), 50)
        # Y is the highest SOC when above s0 and the lowest when below
        np.testing.assert_allclose(np.where(Y >= 50, Y, hi), hi, atol=1e-9)
        np.testing.assert_allclose(np.where(Y <= 50, Y, lo), lo, atol=1e-9)


def test_y_bounds_and_terminal():
    assert y_bounds(CFG) == (7.0, 93.0)
    assert y_bounds(CFG, 0.0) == (5.0, 95.0)
    assert eta_c_range(CFG) == (pytest.approx(0.85), 1.0)
    assert y_terminal_min(CFG) == 50.0
    # a target above s0 needs a larger Y (the smallest SOC uses eta_c_min)
    assert y_terminal_min(CFG.replace(terminal_soc_frac=0.6)) == pytest.approx(50 + 10 / 0.85)
    assert y_terminal_min(CFG.replace(terminal_soc_frac=0.3)) == 30.0


def test_check_soc_flags_breach_and_terminal():
    chg = np.full(4, 45.0)  # +90 MWh stored at eta_c = 1 -> 140 MWh
    c = check_soc(np.zeros(4), chg, CFG)
    assert not c.ok and c.breach_periods >= 1
    c = check_soc(np.zeros(2), np.array([20.0, 0]), CFG, terminal_mwh=50.0)
    assert c.ok
    c = check_soc(np.array([0, 20.0]), np.zeros(2), CFG, terminal_mwh=50.0)
    assert not c.ok  # ends below 50 %


def test_interval_tracker_worst_case():
    t = SocIntervalTracker(CFG, margin=0.0)
    # 40 MW charge that may or may not clear, then a discharge band that may clear
    t.step(c_min=0, c_max=40, label="maybe-charge")
    lo, hi = t.soc_range()
    assert lo == pytest.approx(50.0) and hi == pytest.approx(70.0)
    md = t.max_discharge()
    # the lowest reachable SOC (charge did not clear) is 50: room 45 MWh at eta_d = 0.85
    assert md == pytest.approx(45 * 0.85 / 0.5)
    assert t.feasible(d_max=md)
    assert not t.feasible(d_max=md + 1)
    mc = t.max_charge()
    assert mc == pytest.approx(25 / 0.5)  # highest SOC 70 at eta_c = 1 -> room 25 MWh
    with pytest.raises(ValueError):
        t.step(c_max=mc + 5)
    t.step(d_min=10, d_max=10)
    lo2, hi2 = t.soc_range()
    assert lo2 == pytest.approx(50 - 5 / 0.85) and hi2 == pytest.approx(70 - 5 / 0.85)


# ---- planner -------------------------------------------------------------------------------
def test_action_grid_is_exact():
    g = make_action_grid(CFG, max_charge_mw=50, max_discharge_mw=100, charge_step_mw=2)
    assert g.mw[0] == 0 and g.steps[0] == 0
    for x, k in zip(g.mw, g.steps):
        assert abs(x * 10 - round(x * 10)) < 1e-6  # on the 0.1 MW grid
        dy = 0.5 * (-x if x < 0 else 0) - 0.5 * (x / 0.85 if x > 0 else 0)
        assert dy == pytest.approx(k * g.y_step)
    assert g.mw.min() == -50 and 98 < g.mw.max() <= 100
    assert g.y[0] == 7.0 and g.y[-1] == 93.0
    assert g.state_of(50.0) == 86


def test_action_values_interpolate():
    lv = (-50, 0, 50)
    E = np.array([[110.0, 100.0, 90.0]])
    R = np.array([[20.0, 20.0, 20.0]])
    F = action_values(E, R, lv, np.array([0.0, 25.0, -25.0]), CFG)
    assert F[0, 0] == pytest.approx(0.5 * 100 * 20)
    assert F[0, 1] == pytest.approx(0.5 * (25 * 95 + 75 * 20))
    assert F[0, 2] == pytest.approx(0.5 * (-25 * 105 + 75 * 20))


def _brute(F, g, s0, allowed, V_end):
    n = F.shape[0]
    best = -math.inf
    for seq in itertools.product(range(g.n_actions), repeat=n):
        s, v = s0, 0.0
        for t, a in enumerate(seq):
            if allowed is not None and not allowed[t, a]:
                break
            s += g.steps[a]
            if not 0 <= s < g.n_states:
                break
            v += F[t, a]
        else:
            if V_end[s] > NEG / 2:
                best = max(best, v + V_end[s])
    return best


def test_dp_matches_brute_force():
    cfg = CFG.replace(soc_margin_mwh=40.0)  # tiny grid: Y in [45, 55]
    g = make_action_grid(cfg, max_charge_mw=8, max_discharge_mw=8, y_step=2.0)
    rng = np.random.default_rng(3)
    for _ in range(5):
        F = rng.normal(0, 10, (4, g.n_actions))
        allowed = rng.random((4, g.n_actions)) > 0.2
        allowed[:, 0] = True
        V_end = terminal_values(g, 50.0)
        s0 = g.nearest_state(50.0)
        acts, states, val = dp_plan(F, g, s0, allowed, V_end)
        assert val == pytest.approx(_brute(F, g, s0, allowed, V_end))
        assert sum(F[t, a] for t, a in enumerate(acts)) == pytest.approx(val)
        assert g.y[states[-1]] >= 50.0


def test_dp_cycles_on_a_price_spread_and_respects_bans():
    g = make_action_grid(CFG, max_charge_mw=50, max_discharge_mw=50)
    n = 8
    E = np.array([100.0] * 4 + [300.0] * 4)[:, None] * np.ones((1, 3))
    R = np.full((n, 3), 10.0)
    F = action_values(E, R, (-50, 0, 50), g.mw, CFG)
    V_end = terminal_values(g, 50.0)
    acts, states, val = dp_plan(F, g, g.state_of(50.0), None, V_end)
    mw = g.mw[acts]
    assert (mw[:4] <= 0).all() and (mw[4:] >= 0).all() and mw[:4].sum() < 0
    assert g.y[states[-1]] >= 50.0 - 1e-9
    # charging banned in the cheap periods -> no arbitrage possible (ending >= 50 %)
    allowed = np.ones_like(F, bool)
    allowed[:, g.mw < 0] = False
    acts2, _, _ = dp_plan(F, g, g.state_of(50.0), allowed, V_end)
    assert (g.mw[acts2] == 0).all()
    # the policy is defined for every state: from a full battery it discharges
    V, pol = dp_backward(F, g, None, V_end)
    a_full, _ = dp_forward(pol, g, g.state_of(93.0))
    assert g.mw[a_full].max() > 0


def test_dp_infeasible_terminal_raises():
    g = make_action_grid(CFG, max_charge_mw=2, max_discharge_mw=2)
    F = np.zeros((2, g.n_actions))
    with pytest.raises(RuntimeError):
        dp_plan(F, g, g.state_of(7.0), None, terminal_values(g, 50.0))
