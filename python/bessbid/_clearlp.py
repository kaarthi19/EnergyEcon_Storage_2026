"""Private, minimal single-period clearing LP used by the forecast pipeline.

Same formulation as EMA's engine as reverse-engineered in Phase A (reproduces all 5904
training periods to 0.002 SGD/MWh):

    min  sum_fb pE_fb e_fb + sum_f (pP_f p_f + pC_f c_f + pR_f r_f)
         + 750 sP + 1000 sC + 1200 sR + VoLL u
    s.t. sum_fb e_fb + u            = nd                      (energy price = dual)
         sum_f p_f + sP            >= reqP                    (primary price = dual)
         sum_f c_f + sC            >= reqC                    (contingency)
         sum_f r_f + sR            >= reqR                    (regulation)
         sum_b e_fb + p_f + c_f + r_f <= av_f                 (per facility)
         0 <= e_fb <= q_fb (trimmed by the ramp limit), 0 <= p_f <= Pq_f, ...

The ramp-up limit e_f(t) <= e_f(t-1) + ramp * av_f(t) is applied by trimming the facility's
energy bands in band order (bands are price-ascending), as in Phase A.

:func:`clear` is the entry point used by :mod:`bessbid.forecast`. When the shared
:mod:`bessbid.engine` module provides a compatible ``clear_period`` the forecast module may
use that instead; this file stays as the reference implementation for tests.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = ["ClearResult", "clear", "PENALTIES"]

PENALTIES = (750.0, 1000.0, 1200.0)


@dataclass(frozen=True)
class ClearResult:
    E: float
    P: float
    C: float
    R: float
    sP: float
    sC: float
    sR: float
    unserved: float
    gen: np.ndarray  # energy dispatch per facility (MW)

    def prices(self) -> tuple[float, float, float, float]:
        return (self.E, self.P, self.C, self.R)


_STRUCT: dict = {}


def _structure(F: int, B: int):
    """Constraint matrix (CSC) for F facilities with B energy bands, built once per shape."""
    key = (F, B)
    if key in _STRUCT:
        return _STRUCT[key]
    nE = F * B
    iP, iC, iR = nE, nE + F, nE + 2 * F
    iS = nE + 3 * F
    nv = iS + 4  # sP, sC, sR, u
    # rows: 0 = energy balance; 1..3 = reserve requirements; 4..4+F-1 = facility capacity
    cols: list[list[tuple[int, float]]] = [[] for _ in range(nv)]
    for f in range(F):
        for b in range(B):
            j = f * B + b
            cols[j].append((0, 1.0))
            cols[j].append((4 + f, 1.0))
        for k, off in enumerate((iP, iC, iR)):
            cols[off + f].append((1 + k, 1.0))
            cols[off + f].append((4 + f, 1.0))
    for k in range(3):
        cols[iS + k].append((1 + k, 1.0))
    cols[iS + 3].append((0, 1.0))
    start = [0]
    index: list[int] = []
    value: list[float] = []
    for c in cols:
        for r, v in c:
            index.append(r)
            value.append(v)
        start.append(len(index))
    s = dict(
        nE=nE, iP=iP, iC=iC, iR=iR, iS=iS, nv=nv, nr=4 + F,
        start=np.array(start, dtype=np.int32),
        index=np.array(index, dtype=np.int32),
        value=np.array(value, dtype=np.float64),
    )
    _STRUCT[key] = s
    return s


def trim_ramp(Eq: np.ndarray, av: np.ndarray, prev: np.ndarray | None, ramp: float) -> np.ndarray:
    """Energy band quantities after the ramp-up limit prev + ramp * av (band order)."""
    if prev is None:
        return Eq
    cap = np.asarray(prev, float) + ramp * np.asarray(av, float)
    cum = np.cumsum(Eq, axis=1)
    return np.clip(cap[:, None] - (cum - Eq), 0.0, Eq)


def clear(
    Eq, Ep, Pq, Pp, Cq, Cp, Rq, Rp, av, nd: float, req, prev=None,
    ramp: float = 0.42, penalties=PENALTIES, voll: float = 4500.0,
) -> ClearResult:
    """Clear one period. ``Eq``/``Ep`` are (F, B); reserve arrays are (F,); ``req`` is
    (reqP, reqC, reqR). Raises RuntimeError if HiGHS does not reach optimality."""
    import highspy

    Eq = np.asarray(Eq, float)
    F, B = Eq.shape
    s = _structure(F, B)
    q = trim_ramp(Eq, av, prev, ramp)
    cost = np.concatenate([
        np.asarray(Ep, float).ravel(), np.asarray(Pp, float), np.asarray(Cp, float),
        np.asarray(Rp, float), np.asarray(penalties, float), [float(voll)],
    ])
    ub = np.concatenate([
        q.ravel(), np.asarray(Pq, float), np.asarray(Cq, float), np.asarray(Rq, float),
        [np.inf] * 4,
    ])
    lb = np.zeros(s["nv"])
    inf = highspy.kHighsInf
    ub = np.where(np.isinf(ub), inf, ub)
    rlo = np.concatenate([[nd], np.asarray(req, float), np.full(F, -inf)])
    rhi = np.concatenate([[nd], [inf] * 3, np.asarray(av, float)])

    lp = highspy.HighsLp()
    lp.num_col_ = s["nv"]
    lp.num_row_ = s["nr"]
    lp.col_cost_ = cost
    lp.col_lower_ = lb
    lp.col_upper_ = ub
    lp.row_lower_ = rlo
    lp.row_upper_ = rhi
    lp.a_matrix_.format_ = highspy.MatrixFormat.kColwise
    lp.a_matrix_.start_ = s["start"]
    lp.a_matrix_.index_ = s["index"]
    lp.a_matrix_.value_ = s["value"]
    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    h.setOptionValue("threads", 1)
    h.setOptionValue("random_seed", 0)
    h.passModel(lp)
    h.run()
    if h.getModelStatus() != highspy.HighsModelStatus.kOptimal:
        raise RuntimeError(f"clearing LP not optimal: {h.modelStatusToString(h.getModelStatus())}")
    sol = h.getSolution()
    x = np.asarray(sol.col_value)
    y = np.asarray(sol.row_dual)
    gen = x[: s["nE"]].reshape(F, B).sum(1)
    iS = s["iS"]
    return ClearResult(
        E=float(y[0]), P=float(y[1]), C=float(y[2]), R=float(y[3]),
        sP=float(x[iS]), sC=float(x[iS + 1]), sR=float(x[iS + 2]), unserved=float(x[iS + 3]),
        gen=gen,
    )
