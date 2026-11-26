"""Tiny synthetic stand-ins for EMA's CSV files, in the same column layout and the same
mixed timestamp formats (made-up numbers; no competition data)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

LABEL = "SYNTHETIC - NOT ACTUAL NEMS DATA"
PUB_LAG = pd.Timedelta(days=28, hours=8)


def fmt(ts: pd.Timestamp, sep: str = "T") -> str:
    return ts.strftime(f"%Y-%m-%d{sep}%H:%M:%S") + "+08:00"


def pod(ts: pd.Timestamp) -> int:
    return ts.hour * 2 + ts.minute // 30 + 1


def clearing_frame(start="2026-07-01 00:00", n=96) -> pd.DataFrame:
    ts = pd.date_range(start, periods=n, freq="30min")
    rng = np.arange(n)
    return pd.DataFrame(
        {
            "period_sgt": [fmt(t) for t in ts],
            "period": [pod(t) for t in ts],
            "energy_price_sgd_mwh": 200 + rng,
            "primary_price_sgd_mwh_equiv": 3 + 0 * rng,
            "contingency_price_sgd_mwh_equiv": 11 + 0 * rng,
            "regulation_price_sgd_mwh_equiv": 15 + 0 * rng,
            "load_mw": 7000 + rng,
            "solar_mw": 0 * rng,
            "net_demand_mw": 7000 + rng,
            "unserved_energy_mw": 0,
            "unmet_primary_mw": 0,
            "unmet_contingency_mw": 0,
            "unmet_regulation_mw": 0,
            "primary_requirement_mw": 110,
            "contingency_requirement_mw": 480,
            "regulation_requirement_mw": 105,
            "raw_capacity_margin_mw": 2000,
            "data_label": LABEL,
        }
    )


def predispatch_frame(start="2026-07-01 00:00", n=96, train=True, leads=(3.0, 2.0, 1.0, 0.5)) -> pd.DataFrame:
    rows = []
    for t in pd.date_range(start, periods=n, freq="30min"):
        for L in leads:
            issue = t - pd.Timedelta(hours=L)
            r = {
                "period_sgt": fmt(t),
                "period": pod(t),
                "issue_time_sgt": fmt(issue),
                "lead_hours": L,
                "forecast_load_mw": 7000 + 10 * L,
                "forecast_solar_mw": 5.0 * L,
                "known_outage_mw_at_issue": 100 * L,
            }
            if train:
                r["synthetic_actual_load_mw"] = 7000.0
                r["synthetic_actual_solar_mw"] = 0.0
            r["data_label"] = LABEL
            rows.append(r)
    return pd.DataFrame(rows)


FAC = ["SYN_CCGT_001", "SYN_OTHER_002"]
PRODS = [("Energy", 1), ("Energy", 2), ("Primary", 1), ("Contingency", 1), ("Regulation", 1)]


def stack_rows(true_ts: pd.Timestamp, label_shift=pd.Timedelta(0), sep=" ") -> list[dict]:
    out = []
    for f_i, f in enumerate(FAC):
        for p, b in PRODS:
            out.append(
                {
                    "period_sgt": fmt(true_ts + label_shift, sep),
                    "period": pod(true_ts),
                    "facility_id": f,
                    "product": p,
                    "band": b,
                    "offered_mw": 50.0 + b,
                    "offer_price_sgd_per_mwh_equiv": round(80 + 10 * b + f_i + true_ts.hour * 0.5 + true_ts.day, 3),
                    "available_mw": 250.0,
                    "outage_status": "none",
                    "data_label": "SYNTHETIC NOT ACTUAL NEMS DATA",
                    "assumed_publication_time_sgt": fmt(true_ts + PUB_LAG, " "),
                }
            )
    return out


def write_stack_csv(path: Path, rows: list[dict], start_idx: int = 0) -> Path:
    df = pd.DataFrame(rows)
    df.index = np.arange(start_idx, start_idx + len(df))
    df.to_csv(path, index=True)
    return path


def write_dataset(root: Path, with_test=True) -> Path:
    """EMA-like nested folder with Part-1 stacks carrying the +8 h label shift."""
    p1 = root / "(Part 1) Training" / "(Part 1) Training"
    p2 = root / "(Part 2) Training" / "(Part 2) Training"
    te = root / "Test_Dataset"
    for d in (p1, p2, te):
        d.mkdir(parents=True, exist_ok=True)
    clearing_frame().to_csv(p1 / "training_market_clearing_prices.csv", index=False)
    predispatch_frame().to_csv(p1 / "training_pre_dispatch_forecasts.csv", index=False)
    # Part 1: Jun 30 12:00 .. Jun 30 23:30 labelled +8 h, then Jul 1 00:00 .. 05:30 correct.
    rows = []
    for t in pd.date_range("2026-06-30 12:00", "2026-06-30 23:30", freq="30min"):
        rows += stack_rows(t, pd.Timedelta(hours=8), " ")
    for t in pd.date_range("2026-07-01 00:00", "2026-07-01 05:30", freq="30min"):
        rows += stack_rows(t, pd.Timedelta(0), " ")
    write_stack_csv(p1 / "(Part 1) training_offer_stacks_with_delay.csv", rows)
    rows2 = []
    for t in pd.date_range("2026-07-01 06:00", "2026-07-01 11:30", freq="30min"):
        rows2 += stack_rows(t, pd.Timedelta(0), " ")
    write_stack_csv(p2 / "(Part 2)training_offer_stacks_with_delay.csv", rows2, start_idx=len(rows))
    if with_test:
        rows3 = []
        for t in pd.date_range("2026-07-01 12:00", "2026-07-01 17:30", freq="30min"):
            rows3 += stack_rows(t, pd.Timedelta(0), "T")
        write_stack_csv(te / "testing_delayed_offer_stacks.csv", rows3)
        predispatch_frame("2026-07-03 00:00", 48, train=False).to_csv(
            te / "november_pre_dispatch_forecasts.csv", index=False
        )
    return root
