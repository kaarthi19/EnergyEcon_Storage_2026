"""State-of-charge bookkeeping under every efficiency convention.

EMA has not said how the 85 % round-trip loss is split between charging and discharging,
so a plan must be feasible for every convention (eta_c, eta_d) with eta_c * eta_d = rte.
Write ``N_t`` for the grid-side "net stored" quantity

    N_t = h * sum_{k <= t} (charge_k - discharge_k / rte)          (MWh)

Then for any convention ``SOC_t = s0 + eta_c * N_t`` (because 1 / eta_d = eta_c / rte).
SOC is linear in eta_c, so over a set of conventions the extremes sit at the largest
eta_c: when N_t > 0 the highest SOC is ``s0 + eta_c_max * N_t``; when N_t < 0 the lowest
SOC is ``s0 + eta_c_max * N_t``; the other side stays between s0 and the extreme. Hence

    feasible for all conventions  <=>  lo <= Y_t <= hi,   Y_t := s0 + eta_c_max * N_t

i.e. the "all loss on discharge" convention (1, 0.85) is binding in every period. The
planners work with ``Y`` (one state variable) and the checks here confirm the result
under each convention separately.

:class:`SocIntervalTracker` keeps a worst-case [N_lo, N_hi] interval for offers whose
clearing is not known in advance (price-responsive bands): every band may clear or not,
and a band is only allowed if the whole interval stays feasible. :class:`DeviationTracker`
does the same around a *planned* path: extra bands may only move the SOC so far that every
later planned step, and the month-end target, stay feasible in the worst case.

:func:`regulation_cap` is the largest Regulation MW that the stored energy can sustain for a
given time in both directions (an RC386-style SOC check on Regulation; scenario option).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .config import Config

__all__ = [
    "eta_c_range",
    "net_stored",
    "soc_paths",
    "binding_soc",
    "SocCheck",
    "check_soc",
    "y_bounds",
    "y_terminal_min",
    "SocIntervalTracker",
    "DeviationTracker",
    "regulation_cap",
]

_TOL = 1e-9


def eta_c_range(cfg: Config) -> tuple[float, float]:
    """(smallest, largest) charge efficiency over ``cfg.efficiency_conventions``."""
    ec = [c for c, _ in cfg.efficiency_conventions]
    return min(ec), max(ec)


def net_stored(dis, chg, cfg: Config) -> np.ndarray:
    """N_t (MWh, end of each period) for discharge/charge MW arrays."""
    dis = np.asarray(dis, float)
    chg = np.asarray(chg, float)
    return np.cumsum(cfg.period_hours * (chg - dis / cfg.round_trip_eff))  # N_t of Eq. (15)


def soc_paths(dis, chg, cfg: Config, s0: float | None = None) -> dict[tuple[float, float], np.ndarray]:
    """End-of-period SOC (MWh) under every configured convention."""
    s0 = cfg.initial_soc_mwh if s0 is None else s0
    h = cfg.period_hours
    out = {}
    for ec, ed in cfg.efficiency_conventions:
        out[(ec, ed)] = s0 + np.cumsum(h * (ec * np.asarray(chg, float) - np.asarray(dis, float) / ed))  # Eq. (14)
    return out


def binding_soc(dis, chg, cfg: Config, s0: float | None = None) -> np.ndarray:
    """Y_t = s0 + eta_c_max * N_t: the SOC path that decides feasibility for all conventions."""
    s0 = cfg.initial_soc_mwh if s0 is None else s0
    return s0 + eta_c_range(cfg)[1] * net_stored(dis, chg, cfg)  # Eq. (15)


def y_bounds(cfg: Config, margin: float | None = None) -> tuple[float, float]:
    """Allowed range of Y with the planning margin."""
    m = cfg.soc_margin_mwh if margin is None else margin
    return cfg.soc_min_mwh + m, cfg.soc_max_mwh - m  # Eq. (16)


def y_terminal_min(cfg: Config, target_mwh: float | None = None, s0: float | None = None) -> float:
    """Smallest end-of-month Y that leaves SOC >= target under every convention."""
    s0 = cfg.initial_soc_mwh if s0 is None else s0
    tgt = cfg.terminal_soc_frac * cfg.energy_mwh if target_mwh is None else target_mwh
    lo, hi = eta_c_range(cfg)
    if tgt <= s0:  # Eq. (20)
        return tgt  # N < 0 is binding at eta_c_max, i.e. Y itself
    return s0 + (tgt - s0) * hi / lo  # N > 0: the smallest SOC uses eta_c_min


@dataclass
class SocCheck:
    ok: bool
    by_convention: dict
    breach_periods: int
    final_min: float

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "breach_periods": self.breach_periods,
            "final_min": self.final_min,
            "by_convention": {f"c{k[0]:.4f}_d{k[1]:.4f}": v for k, v in self.by_convention.items()},
        }


def check_soc(dis, chg, cfg: Config, margin: float = 0.0, terminal_mwh: float | None = None,
              s0: float | None = None) -> SocCheck:
    """Check SOC limits (shrunk by ``margin``) and, optionally, a terminal minimum under
    every convention."""
    lo, hi = cfg.soc_min_mwh + margin, cfg.soc_max_mwh - margin
    by, breaches, ok, fin = {}, 0, True, np.inf
    for conv, s in soc_paths(dis, chg, cfg, s0).items():
        b = (s < lo - 1e-7) | (s > hi + 1e-7)
        n = int(b.sum())
        breaches = max(breaches, n)
        final = float(s[-1]) if len(s) else (cfg.initial_soc_mwh if s0 is None else s0)
        fin = min(fin, final)
        by[conv] = {"min": float(s.min()) if len(s) else final, "max": float(s.max()) if len(s) else final,
                    "final": final, "breach_periods": n}
        if n:
            ok = False
    if terminal_mwh is not None and fin < terminal_mwh - 1e-7:
        ok = False
    return SocCheck(ok, by, breaches, float(fin))


@dataclass
class SocIntervalTracker:
    """Worst-case SOC interval for offers that may or may not clear.

    The state is the interval [N_lo, N_hi] of possible net-stored values. ``step`` adds a
    period in which the cleared discharge lies in [d_min, d_max] and the cleared charge in
    [c_min, c_max] (MW). ``max_discharge`` / ``max_charge`` give the largest extra
    discharge/charge MW that can be offered on top of committed amounts while every
    reachable SOC stays within the limits under every convention.
    """

    cfg: Config
    margin: float = 0.0
    n_lo: float = 0.0
    n_hi: float = 0.0
    s0: float | None = None
    history: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.s0 is None:
            self.s0 = self.cfg.initial_soc_mwh
        self._ec_lo, self._ec_hi = eta_c_range(self.cfg)

    def _soc_range(self, n_lo: float, n_hi: float) -> tuple[float, float]:
        e = (self._ec_lo, self._ec_hi)
        lo = self.s0 + min(c * n_lo for c in e)
        hi = self.s0 + max(c * n_hi for c in e)
        return lo, hi

    def soc_range(self) -> tuple[float, float]:
        """(lowest, highest) SOC reachable now, over all conventions."""
        return self._soc_range(self.n_lo, self.n_hi)

    def feasible(self, d_min=0.0, d_max=0.0, c_min=0.0, c_max=0.0) -> bool:
        h, r = self.cfg.period_hours, self.cfg.round_trip_eff
        lo_n = self.n_lo + h * (c_min - d_max / r)
        hi_n = self.n_hi + h * (c_max - d_min / r)
        lo, hi = self._soc_range(lo_n, hi_n)
        return lo >= self.cfg.soc_min_mwh + self.margin - _TOL and hi <= self.cfg.soc_max_mwh - self.margin + _TOL

    def step(self, d_min=0.0, d_max=0.0, c_min=0.0, c_max=0.0, label=None) -> tuple[float, float]:
        if not (0 <= d_min <= d_max + _TOL and 0 <= c_min <= c_max + _TOL):
            raise ValueError("need 0 <= min <= max for discharge and charge")
        if not self.feasible(d_min, d_max, c_min, c_max):
            raise ValueError(f"step {label}: worst-case SOC would leave the limits")
        h, r = self.cfg.period_hours, self.cfg.round_trip_eff
        self.n_lo += h * (c_min - d_max / r)
        self.n_hi += h * (c_max - d_min / r)
        self.history.append((label, self.n_lo, self.n_hi))
        return self.soc_range()

    def max_discharge(self, committed_dis: float = 0.0) -> float:
        """Largest extra discharge MW that may clear, given ``committed_dis`` that surely clears."""
        h, r = self.cfg.period_hours, self.cfg.round_trip_eff
        # lowest SOC after the period: s0 + ec_hi * (n_lo - h * (committed + x) / r) when negative
        floor_y = self.cfg.soc_min_mwh + self.margin
        # need s0 + ec_hi * n >= floor_y for the low end (n < 0 is the binding case)
        n_min = (floor_y - self.s0) / self._ec_hi if floor_y < self.s0 else (floor_y - self.s0) / self._ec_lo
        room = self.n_lo - h * committed_dis / r - n_min
        return max(0.0, room * r / h)

    def max_charge(self, committed_chg: float = 0.0) -> float:
        h = self.cfg.period_hours
        cap_y = self.cfg.soc_max_mwh - self.margin
        n_max = (cap_y - self.s0) / self._ec_hi if cap_y > self.s0 else (cap_y - self.s0) / self._ec_lo
        room = n_max - self.n_hi - h * committed_chg
        return max(0.0, room / h)


def regulation_cap(y_start, x, cfg: Config, hours: float | None = None, s0: float | None = None) -> np.ndarray:
    """Largest Regulation MW sustainable for ``hours`` (default one period) in each direction.

    ``y_start``: binding SOC Y (MWh, :func:`binding_soc`) at the start of the period; ``x``:
    the period's energy schedule (MW, + discharge); both broadcast. Up-regulation means the
    BESS output is ``x + R`` for ``hours``, down-regulation ``x - R``; the SOC must stay in
    [soc_min, soc_max] in both cases under every configured efficiency convention:

        R <= (SOC_c - soc_min) * eta_d / hours - x,   R <= (soc_max - SOC_c) / (hours * eta_c) + x

    The result is clipped to [0, power_mw - |x|] (the strict capacity rule).
    """
    h = cfg.period_hours if hours is None else float(hours)
    s0 = cfg.initial_soc_mwh if s0 is None else s0
    y = np.asarray(y_start, float)
    x = np.asarray(x, float)
    n = (y - s0) / eta_c_range(cfg)[1]  # Eq. (15) inverted: N from Y
    cap = np.full(np.broadcast(y, x).shape, np.inf)
    for ec, ed in cfg.efficiency_conventions:
        soc = s0 + ec * n
        up = (soc - cfg.soc_min_mwh) * ed / h - x
        down = (cfg.soc_max_mwh - soc) / (h * ec) + x
        cap = np.minimum(cap, np.minimum(up, down))
    return np.clip(cap, 0.0, cfg.power_mw - np.abs(x))


class DeviationTracker:
    """Worst-case deviation from a planned path caused by bands that may or may not clear.

    ``n_plan``: the planned grid-side net stored energy N_t (MWh, end of each period; see
    :func:`net_stored`). The deviation D (MWh of N) lies in [d_lo, d_hi]: an extra discharge
    band of q MW adds [-h q / rte, 0], an extra charge band [0, h q]. A band in period t is
    allowed only if, for every later t' >= t, SOC = s0 + eta_c (N_t' + D) stays inside
    [soc_min + margin, soc_max - margin] under every convention for the whole interval, and
    the month-end SOC stays >= ``terminal_mwh`` (default ``cfg.terminal_soc_frac``) under
    every convention. The planned path itself is then always executable."""

    def __init__(self, n_plan, cfg: Config, margin: float | None = None, terminal_mwh: float | None = None,
                 s0: float | None = None):
        self.cfg = cfg
        self.s0 = cfg.initial_soc_mwh if s0 is None else s0
        m = cfg.soc_margin_mwh if margin is None else margin
        ec_hi = eta_c_range(cfg)[1]
        n = np.asarray(n_plan, float)
        lo_n = (cfg.soc_min_mwh + m - self.s0) / ec_hi
        hi_n = (cfg.soc_max_mwh - m - self.s0) / ec_hi
        dmin = lo_n - n
        dmax = hi_n - n
        if len(n):
            y_t = y_terminal_min(cfg, terminal_mwh, self.s0)
            dmin[-1] = max(dmin[-1], (y_t - self.s0) / ec_hi - n[-1])
        # suffix bounds: a deviation at t persists to the end of the month
        self.lb = np.maximum.accumulate(dmin[::-1])[::-1] if len(n) else dmin
        self.ub = np.minimum.accumulate(dmax[::-1])[::-1] if len(n) else dmax
        self.d_lo = 0.0
        self.d_hi = 0.0

    def max_discharge(self, t: int) -> float:
        """Largest extra discharge MW in period ``t`` that keeps every later step feasible."""
        h, r = self.cfg.period_hours, self.cfg.round_trip_eff
        return max(0.0, (self.d_lo - self.lb[t]) * r / h)

    def max_charge(self, t: int) -> float:
        h = self.cfg.period_hours
        return max(0.0, (self.ub[t] - self.d_hi) / h)

    def step(self, t: int, q_dis: float = 0.0, q_chg: float = 0.0) -> tuple[float, float]:
        h, r = self.cfg.period_hours, self.cfg.round_trip_eff
        if q_dis > self.max_discharge(t) + 1e-9 or q_chg > self.max_charge(t) + 1e-9:
            raise ValueError(f"period {t}: extra bands would make a planned step infeasible")
        self.d_lo -= h * q_dis / r
        self.d_hi += h * q_chg
        return self.d_lo, self.d_hi
