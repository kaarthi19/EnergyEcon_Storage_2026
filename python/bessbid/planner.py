"""Energy-overlay planner: values of always-clearing energy actions and an SOC dynamic programme.

Two pieces:

:class:`ValueModel`
    For a period T, a decision time and forecast inputs (net demand, expected outage), it
    clears the *forecast* stack with our own offer inside the clearing LP for a ladder of
    signed energy levels x (MW, + = discharge) and Regulation ``power - |x|`` at the AS floor
    price, and records the energy and regulation prices. So the value of an action already
    contains our price impact (price-maker view):

        F_T(x) = h * (x * E_T(x) + (power - |x|) * R_T(x))

    Between ladder levels E and R are interpolated linearly.

:func:`dp_plan`
    A dynamic programme over the binding SOC variable ``Y`` (see :mod:`bessbid.soc`; Y in
    [lo, hi] is exactly "feasible under every efficiency convention"). Actions are SOC steps
    on a 0.5 MWh grid whose MW are exact multiples of the MW resolution (for the default
    BESS: charge in 2 MW steps, discharge in 1.7 MW steps), so a planned path is reproduced
    exactly by the bid quantities. Banned actions are masked per period; a terminal value
    (or terminal minimum Y) closes the horizon.

The planner only ever sees forecast values that the caller computed at a legal decision
time; it has no data access of its own.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import Config
from .engine import BessOffer, ClearingEngine, PeriodStack
from .soc import eta_c_range, y_bounds

__all__ = [
    "LEVELS",
    "ActionGrid",
    "make_action_grid",
    "ValueModel",
    "action_values",
    "dp_backward",
    "dp_forward",
    "dp_plan",
    "NEG",
    "terminal_values",
]

#: Default signed energy ladder (MW) at which the forecast market is cleared with our offer.
LEVELS = (-100.0, -75.0, -50.0, -35.0, -20.0, -10.0, 0.0, 10.0, 20.0, 35.0, 50.0, 75.0, 100.0)
NEG = -1e15


# --------------------------------------------------------------------------------------
# Action grid
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionGrid:
    """Energy actions as exact SOC steps.

    ``mw``: signed MW (+ discharge, - charge), index 0 is idle. ``steps``: change of Y in grid
    units. ``y``: the Y grid (MWh). ``y_step``: grid spacing."""

    mw: np.ndarray
    steps: np.ndarray
    y: np.ndarray
    y_step: float

    @property
    def n_actions(self) -> int:
        return len(self.mw)

    @property
    def n_states(self) -> int:
        return len(self.y)

    def state_of(self, y: float) -> int:
        """Grid index of Y (must be on the grid to 1e-6)."""
        i = int(round((y - self.y[0]) / self.y_step))
        if not 0 <= i < len(self.y) or abs(self.y[i] - y) > 1e-6:
            raise ValueError(f"Y={y} is not on the planning grid [{self.y[0]}, {self.y[-1]}] / {self.y_step}")
        return i

    def nearest_state(self, y: float, side: str = "inside") -> int:
        i = (y - self.y[0]) / self.y_step
        j = int(np.floor(i + 1e-9)) if side == "down" else int(np.ceil(i - 1e-9)) if side == "up" else int(round(i))
        return int(np.clip(j, 0, len(self.y) - 1))

    def dy(self, a) -> np.ndarray:
        return np.asarray(self.steps)[a] * self.y_step


def _quantum(unit_mw: float, res: float, kmax: int = 200) -> int:
    """Smallest k with k * unit_mw on the MW resolution grid (1 if none up to kmax)."""
    for k in range(1, kmax + 1):
        v = k * unit_mw / res
        if abs(v - round(v)) < 1e-7:
            return k
    return 1


def make_action_grid(cfg: Config, max_charge_mw: float | None = None, max_discharge_mw: float | None = None,
                     y_step: float = 0.5, charge_step_mw: float | None = None,
                     margin: float | None = None) -> ActionGrid:
    """Grid of exact actions. Charge MW per Y step is ``y_step / (h * eta_c_max)``, discharge
    MW per Y step ``y_step * rte / (h * eta_c_max)``; steps are grouped so that the MW land on
    ``cfg.mw_resolution`` (and on ``charge_step_mw`` for charging, if given)."""
    h, rte, res = cfg.period_hours, cfg.round_trip_eff, cfg.mw_resolution
    ec = eta_c_range(cfg)[1]
    max_c = cfg.power_mw if max_charge_mw is None else max_charge_mw
    max_d = cfg.power_mw if max_discharge_mw is None else max_discharge_mw
    c_unit = y_step / (h * ec)  # MW per Y step when charging
    d_unit = y_step * rte / (h * ec)  # MW per Y step when discharging
    kc = _quantum(c_unit, res)
    if charge_step_mw:
        kc = max(kc, int(round(charge_step_mw / c_unit)))
    kd = _quantum(d_unit, res)
    mw, steps = [0.0], [0]
    k = kc
    while k * c_unit <= max_c + 1e-9:
        mw.append(-round(k * c_unit, 6))
        steps.append(k)
        k += kc
    k = kd
    while k * d_unit <= max_d + 1e-9:
        mw.append(round(k * d_unit, 6))
        steps.append(-k)
        k += kd
    lo, hi = y_bounds(cfg, margin)
    n = int(np.floor((hi - lo) / y_step + 1e-9))
    y = lo + y_step * np.arange(n + 1)
    return ActionGrid(np.array(mw), np.array(steps, dtype=int), y, y_step)


# --------------------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------------------


def action_values(E: np.ndarray, R: np.ndarray, levels, mw: np.ndarray, cfg: Config,
                  reg_mw: np.ndarray | None = None) -> np.ndarray:
    """F (n_periods, n_actions) from ladder prices ``E``, ``R`` (n_periods, n_levels).

    Regulation offered with an action of x MW is ``power - |x|`` (strict capacity rule) unless
    ``reg_mw`` is given."""
    lv = np.asarray(levels, float)
    E = np.atleast_2d(np.asarray(E, float))
    R = np.atleast_2d(np.asarray(R, float))
    x = np.asarray(mw, float)
    Ex = np.stack([np.interp(x, lv, e) for e in E])
    Rx = np.stack([np.interp(x, lv, r) for r in R])
    q = cfg.power_mw - np.abs(x) if reg_mw is None else np.asarray(reg_mw, float)
    return cfg.period_hours * (x[None, :] * Ex + q[None, :] * Rx)


class ValueModel:
    """Forecast market prices as a function of our own energy action.

    ``forecaster``: a fitted :class:`bessbid.forecast.Forecaster`. Every call states its
    decision time; the forecast stacks are built with that decision time (the stack model
    enforces the publication cut) and an audit entry is recorded."""

    def __init__(self, forecaster, cfg: Config | None = None, levels=LEVELS, reg_price: float | None = None):
        self.fc = forecaster
        self.cfg = cfg or forecaster.cfg
        self.levels = np.asarray(levels, float)
        if 0.0 not in set(self.levels.tolist()):
            raise ValueError("levels must contain 0")
        self.reg_price = self.cfg.as_floor_price if reg_price is None else reg_price
        self.engine = ClearingEngine(self.cfg, warm_start=False)
        self.n_solves = 0

    def _offer(self, x: float) -> BessOffer:
        cfg = self.cfg
        if x > 0:
            e = ((cfg.discharge_floor_price, float(x)),)
        elif x < 0:
            e = ((cfg.charge_cap_price, float(x)),)
        else:
            e = ()
        q = cfg.power_mw - abs(x)
        r = ((self.reg_price, q),) if q > 0 else ()
        return BessOffer(energy=e, regulation=r)

    def ladder(self, Ts, decision_times, nd, outage, kind: str = "plan", nodes=None) -> dict:
        """Clear every T in ``Ts`` at each ladder level. ``decision_times``: one timestamp or
        one per T. ``nodes``: net-demand forecast-error nodes (forecast - actual, MW); the
        prices are then the equally weighted mean over ``nd - node``, which turns the step
        function of a single merit order into an expected price curve. Returns dict of
        (n, n_levels) arrays E, P, C, R, short (mean reserve shortfall MW) plus the margin
        ``nominal capacity - (nd + outage) - requirements``."""
        nodes = np.zeros(1) if nodes is None else np.asarray(nodes, float)
        Ts = pd.DatetimeIndex(Ts)
        n, L = len(Ts), len(self.levels)
        decs = [pd.Timestamp(decision_times)] * n if not isinstance(decision_times, (list, tuple, np.ndarray, pd.Index)) \
            else [pd.Timestamp(d) for d in decision_times]
        nd = np.asarray(nd, float)
        outage = np.asarray(outage, float)
        out = {k: np.zeros((n, L)) for k in ("E", "P", "C", "R", "short")}
        req_tot = np.zeros(n)
        nominal = float(np.sum(self.fc.stacks.nom))
        offers = [self._offer(x) for x in self.levels]
        info = self.fc.info
        for i, (T, d) in enumerate(zip(Ts, decs)):
            if T - info.gate < d:
                raise ValueError(f"decision time {d} is after the cutoff of {T}")
            sf = self.fc.stacks.forecast_stack(T, d, float(outage[i]))
            st = PeriodStack(*[np.asarray(a, dtype=float) for a in sf.lp_args()])
            req = self.fc.calendar.requirements(T)
            req_tot[i] = float(sum(req))
            for j, off in enumerate(offers):
                acc = np.zeros(5)
                for e in nodes:
                    r = self.engine.clear(st, float(nd[i] - e + outage[i]), req, None, off)
                    acc += (r.E, r.P, r.C, r.R, r.sP + r.sC + r.sR)
                acc /= len(nodes)
                out["E"][i, j], out["P"][i, j], out["C"][i, j], out["R"][i, j], out["short"][i, j] = acc
            self.n_solves += L * len(nodes)
            info.record(f"planner.{kind}", d, sf.newest_avail, 1, T)
        out["margin"] = nominal - (nd + outage) - req_tot
        out["ts"] = Ts
        return out

    def values(self, lad: dict, grid: ActionGrid) -> np.ndarray:
        return action_values(lad["E"], lad["R"], self.levels, grid.mw, self.cfg)


# --------------------------------------------------------------------------------------
# Dynamic programme
# --------------------------------------------------------------------------------------


def _transitions(grid: ActionGrid):
    S = np.arange(grid.n_states)[:, None] + grid.steps[None, :]
    valid = (S >= 0) & (S < grid.n_states)
    return np.clip(S, 0, grid.n_states - 1), valid


def dp_backward(F: np.ndarray, grid: ActionGrid, allowed: np.ndarray | None = None,
                V_end: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Value-to-go ``V`` (n+1, n_states) and policy (n, n_states) for values F (n, n_actions).

    ``allowed``: (n, n_actions) bool. ``V_end``: terminal value per state (default 0; use
    ``NEG`` entries to forbid end states)."""
    n = F.shape[0]
    nxt, valid = _transitions(grid)
    V = np.empty((n + 1, grid.n_states))
    V[n] = 0.0 if V_end is None else V_end
    pol = np.zeros((n, grid.n_states), dtype=np.int32)
    for t in range(n - 1, -1, -1):
        f = F[t]
        if allowed is not None:
            f = np.where(allowed[t], f, NEG)
        cand = np.where(valid, f[None, :] + V[t + 1][nxt], NEG)
        a = cand.argmax(1)  # first maximum: idle (index 0) wins ties, then smaller actions
        pol[t] = a
        V[t] = cand[np.arange(grid.n_states), a]
    return V, pol


def dp_forward(pol: np.ndarray, grid: ActionGrid, s0: int, n: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Follow a policy from state index ``s0``; returns (action indices, state index after each)."""
    n = pol.shape[0] if n is None else n
    acts = np.zeros(n, dtype=int)
    states = np.zeros(n, dtype=int)
    s = int(s0)
    for t in range(n):
        a = int(pol[t, s])
        acts[t] = a
        s = s + int(grid.steps[a])
        if not 0 <= s < grid.n_states:
            raise RuntimeError("policy left the SOC grid")
        states[t] = s
    return acts, states


def dp_plan(F: np.ndarray, grid: ActionGrid, s0: int, allowed: np.ndarray | None = None,
            V_end: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray, float]:
    """Optimal action indices and states from ``s0``; also the optimal value."""
    V, pol = dp_backward(F, grid, allowed, V_end)
    if V[0, s0] <= NEG / 2:
        raise RuntimeError("no feasible plan from this state (terminal condition unreachable)")
    acts, states = dp_forward(pol, grid, s0)
    return acts, states, float(V[0, s0])


def terminal_values(grid: ActionGrid, y_min: float | None = None) -> np.ndarray:
    """0 for states with Y >= y_min, NEG below."""
    v = np.zeros(grid.n_states)
    if y_min is not None:
        v[grid.y < y_min - 1e-9] = NEG
    return v
