"""A small synthetic market for forecast-pipeline unit tests (made-up numbers only).

Four units, two energy bands each, one band per reserve. Two periodic outage cycles
(5-day and 7-day), a raised contingency requirement and a demand surge tied to the 7-day
cycle, log offer prices with an outage and weekend factor, and clearing prices from the
reference LP. Frames have the same columns as :mod:`bessbid.io` returns."""

from __future__ import annotations

import numpy as np
import pandas as pd

from bessbid import _clearlp

START = pd.Timestamp("2026-07-01")
PUB = pd.Timedelta(days=28, hours=8)
FAC = ["SYN_CCGT_A", "SYN_CCGT_B", "SYN_CCGT_C", "SYN_OTHER_D"]
NOM = np.array([1600.0, 1500.0, 900.0, 800.0])
BASE = np.array([60.0, 75.0, 95.0, 130.0])
BETA, WEEKEND = 0.0004, -0.08
# cycles: (period days, anchor offset from START, duration periods, unit taken out)
CYC_A = (5, pd.Timedelta(days=2, hours=15), 20, 2)  # unit C (900 MW)
CYC_B = (7, pd.Timedelta(days=3, hours=12), 60, 3)  # unit D (800 MW)
WIN_OFF, WIN_DUR, SURGE, CREQ = 24, 36, 300.0, 550.0


def _active(t: pd.DatetimeIndex, cyc) -> np.ndarray:
    P, off, dur, _ = cyc
    n = ((t - (START + off)) // pd.Timedelta(minutes=30)).to_numpy()
    return np.mod(n, P * 48) < dur


def _window(t: pd.DatetimeIndex) -> np.ndarray:
    P, off, _, _ = CYC_B
    n = np.mod(((t - (START + off)) // pd.Timedelta(minutes=30)).to_numpy(), P * 48)
    return (n >= WIN_OFF) & (n < WIN_OFF + WIN_DUR)


def build(days: int = 70, seed: int = 7) -> dict:
    rng = np.random.default_rng(seed)
    ts = pd.date_range(START, periods=days * 48, freq="30min")
    n = len(ts)
    pod = np.asarray(ts.hour * 2 + ts.minute // 30)
    we = np.asarray(ts.dayofweek >= 5)
    av = np.tile(NOM, (n, 1))
    stat = np.full((n, 4), "none", dtype=object)
    for cyc in (CYC_A, CYC_B):
        m = _active(ts, cyc)
        av[m, cyc[3]] = 0.0
        stat[m, cyc[3]] = "planned"
    out = (NOM[None, :] - av).sum(1)
    hour = np.asarray(ts.hour + ts.minute / 60, dtype=float)
    shape = 1 + 0.05 * np.sin((hour - 6) / 24 * 2 * np.pi)
    noise = rng.normal(0, 0.03, (n, 4))
    logf = BETA * out + WEEKEND * we
    Ep = np.empty((n, 4, 2))
    for b, mult in enumerate((1.0, 1.35)):
        Ep[:, :, b] = BASE[None, :] * mult * shape[:, None] * np.exp(logf[:, None] + noise)
    Pp = (2.0 + 0.5 * np.arange(4))[None, :] * np.exp(logf[:, None] + noise)
    Cp = (9.0 + 1.0 * np.arange(4))[None, :] * np.exp(logf[:, None] + noise)
    Rp = (12.0 + 1.5 * np.arange(4))[None, :] * np.exp(logf[:, None] + noise)
    Eq = np.stack([0.5 * av, 0.5 * av], axis=2)
    Pq, Cq, Rq = 0.08 * av, 0.25 * av, 0.06 * av
    Rq[:, 3] = 0.0  # the OTHER unit offers no regulation

    # demand: daily shape + weekend dip + surge window; solar by day only
    load = 2400 + 250 * np.sin((hour - 9) / 24 * 2 * np.pi) - 120 * we + SURGE * _window(ts)
    load = load + rng.normal(0, 25, n)
    solar = np.where((hour >= 7) & (hour < 18), 180 * np.sin((hour - 7) / 11 * np.pi), 0.0)
    nd = load - solar
    creq = np.where(_window(ts), CREQ, 480.0)

    # clearing prices from the reference LP
    rows = []
    prev = None
    for i in range(n):
        r = _clearlp.clear(Eq[i], Ep[i], Pq[i], Pp[i], Cq[i], Cp[i], Rq[i], Rp[i], av[i], nd[i],
                           (110.0, creq[i], 105.0), prev)
        prev = r.gen
        rows.append((r.E, r.P, r.C, r.R))
    pr = np.array(rows)
    clearing = pd.DataFrame({
        "ts": ts, "period": pod + 1,
        "energy_price_sgd_mwh": pr[:, 0], "primary_price_sgd_mwh_equiv": pr[:, 1],
        "contingency_price_sgd_mwh_equiv": pr[:, 2], "regulation_price_sgd_mwh_equiv": pr[:, 3],
        "load_mw": load, "solar_mw": solar, "net_demand_mw": nd,
        "unserved_energy_mw": 0.0, "unmet_primary_mw": 0.0, "unmet_contingency_mw": 0.0,
        "unmet_regulation_mw": 0.0, "primary_requirement_mw": 110.0,
        "contingency_requirement_mw": creq, "regulation_requirement_mw": 105.0,
        "raw_capacity_margin_mw": av.sum(1) - nd,
    })

    # pre-dispatch: leads 3/2/1/0.5; known outage = outage at the issue time
    pdr = []
    for L in (3.0, 2.0, 1.0, 0.5):
        issue = ts - pd.Timedelta(hours=L)
        k = np.searchsorted(ts, issue)
        k_ok = np.clip(k, 0, n - 1)
        known = np.where(k >= 0, out[k_ok], 0.0)
        err = rng.normal(0, 20 + 25 * L, n)
        sol_f = np.maximum(solar + rng.normal(0, 15, n), 0.0) + (5.0 * L)  # small night solar
        pdr.append(pd.DataFrame({
            "ts": ts, "period": pod + 1, "issue_time": issue, "lead_hours": L,
            "forecast_load_mw": load + err, "forecast_solar_mw": sol_f,
            "known_outage_mw_at_issue": known,
            "synthetic_actual_load_mw": load, "synthetic_actual_solar_mw": solar,
        }))
    pre = pd.concat(pdr, ignore_index=True).sort_values(["ts", "issue_time"]).reset_index(drop=True)

    # stacks (tidy)
    recs = []
    prods = [("Energy", 1), ("Energy", 2), ("Primary", 1), ("Contingency", 1), ("Regulation", 1)]
    for f in range(4):
        for p, b in prods:
            q = {"Energy": Eq[:, f, b - 1], "Primary": Pq[:, f], "Contingency": Cq[:, f], "Regulation": Rq[:, f]}[p]
            pp = {"Energy": Ep[:, f, b - 1], "Primary": Pp[:, f], "Contingency": Cp[:, f], "Regulation": Rp[:, f]}[p]
            recs.append(pd.DataFrame({
                "ts": ts, "date": ts.normalize(), "period": pod + 1, "facility_id": FAC[f], "product": p,
                "band": b, "offered_mw": q, "price": np.round(pp, 3), "available_mw": av[:, f],
                "outage_status": stat[:, f], "pub": ts + PUB,
            }))
    stacks = pd.concat(recs, ignore_index=True)
    stacks["product"] = stacks["product"].astype("category")
    stacks["facility_id"] = stacks["facility_id"].astype("category")
    stacks["outage_status"] = stacks["outage_status"].astype("category")
    return dict(clearing=clearing, predispatch=pre, stacks=stacks, nd=nd, out=out)


def info_from(world: dict, cfg=None):
    """InfoSet over the synthetic world."""
    from bessbid.config import Config
    from bessbid.infoset import InfoSet

    p = world["predispatch"]
    return InfoSet(cfg or Config(), world["clearing"], p, None, world["stacks"])
