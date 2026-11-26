"""Engine replication and backtest golden numbers on EMA's data (skipped unless
BESSBID_DATA_DIR is set). ``-m "not slow"`` skips the full-length runs."""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

from bessbid import io
from bessbid.backtest import MarketData, simulate
from bessbid.bids import energy_fixed, regulation_only, to_table
from bessbid.config import Config
from bessbid.engine import ClearingEngine, replicate
from bessbid.periods import month_slots

pytestmark = pytest.mark.data
T = pd.Timestamp
CFG = Config()


@pytest.fixture(scope="module")
def ds(data_dir, data_work_dir):
    return io.load_all(data_dir, data_work_dir)


@pytest.fixture(scope="module")
def md(ds):
    return MarketData.from_dataset(ds, start="2026-07-01")


def _check(rep: pd.DataFrame, tol: float = 0.01) -> None:
    for k in "EPCR":
        bad = rep[rep[k + "_err"] > tol]
        assert bad.empty, f"{k}: {len(bad)} periods off, e.g.\n{bad.head(3).to_string()}"
    for k in ("sP", "sC", "sR"):
        if k + "_act" in rep:
            assert (rep[k] - rep[k + "_act"]).abs().max() < 0.01


def test_replicate_sample_200(md):
    """200 periods (incl. the Oct 12 16:30-17:30 scarcity), each with a cold start one day
    earlier to build its ramp state."""
    c = md.clearing
    ts = c["ts"]
    rng = np.random.default_rng(12345)
    pool = ts[ts >= T("2026-07-02")].to_numpy()
    pool = pool[~np.isin(pool, pd.to_datetime(["2026-10-12 16:30", "2026-10-12 17:00", "2026-10-12 17:30"]).to_numpy())]
    must = pd.to_datetime(["2026-10-12 16:30", "2026-10-12 17:00", "2026-10-12 17:30"]).to_numpy()
    pick = np.unique(np.concatenate([must, rng.choice(pool, 197, replace=False)]))
    assert len(pick) == 200
    eng = ClearingEngine(CFG)
    reps = []
    for t in pick:
        t = T(t)
        rep = replicate(md.stacks, c, start=t - pd.Timedelta(days=1), end=t, engine=eng)
        reps.append(rep.iloc[[-1]])
    rep = pd.concat(reps, ignore_index=True)
    _check(rep)
    oct12 = rep.set_index("ts").loc[T("2026-10-12 16:30")]
    assert oct12["E"] > 1000  # scarcity period priced far above normal


@pytest.mark.slow
def test_replicate_all_training_periods(md):
    t0 = time.perf_counter()
    rep = replicate(md.stacks, md.clearing)  # cold start Jul 1, ramp chained, as EMA did
    el = time.perf_counter() - t0
    assert len(rep) == 5904
    _check(rep)
    for k in "EPCR":
        assert rep[k + "_err"].mean() < 0.001 and rep[k + "_err"].max() < 0.005
    assert el < 120


def _reg_table(month):
    return to_table([regulation_only(s.date, s.period, CFG) for s in month_slots(month)])


def test_golden_regulation_october(md):
    r = simulate(_reg_table("2026-10"), "2026-10", md, mode="maker", cfg=CFG)
    assert r.summary["revenue_per_30d"] == pytest.approx(2.0582e6, rel=1e-3)
    assert r.summary["runtime_s"] < 60
    assert r.summary["breach_periods"] == 0 and r.summary["soc_final"] == pytest.approx(50.0)


@pytest.mark.slow
def test_golden_regulation_jul_oct(md):
    tot = {"maker": 0.0, "taker": 0.0}
    days = 0.0
    for m in ("2026-07", "2026-08", "2026-09", "2026-10"):
        tbl = _reg_table(m)
        for mode in tot:
            s = simulate(tbl, m, md, mode=mode, cfg=CFG).summary
            tot[mode] += s["revenue"]
        days += s["days"]
    assert tot["maker"] / days * 30 == pytest.approx(1.416e6, rel=2e-3)
    assert tot["taker"] / days * 30 == pytest.approx(1.576e6, rel=2e-3)


def test_energy_schedule_clip_never_breaches(md):
    """A deliberately oversized schedule: with soc_policy='clip' no convention breaches."""
    def bid(s):
        if 7 <= s.period <= 12:
            return energy_fixed(s.date, s.period, -50, CFG)
        if s.period in (27, 28, 35, 36, 37, 38):
            return energy_fixed(s.date, s.period, 50, CFG)
        return regulation_only(s.date, s.period, CFG)

    tbl = to_table([bid(s) for s in month_slots("2026-09")])
    for conv in CFG.efficiency_conventions:
        r = simulate(tbl, "2026-09", md, mode="maker", convention=conv, soc_policy="clip", cfg=CFG)
        assert r.summary["breach_periods"] == 0
        assert CFG.soc_min_mwh - 1e-6 <= r.summary["soc_min"] and r.summary["soc_max"] <= CFG.soc_max_mwh + 1e-6
        assert r.summary["rev_E"] != 0
        f = r.frame
        # charging never earns energy revenue, discharging never pays
        assert (f.loc[f["chg"] > 0, "rev_E"] <= 0).all() and (f.loc[f["dis"] > 0, "rev_E"] >= 0).all()


def test_cli_backtest(data_dir, data_work_dir, capsys):
    import json

    from bessbid import cli

    rc = cli.main(["backtest", "--data-dir", str(data_dir), "--work-dir", str(data_work_dir),
                   "--strategy", "regulation", "--month", "2026-10", "--mode", "maker"])
    assert rc == 0
    rows = json.loads((data_work_dir / "backtest_regulation.json").read_text())
    oct_ = [r for r in rows if r["month"] == "2026-10"][0]
    assert oct_["revenue_per_30d"] == pytest.approx(2.0582e6, rel=1e-3)
    # a November workbook has no rows in October -> clean error
    from bessbid.writer import write_submission

    nov = to_table([regulation_only(s.date, s.period, CFG) for s in month_slots("2026-11")])
    out = write_submission(nov, io.find_template(data_dir), data_work_dir / "bt_nov.xlsx", CFG, "2026-11")
    rc = cli.main(["backtest", "--data-dir", str(data_dir), "--work-dir", str(data_work_dir),
                   "--bids", str(out), "--month", "2026-10", "--mode", "taker"])
    assert rc == 1 and "no rows in the window" in capsys.readouterr().err
