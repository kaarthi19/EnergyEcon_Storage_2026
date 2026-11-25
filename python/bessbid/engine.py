"""Exact replica of EMA's per-period clearing engine, with optional BESS offers.

Formulation (reverse-engineered in Phase A; reproduces every training period Jul 1 - Oct 31
to within 0.002 SGD for all four prices). One LP per 30-minute period::

    min   sum_fb pE_fb e_fb + sum_f (pP_f p_f + pC_f c_f + pR_f r_f)
          + 750 sP + 1000 sC + 1200 sR + VoLL u
          + sum_j oD_j d_j - sum_j oC_j g_j + sum_kj oK_kj b_kj           (BESS bands)
    s.t.  sum e + u + sum d - sum g                = net_demand   -> energy price (dual)
          sum p + sP + sum b_P                     >= req_P       -> primary price
          sum c + sC + sum b_C                     >= req_C       -> contingency price
          sum r + sR + sum b_R                     >= req_R       -> regulation price
          sum_b e_fb + p_f + c_f + r_f             <= available_mw_f      (joint headroom)
          sum_b e_fb                               <= e_f(t-1) + 0.42 * available_mw_f(t)
                                                                        (ramp-up, if a state is given)
          0 <= e_fb <= offered_fb ; 0 <= p_f <= P offered_f (likewise c, r)
          shortfalls sP, sC, sR, u >= 0 and uncapped
          BESS:  sum d + sum b_P + sum b_C + sum b_R <= bess_pmax          (discharge side)
                 sum g + sum b_R                     <= bess_pmax          (charge side)
                 0 <= d_j, g_j, b_kj <= band MW

The ramp limit is applied, as in Phase A, by clipping each unit's band capacities in merit
order (bands are ascending in price, so this is the same LP as an explicit ramp row).

BESS offers follow RC383 s3.1.2: a discharge band (MW > 0) is supply at its offer price and
clears when the offer is below the clearing price; a charge band (MW < 0) is a demand bid and
clears when the offer is above the clearing price; reserve bands are supply at their price. At
equality the LP may clear a band partially (it is the marginal band). The two BESS joint rows
are the *physical* limits a dispatch engine would apply. Our bid generator enforces the stricter
*offered-MW* rule separately (``Config.capacity_rule = 'strict'``: sum |energy MW| + all AS MW
<= 100 per period, see :func:`bessbid.bids.check_capacity`), so for our own bids the BESS rows
never bind.

:func:`price_taker_clear` is the counterpart for fixed prices: each band that is in the money
is awarded, subject to the same BESS joint rows.

The solver is HiGHS through ``highspy`` directly. One model is built per (facilities, bands)
shape and re-used: each call only changes bounds, costs and row bounds, and the simplex is
warm-started from the previous basis. A call takes about 1-2 ms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from .config import Config

__all__ = [
    "PeriodStack",
    "StackArrays",
    "BessOffer",
    "ClearResult",
    "TakerResult",
    "ClearingEngine",
    "EngineError",
    "clear",
    "clear_period",
    "price_taker_clear",
    "replicate",
    "requirements",
    "MAX_E_BANDS",
    "MAX_AS_BANDS",
]

#: BESS band slots in the LP (template limits: 10 energy bands, 5 per AS product).
MAX_E_BANDS = 10
MAX_AS_BANDS = 5
RES = ("P", "C", "R")
STACK_PRODUCTS = {"Energy": "E", "Primary": "P", "Contingency": "C", "Regulation": "R"}


class EngineError(RuntimeError):
    """The LP did not solve to optimality."""


# --------------------------------------------------------------------------------------
# Generator stacks
# --------------------------------------------------------------------------------------


@dataclass
class PeriodStack:
    """Generator offers for one period. Arrays are float, NaN-free (missing = 0 MW).

    ``e_q``/``e_p``: (F, B) energy band MW and prices (ascending within a unit);
    ``p_q``/``p_p``, ``c_q``/``c_p``, ``r_q``/``r_p``: (F,) reserve MW and prices;
    ``av``: (F,) available MW.
    """

    e_q: np.ndarray
    e_p: np.ndarray
    p_q: np.ndarray
    p_p: np.ndarray
    c_q: np.ndarray
    c_p: np.ndarray
    r_q: np.ndarray
    r_p: np.ndarray
    av: np.ndarray

    @property
    def shape(self) -> tuple[int, int]:
        return self.e_q.shape  # type: ignore[return-value]


@dataclass
class StackArrays:
    """Dense (T, F, ...) arrays of generator offers for many periods.

    Build with :meth:`from_frame` from the long stack frame of :func:`bessbid.io.load_stacks`
    (columns ts, facility_id, product, band, offered_mw, price, available_mw) or from any frame
    in that format (e.g. a forecast stack).
    """

    ts: np.ndarray  # (T,) datetime64[ns], sorted
    facilities: tuple[str, ...]
    e_q: np.ndarray  # (T, F, B)
    e_p: np.ndarray
    p_q: np.ndarray  # (T, F)
    p_p: np.ndarray
    c_q: np.ndarray
    c_p: np.ndarray
    r_q: np.ndarray
    r_p: np.ndarray
    av: np.ndarray

    def __len__(self) -> int:
        return len(self.ts)

    @classmethod
    def from_frame(cls, df: pd.DataFrame, facilities: Sequence[str] | None = None,
                   n_bands: int | None = None) -> "StackArrays":
        need = ["ts", "facility_id", "product", "band", "offered_mw", "price", "available_mw"]
        miss = [c for c in need if c not in df.columns]
        if miss:
            raise ValueError(f"stack frame lacks columns {miss}")
        ts_all = df["ts"].to_numpy(dtype="datetime64[ns]")
        ts = np.unique(ts_all)
        fac_col = df["facility_id"].astype(str).to_numpy()
        facs = tuple(sorted(set(fac_col))) if facilities is None else tuple(facilities)
        fmap = {f: i for i, f in enumerate(facs)}
        fi = np.array([fmap.get(f, -1) for f in fac_col]) if len(fac_col) else np.zeros(0, int)
        if (fi < 0).any():
            raise ValueError("stack frame has facilities outside the given list")
        ti = np.searchsorted(ts, ts_all)
        prod = df["product"].astype(str).to_numpy()
        band = df["band"].to_numpy().astype(int)
        q = df["offered_mw"].to_numpy(dtype=float)
        p = df["price"].to_numpy(dtype=float)
        a = df["available_mw"].to_numpy(dtype=float)
        T, F = len(ts), len(facs)
        isE = prod == "Energy"
        B = int(n_bands or (band[isE].max() if isE.any() else 5))
        e_q = np.zeros((T, F, B)); e_p = np.zeros((T, F, B))
        m = isE & (band >= 1) & (band <= B)
        e_q[ti[m], fi[m], band[m] - 1] = q[m]
        e_p[ti[m], fi[m], band[m] - 1] = p[m]
        av = np.zeros((T, F))
        # available_mw is the same on every product row of a unit; take the max to be safe
        np.maximum.at(av, (ti, fi), np.nan_to_num(a))
        out = {}
        for name, k in (("Primary", "p"), ("Contingency", "c"), ("Regulation", "r")):
            mq = np.zeros((T, F)); mp = np.zeros((T, F))
            mm = (prod == name) & (band == 1)
            mq[ti[mm], fi[mm]] = q[mm]
            mp[ti[mm], fi[mm]] = p[mm]
            out[k + "_q"] = mq; out[k + "_p"] = mp
        arrs = dict(e_q=e_q, e_p=e_p, av=av, **out)
        for k, v in arrs.items():
            arrs[k] = np.nan_to_num(v)
        return cls(ts=ts, facilities=facs, **arrs)

    def index_of(self, ts) -> np.ndarray | int:
        """Positions of timestamps (raises KeyError if any is missing)."""
        t = np.atleast_1d(np.asarray(pd.to_datetime(ts), dtype="datetime64[ns]"))
        i = np.searchsorted(self.ts, t)
        ok = (i < len(self.ts)) & (self.ts[np.minimum(i, len(self.ts) - 1)] == t)
        if not ok.all():
            raise KeyError(f"no stack for {t[~ok][:3]}")
        return int(i[0]) if np.ndim(ts) == 0 and not isinstance(ts, (list, pd.Index, np.ndarray)) else i

    def period(self, i: int) -> PeriodStack:
        return PeriodStack(self.e_q[i], self.e_p[i], self.p_q[i], self.p_p[i], self.c_q[i],
                           self.c_p[i], self.r_q[i], self.r_p[i], self.av[i])


def requirements(clearing_row) -> dict[str, float]:
    """{P, C, R} requirement MW from a clearing-frame row (Series or namedtuple)."""
    g = clearing_row.__getitem__ if hasattr(clearing_row, "__getitem__") and not hasattr(clearing_row, "_fields") \
        else lambda k: getattr(clearing_row, k)
    return dict(P=float(g("primary_requirement_mw")), C=float(g("contingency_requirement_mw")),
                R=float(g("regulation_requirement_mw")))


# --------------------------------------------------------------------------------------
# BESS offers
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BessOffer:
    """BESS offers for one period, as (price, MW) bands. Energy MW is signed (charge < 0)."""

    energy: tuple[tuple[float, float], ...] = ()
    primary: tuple[tuple[float, float], ...] = ()
    contingency: tuple[tuple[float, float], ...] = ()
    regulation: tuple[tuple[float, float], ...] = ()

    @classmethod
    def from_period_bid(cls, pb) -> "BessOffer":
        f = lambda bands: tuple((float(b.price), float(b.mw)) for b in bands)  # noqa: E731
        return cls(f(pb.energy), f(pb.primary), f(pb.contingency), f(pb.regulation))

    @classmethod
    def from_arrays(cls, price: np.ndarray, qty: np.ndarray) -> "BessOffer":
        """From (4, n) price/quantity arrays in product order E, P, C, R (NaN = blank)."""
        out = []
        for k in range(4):
            m = ~(np.isnan(price[k]) | np.isnan(qty[k])) & (qty[k] != 0)
            out.append(tuple(zip(price[k][m].tolist(), qty[k][m].tolist())))
        return cls(*out)

    def reserve(self, k: str) -> tuple[tuple[float, float], ...]:
        return {"P": self.primary, "C": self.contingency, "R": self.regulation}[k]

    @property
    def discharge(self) -> tuple[tuple[float, float], ...]:
        return tuple((p, q) for p, q in self.energy if q > 0)

    @property
    def charge(self) -> tuple[tuple[float, float], ...]:
        """Charge bands as (price, +MW)."""
        return tuple((p, -q) for p, q in self.energy if q < 0)


# --------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------


@dataclass
class ClearResult:
    E: float
    P: float
    C: float
    R: float
    sP: float
    sC: float
    sR: float
    unserved: float
    cost: float
    gen_e: np.ndarray  # (F,) energy dispatch per unit
    gen_p: np.ndarray
    gen_c: np.ndarray
    gen_r: np.ndarray
    #: BESS awards per offered band, same order as the offer's bands (MW, >= 0)
    dis_bands: np.ndarray = field(default_factory=lambda: np.zeros(0))
    chg_bands: np.ndarray = field(default_factory=lambda: np.zeros(0))
    res_bands: dict = field(default_factory=dict)

    @property
    def prices(self) -> dict[str, float]:
        return {"E": self.E, "P": self.P, "C": self.C, "R": self.R}

    def price_tuple(self) -> tuple[float, float, float, float]:
        return (self.E, self.P, self.C, self.R)

    @property
    def gen(self) -> np.ndarray:
        """Alias of ``gen_e`` (the ramp state for the next period)."""
        return self.gen_e

    @property
    def dis(self) -> float:
        return float(self.dis_bands.sum())

    @property
    def chg(self) -> float:
        return float(self.chg_bands.sum())

    def bess_award(self, k: str) -> float:
        return float(np.sum(self.res_bands.get(k, 0.0)))

    @property
    def awards(self) -> dict[str, float]:
        """Cleared BESS MW: dis, chg (both >= 0), net (= dis - chg), P, C, R."""
        d, c = self.dis, self.chg
        return {"dis": d, "chg": c, "net": d - c, **{k: self.bess_award(k) for k in RES}}


# --------------------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------------------


def _ramp_clip(e_q: np.ndarray, av: np.ndarray, prev: np.ndarray | None, k: float) -> np.ndarray:
    if prev is None:
        return e_q
    cap = prev + k * av
    cum = np.cumsum(e_q, axis=1)
    return np.clip(cap[:, None] - (cum - e_q), 0.0, e_q)


def _new_highs():
    """A Highs instance, without the banner HiGHS prints to fd 1 the first time."""
    import os
    import sys

    import highspy

    try:
        sys.stdout.flush()
        saved = os.dup(1)
    except (OSError, ValueError):  # no real fd 1 (e.g. some embedded consoles)
        return highspy.Highs()
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, 1)
        return highspy.Highs()
    finally:
        os.dup2(saved, 1)
        os.close(saved)
        os.close(null)


class _Model:
    """A HiGHS model for a fixed (F, B) shape, with BESS band slots."""

    def __init__(self, F: int, B: int, cfg: Config, threads: int = 1):
        import highspy

        self.F, self.B = F, B
        nE = F * B
        self.iP, self.iC, self.iR = nE, nE + F, nE + 2 * F
        self.iS = nE + 3 * F  # sP, sC, sR, u
        self.iD = self.iS + 4  # discharge slots
        self.iG = self.iD + MAX_E_BANDS  # charge slots
        self.iB = {k: self.iG + MAX_E_BANDS + j * MAX_AS_BANDS for j, k in enumerate(RES)}
        self.nv = self.iB["R"] + MAX_AS_BANDS
        # rows: 0 balance, 1-3 reserves, 4..4+F-1 headroom, then 2 BESS rows
        self.rBal, self.rRes = 0, 1
        self.rHead = 4
        self.rBd, self.rBc = 4 + F, 5 + F
        self.nr = 6 + F
        cols: list[list[tuple[int, float]]] = [[] for _ in range(self.nv)]
        for f in range(F):
            for b in range(B):
                j = f * B + b
                cols[j] += [(self.rBal, 1.0), (self.rHead + f, 1.0)]
            for kk, off in enumerate((self.iP, self.iC, self.iR)):
                cols[off + f] += [(self.rRes + kk, 1.0), (self.rHead + f, 1.0)]
        for kk in range(3):
            cols[self.iS + kk].append((self.rRes + kk, 1.0))
        cols[self.iS + 3].append((self.rBal, 1.0))
        for j in range(MAX_E_BANDS):
            cols[self.iD + j] += [(self.rBal, 1.0), (self.rBd, 1.0)]
            cols[self.iG + j] += [(self.rBal, -1.0), (self.rBc, 1.0)]
        for kk, k in enumerate(RES):
            for j in range(MAX_AS_BANDS):
                c = [(self.rRes + kk, 1.0), (self.rBd, 1.0)]
                if k == "R":
                    c.append((self.rBc, 1.0))
                cols[self.iB[k] + j] += c
        start, index, value = [0], [], []
        for c in cols:
            c.sort()
            index += [r for r, _ in c]
            value += [v for _, v in c]
            start.append(len(index))
        inf = highspy.kHighsInf
        lp = highspy.HighsLp()
        lp.num_col_ = self.nv
        lp.num_row_ = self.nr
        lp.col_cost_ = np.zeros(self.nv)
        lp.col_lower_ = np.zeros(self.nv)
        ub = np.zeros(self.nv)
        ub[self.iS:self.iS + 4] = inf
        lp.col_upper_ = ub
        rl = np.full(self.nr, -inf)
        ru = np.full(self.nr, inf)
        rl[self.rBal] = ru[self.rBal] = 0.0
        rl[self.rRes:self.rRes + 3] = 0.0
        ru[self.rHead:self.rHead + F] = 0.0
        ru[self.rBd] = ru[self.rBc] = cfg.power_mw
        lp.row_lower_ = rl
        lp.row_upper_ = ru
        lp.a_matrix_.format_ = highspy.MatrixFormat.kColwise
        lp.a_matrix_.start_ = np.array(start, dtype=np.int32)
        lp.a_matrix_.index_ = np.array(index, dtype=np.int32)
        lp.a_matrix_.value_ = np.array(value, dtype=float)
        lp.a_matrix_.num_col_ = self.nv
        lp.a_matrix_.num_row_ = self.nr
        h = _new_highs()
        h.setOptionValue("output_flag", False)
        h.setOptionValue("log_to_console", False)
        h.setOptionValue("threads", threads)
        h.setOptionValue("random_seed", 0)
        h.setOptionValue("presolve", "off")
        h.setOptionValue("solver", "simplex")
        h.passModel(lp)
        self.h = h
        self.inf = inf
        self.optimal = highspy.HighsModelStatus.kOptimal
        self.all_cols = np.arange(self.nv, dtype=np.int32)
        self.all_rows = np.arange(self.nr, dtype=np.int32)
        self.cost = np.zeros(self.nv)
        self.cost[self.iS:self.iS + 3] = (cfg.shortfall_price_primary, cfg.shortfall_price_contingency,
                                          cfg.shortfall_price_regulation)
        self.cost[self.iS + 3] = cfg.voll
        self.ub = ub.copy()
        self.rl = rl.copy()
        self.ru = ru.copy()


class ClearingEngine:
    """Per-period clearing with re-used HiGHS models.

    ``bess_pmax``: limit of the BESS joint rows (default ``cfg.power_mw``).
    ``warm_start``: keep the simplex basis between calls (faster; prices agree with cold
    solves to solver tolerance except in exactly degenerate periods).
    """

    def __init__(self, cfg: Config | None = None, bess_pmax: float | None = None,
                 warm_start: bool = True, threads: int = 1):
        self.cfg = cfg or Config()
        self.bess_pmax = self.cfg.power_mw if bess_pmax is None else float(bess_pmax)
        self.warm_start = warm_start
        self.threads = threads
        self._models: dict[tuple[int, int], _Model] = {}
        self.n_solves = 0

    def _model(self, F: int, B: int) -> _Model:
        m = self._models.get((F, B))
        if m is None:
            m = _Model(F, B, self.cfg, self.threads)
            self._models[(F, B)] = m
        return m

    def clear(self, stack: PeriodStack, net_demand: float, req: Mapping[str, float] | Sequence[float],
              prev: np.ndarray | None = None, bess: BessOffer | None = None) -> ClearResult:
        """Clear one period.

        ``req``: {P, C, R} MW (or a 3-sequence). ``prev``: (F,) energy dispatch of the
        previous period (ramp state; None = no ramp limit, i.e. a cold start).
        ``bess``: optional :class:`BessOffer` (or a PeriodBid).
        """
        if bess is not None and not isinstance(bess, BessOffer):
            bess = BessOffer.from_period_bid(bess)
        F, B = stack.e_q.shape
        m = self._model(F, B)
        h = m.h
        if isinstance(req, Mapping):
            rq = (float(req["P"]), float(req["C"]), float(req["R"]))
        else:
            rq = tuple(float(x) for x in req)
        eq = _ramp_clip(stack.e_q, stack.av, prev, self.cfg.ramp_up_frac)
        cost = m.cost.copy()
        ub = m.ub.copy()
        nE = F * B
        cost[:nE] = stack.e_p.ravel()
        ub[:nE] = eq.ravel()
        cost[m.iP:m.iP + F] = stack.p_p
        cost[m.iC:m.iC + F] = stack.c_p
        cost[m.iR:m.iR + F] = stack.r_p
        ub[m.iP:m.iP + F] = stack.p_q
        ub[m.iC:m.iC + F] = stack.c_q
        ub[m.iR:m.iR + F] = stack.r_q
        nd = nc = 0
        nres = {k: 0 for k in RES}
        if bess is not None:
            dis, chg = bess.discharge, bess.charge
            if len(dis) > MAX_E_BANDS or len(chg) > MAX_E_BANDS:
                raise ValueError("too many BESS energy bands")
            nd, nc = len(dis), len(chg)
            for j, (p, q) in enumerate(dis):
                cost[m.iD + j] = p
                ub[m.iD + j] = q
            for j, (p, q) in enumerate(chg):
                cost[m.iG + j] = -p
                ub[m.iG + j] = q
            for k in RES:
                bands = bess.reserve(k)
                if len(bands) > MAX_AS_BANDS:
                    raise ValueError(f"too many BESS {k} bands")
                nres[k] = len(bands)
                for j, (p, q) in enumerate(bands):
                    cost[m.iB[k] + j] = p
                    ub[m.iB[k] + j] = q
        ub = np.maximum(ub, 0.0)
        rl = m.rl.copy()
        ru = m.ru.copy()
        rl[m.rBal] = ru[m.rBal] = float(net_demand)
        rl[m.rRes:m.rRes + 3] = rq
        ru[m.rHead:m.rHead + F] = stack.av
        ru[m.rBd] = ru[m.rBc] = self.bess_pmax
        if not self.warm_start:
            h.clearSolver()
        h.changeColsCost(m.nv, m.all_cols, cost)
        h.changeColsBounds(m.nv, m.all_cols, np.zeros(m.nv), ub)
        h.changeRowsBounds(m.nr, m.all_rows, rl, ru)
        h.run()
        self.n_solves += 1
        status = h.getModelStatus()
        if status != m.optimal:
            # retry once from scratch (a stale basis can rarely stall)
            h.clearSolver()
            h.run()
            status = h.getModelStatus()
            if status != m.optimal:
                raise EngineError(f"clearing LP not optimal: {h.modelStatusToString(status)}")
        sol = h.getSolution()
        x = np.asarray(sol.col_value)
        y = np.asarray(sol.row_dual)
        info = h.getInfo()
        r = ClearResult(
            E=float(y[m.rBal]), P=float(y[m.rRes]), C=float(y[m.rRes + 1]), R=float(y[m.rRes + 2]),
            sP=float(x[m.iS]), sC=float(x[m.iS + 1]), sR=float(x[m.iS + 2]), unserved=float(x[m.iS + 3]),
            cost=float(info.objective_function_value),
            gen_e=x[:nE].reshape(F, B).sum(1), gen_p=x[m.iP:m.iP + F].copy(),
            gen_c=x[m.iC:m.iC + F].copy(), gen_r=x[m.iR:m.iR + F].copy(),
            dis_bands=x[m.iD:m.iD + nd].copy(), chg_bands=x[m.iG:m.iG + nc].copy(),
            res_bands={k: x[m.iB[k]:m.iB[k] + nres[k]].copy() for k in RES},
        )
        return r


_DEFAULT: dict[tuple, ClearingEngine] = {}


def clear(stack: PeriodStack, net_demand: float, req, prev: np.ndarray | None = None,
          bess: BessOffer | None = None, cfg: Config | None = None) -> ClearResult:
    """Convenience wrapper around a module-level :class:`ClearingEngine` (one per Config)."""
    cfg = cfg or Config()
    key = (cfg.power_mw, cfg.ramp_up_frac, cfg.voll, cfg.shortfall_price_primary,
           cfg.shortfall_price_contingency, cfg.shortfall_price_regulation)
    eng = _DEFAULT.get(key)
    if eng is None:
        eng = _DEFAULT[key] = ClearingEngine(cfg)
    return eng.clear(stack, net_demand, req, prev, bess)


def clear_period(Eq, Ep, Pq, Pp, Cq, Cp, Rq, Rp, av, nd: float, req, prev=None,
                 ramp: float | None = None, penalties=None, voll: float | None = None,
                 bess: BessOffer | None = None, cfg: Config | None = None) -> ClearResult:
    """Array-argument form of :func:`clear` (``Eq``/``Ep`` (F, B); reserve arrays and ``av``
    (F,); ``req`` = (P, C, R)). ``ramp``, ``penalties`` = (P, C, R) and ``voll`` override the
    Config values."""
    cfg = cfg or Config()
    ch = {}
    if ramp is not None:
        ch["ramp_up_frac"] = float(ramp)
    if penalties is not None:
        ch.update(shortfall_price_primary=float(penalties[0]), shortfall_price_contingency=float(penalties[1]),
                  shortfall_price_regulation=float(penalties[2]))
    if voll is not None:
        ch["voll"] = float(voll)
    if ch:
        cfg = cfg.replace(**ch)
    f = lambda a: np.nan_to_num(np.asarray(a, dtype=float))  # noqa: E731
    st = PeriodStack(f(Eq), f(Ep), f(Pq), f(Pp), f(Cq), f(Cp), f(Rq), f(Rp), f(av))
    return clear(st, nd, req, prev, bess, cfg)


# --------------------------------------------------------------------------------------
# Price-taker awards
# --------------------------------------------------------------------------------------


@dataclass
class TakerResult:
    dis_bands: np.ndarray
    chg_bands: np.ndarray
    res_bands: dict

    @property
    def dis(self) -> float:
        return float(self.dis_bands.sum())

    @property
    def chg(self) -> float:
        return float(self.chg_bands.sum())

    @property
    def awards(self) -> dict[str, float]:
        d, c = self.dis, self.chg
        return {"dis": d, "chg": c, "net": d - c,
                **{k: float(np.sum(self.res_bands.get(k, 0.0))) for k in RES}}


def _in_money(margin: np.ndarray, tie: str) -> np.ndarray:
    return margin > 0 if tie == "strict" else margin >= 0


def price_taker_clear(bess: BessOffer, prices: Mapping[str, float], tie: str = "strict",
                      pmax: float = 100.0) -> TakerResult:
    """Awards for fixed clearing prices {E, P, C, R}.

    A discharge band clears if its offer is below the energy price, a charge band if its offer
    is above it, a reserve band if its offer is below that reserve's price. ``tie='strict'``
    (default) follows RC383's wording literally: a band offered exactly at the clearing price
    is NOT cleared. ``tie='inclusive'`` clears it in full.

    In-the-money bands are awarded in full unless the BESS joint rows (discharge + P + C + R <=
    pmax, charge + R <= pmax) would be exceeded; then MW go to the bands with the largest
    margin (|price - offer|) first, which maximises the BESS's surplus at the given prices. For
    bids that satisfy our strict offered-MW rule the joint rows never bind.
    """
    if not isinstance(bess, BessOffer):
        bess = BessOffer.from_period_bid(bess)
    if tie not in ("strict", "inclusive"):
        raise ValueError("tie must be 'strict' or 'inclusive'")
    lam = float(prices["E"])
    items = []  # (margin, kind, index, qty)
    dis, chg = bess.discharge, bess.charge
    for j, (p, q) in enumerate(dis):
        items.append((lam - p, "dis", j, q))
    for j, (p, q) in enumerate(chg):
        items.append((p - lam, "chg", j, q))
    for k in RES:
        pk = float(prices[k])
        for j, (p, q) in enumerate(bess.reserve(k)):
            items.append((pk - p, k, j, q))
    out = {"dis": np.zeros(len(dis)), "chg": np.zeros(len(chg)),
           **{k: np.zeros(len(bess.reserve(k))) for k in RES}}
    cap_d = cap_c = float(pmax)
    # margin-descending greedy; ties broken by a fixed kind order so results are deterministic
    order = {"dis": 0, "chg": 1, "R": 2, "C": 3, "P": 4}
    items.sort(key=lambda it: (-it[0], order[it[1]], it[2]))
    for margin, kind, j, q in items:
        if not _in_money(np.array(margin), tie):
            continue
        if kind == "chg":
            a = min(q, cap_c)
            cap_c -= a
        elif kind == "R":
            a = min(q, cap_d, cap_c)
            cap_d -= a
            cap_c -= a
        else:
            a = min(q, cap_d)
            cap_d -= a
        out[kind][j] = max(a, 0.0)
    return TakerResult(out["dis"], out["chg"], {k: out[k] for k in RES})


# --------------------------------------------------------------------------------------
# Replication of the training clearing prices
# --------------------------------------------------------------------------------------


def replicate(stacks: StackArrays, clearing: pd.DataFrame, start=None, end=None,
              engine: ClearingEngine | None = None, cold_start: bool = True,
              prev: np.ndarray | None = None) -> pd.DataFrame:
    """Re-clear the training periods with no BESS, chaining the ramp state, and compare.

    ``clearing``: frame from :func:`bessbid.io.load_clearing`. Periods [start, end] (inclusive;
    default all). The ramp state starts cold (no limit) at ``start`` unless ``prev`` is given.
    Returns one row per period: ts, model prices E/P/C/R, actual prices, abs errors, model
    and actual unmet MW, solve time.
    """
    import time

    eng = engine or ClearingEngine()
    c = clearing.sort_values("ts")
    if start is not None:
        c = c[c["ts"] >= pd.Timestamp(start)]
    if end is not None:
        c = c[c["ts"] <= pd.Timestamp(end)]
    idx = stacks.index_of(c["ts"].to_numpy())
    nd = c["net_demand_mw"].to_numpy(float)
    rq = c[["primary_requirement_mw", "contingency_requirement_mw", "regulation_requirement_mw"]].to_numpy(float)
    st = None if cold_start and prev is None else prev
    rows = []
    for n, i in enumerate(np.atleast_1d(idx)):
        t0 = time.perf_counter()
        r = eng.clear(stacks.period(int(i)), nd[n], rq[n], st)
        dt_ = time.perf_counter() - t0
        st = r.gen_e
        rows.append((r.E, r.P, r.C, r.R, r.sP, r.sC, r.sR, r.unserved, dt_))
    out = pd.DataFrame(rows, columns=["E", "P", "C", "R", "sP", "sC", "sR", "unserved", "solve_s"])
    out.insert(0, "ts", c["ts"].to_numpy())
    act = {"E": "energy_price_sgd_mwh", "P": "primary_price_sgd_mwh_equiv",
           "C": "contingency_price_sgd_mwh_equiv", "R": "regulation_price_sgd_mwh_equiv"}
    for k, col in act.items():
        out[k + "_act"] = c[col].to_numpy(float)
        out[k + "_err"] = (out[k] - out[k + "_act"]).abs()
    for k, col in (("sP", "unmet_primary_mw"), ("sC", "unmet_contingency_mw"), ("sR", "unmet_regulation_mw")):
        if col in c.columns:
            out[k + "_act"] = c[col].to_numpy(float)
    return out
