"""The full strategy on EMA's data (slow): November generate through the CLI, and a
pseudo-November (October, information cut as in November) replayed in the EMA-replica
engine."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from bessbid import cli
from bessbid.backtest import MarketData, simulate
from bessbid.bids import to_table
from bessbid.config import Config
from bessbid.infoset import InfoSet
from bessbid.io import load_all
from bessbid.strategy_full import plan_month
from bessbid.strategy_regulation import RegulationOnlyStrategy
from bessbid.writer import read_back

pytestmark = [pytest.mark.data, pytest.mark.slow]


def test_generate_full_november(data_dir, data_work_dir, tmp_path):
    out = tmp_path / "submission_full.xlsx"
    plan = tmp_path / "plan.csv"
    rc = cli.main(["generate", "--data-dir", str(data_dir), "--out", str(out), "--work-dir", str(data_work_dir),
                   "--month", "2026-11", "--plan-out", str(plan)])
    assert rc == 0
    man = json.loads((data_work_dir / "submission_full.manifest.json").read_text())
    assert man["strategy"] == "full"
    assert man["validation"]["errors"] == 0 and man["roundtrip_ok"] and man["rows"] == 5760
    assert man["information_audit"]["leaks"] == 0
    assert man["plan"]["info"]["soc_check"]["ok"]
    p = pd.read_csv(plan)
    assert len(p) == 1440
    for c in [c for c in p.columns if c.startswith("soc_c")]:
        assert p[c].min() >= 5.0 and p[c].max() <= 95.0 and p[c].iloc[-1] >= 50.0 - 1e-6
    back = read_back(out)
    e = back[back["product"] == "Energy"]
    assert e["quantity_1"].notna().sum() > 100  # the overlay is active
    # Nov 19 is the expected stress day: the plan discharges into it
    p["ts"] = pd.to_datetime(p["ts"])
    nov19 = p[p["ts"].dt.normalize() == pd.Timestamp("2026-11-19")]
    assert nov19["energy_mw"].clip(lower=0).sum() > 0
    assert (nov19["energy_mw"] >= 0).all()


def test_pseudo_november_october_beats_regulation(data_dir, data_work_dir):
    ds = load_all(data_dir, data_work_dir)
    cfg = Config()
    info = InfoSet.from_dataset(ds, cfg)
    bids, frame, inf = plan_month("2026-10", info, cfg)
    info.assert_no_leak()
    md = MarketData.from_dataset(ds, start=cfg.stack_fit_start)
    tb = to_table(bids)
    reg = to_table(RegulationOnlyStrategy().generate("2026-10", None, cfg))
    for mode in ("maker", "taker"):
        base = simulate(reg, "2026-10", md, mode=mode, cfg=cfg).summary["revenue"]
        for conv in ("charge", "sqrt", "discharge"):
            s = simulate(tb, "2026-10", md, mode=mode, convention=conv, cfg=cfg).summary
            assert s["breach_periods"] == 0
            assert s["revenue"] - base > 50_000
    assert np.isclose(frame["soc_binding"].iloc[-1], 50.0) or frame["soc_binding"].iloc[-1] >= 50.0
