"""Checks against EMA's real files. Run with BESSBID_DATA_DIR=<folder> (optionally
BESSBID_WORK_DIR=<cache folder outside the repo>); skipped otherwise."""

import json

import numpy as np
import pandas as pd
import pytest

from bessbid import cli, io
from bessbid.config import Config
from bessbid.infoset import InfoSet
from bessbid.writer import read_back

pytestmark = pytest.mark.data
T = pd.Timestamp


@pytest.fixture(scope="module")
def ds(data_dir, data_work_dir):
    return io.load_all(data_dir, data_work_dir)


@pytest.fixture(scope="module")
def info(ds):
    return InfoSet.from_dataset(ds, Config())


def test_files(ds):
    f = ds.files
    assert len(f.stacks) == 3 and len(f.clearing) == 1
    assert len(f.predispatch_train) == 1 and len(f.predispatch_test) == 1
    assert f.template is not None and f.template.suffix == ".xlsx"


def test_clearing_and_predispatch(ds):
    c = ds.clearing
    assert len(c) == 5904
    assert c["ts"].min() == T("2026-07-01 00:00") and c["ts"].max() == T("2026-10-31 23:30")
    assert (np.diff(c["ts"].to_numpy()) == np.timedelta64(30, "m")).all()
    p = ds.predispatch_train
    assert len(p) == 5904 * 4 and sorted(p["lead_hours"].unique()) == [0.5, 1.0, 2.0, 3.0]
    q = ds.predispatch_test
    assert len(q) == 1440 * 4
    assert q["ts"].min() == T("2026-11-01 00:00") and q["ts"].max() == T("2026-11-30 23:30")
    # pre-dispatch actuals equal clearing net demand
    a = p[p["lead_hours"] == 2.0].set_index("ts")
    nd = a["synthetic_actual_load_mw"] - a["synthetic_actual_solar_mw"]
    assert (nd - c.set_index("ts")["net_demand_mw"]).abs().max() < 0.01


def test_stacks(ds):
    s = ds.stacks
    rep = io.stack_report(s)
    assert rep["rows"] == 1_880_064 and rep["periods"] == 7344 and rep["gaps"] == 0
    assert rep["first"] == "2026-06-03 00:00:00" and rep["last"] == "2026-11-02 23:30:00"
    assert rep["shifted_rows"] == 344_064 and rep["shift_hours"] == [8.0]
    assert rep["pub_lags"] == {"28 days 08:00:00": 1_880_064}
    assert not s.duplicated(io.STACK_KEY).any()
    cd = io.copied_dates(s)
    assert cd == list(pd.date_range("2026-06-03", "2026-06-30", freq="D"))


def test_shift_fix_keeps_the_right_copy(ds):
    """At Jul 1 00:00-07:30 the kept stack reproduces the clearing price through its
    marginal energy offer at net demand (the shifted copy would be ~100 % off)."""
    s = ds.stacks
    c = ds.clearing.set_index("ts")
    errs = []
    for t in pd.date_range("2026-07-01 00:00", periods=16, freq="30min"):
        e = s[(s["ts"] == t) & (s["product"] == "Energy")].sort_values("price")
        cum = e["offered_mw"].cumsum().to_numpy()
        i = min(int(np.searchsorted(cum, c.at[t, "net_demand_mw"])), len(e) - 1)
        errs.append(abs(e["price"].iloc[i] / c.at[t, "energy_price_sgd_mwh"] - 1))
    assert np.mean(errs) < 0.01


def test_november_information_cut(info):
    nov1 = T("2026-11-01 00:00")
    assert info.latest_stack_ts_known_at(info.cutoff(nov1)) == T("2026-10-03 14:30")
    # the test stack file becomes usable from Nov 1 09:30
    assert "testing" not in " ".join(map(str, info.stacks_for(T("2026-11-01 09:00"))["src"].unique()))
    assert "testing" in " ".join(map(str, info.stacks_for(T("2026-11-01 09:30"))["src"].unique()))
    assert info.latest_stack_ts_known_at(info.cutoff(T("2026-11-30 23:30"))) == T("2026-11-02 14:00")
    cl = info.clearing_for(nov1)
    assert cl["ts"].max() == T("2026-10-31 22:00") and len(cl) == 5904 - 3
    Ts = pd.date_range("2026-11-01 00:00", "2026-11-30 23:30", freq="30min")
    m = info.predispatch_for_many(Ts)
    assert len(m) == 1440 and (m["lead_hours"] == 2.0).all()
    assert (m["issue_time"] <= m.index - pd.Timedelta(minutes=65)).all()
    assert set(m["source"]) == {"test"}
    info.assert_no_leak()


def test_random_cutoffs(info):
    rng = np.random.default_rng(1)
    for _ in range(40):
        Tp = T("2026-07-01") + pd.Timedelta(minutes=30 * int(rng.integers(0, 153 * 48)))
        cut = info.cutoff(Tp)
        assert (info.stacks_for(Tp)["pub"] <= cut).all()
        k = info.predispatch_known_at(cut)
        assert (k["issue_time"] <= cut).all()
        r = info.predispatch_for(Tp)
        assert r is None or (r["issue_time"] <= cut and r["lead_hours"] in (2.0, 3.0))
    info.assert_no_leak()


def test_generate_regulation_on_real_template(data_dir, tmp_path):
    out = tmp_path / "submission_regulation_only.xlsx"
    rc = cli.main(["generate", "--data-dir", str(data_dir), "--out", str(out),
                   "--work-dir", str(tmp_path / "w"), "--strategy", "regulation", "--month", "2026-11"])
    assert rc == 0
    man = json.loads((tmp_path / "w" / "submission_regulation_only.manifest.json").read_text())
    assert man["validation"]["errors"] == 0 and man["roundtrip_ok"] and man["rows"] == 5760
    back = read_back(out)
    assert len(back) == 5760
    reg = back[back["product"] == "Regulation"]
    assert (reg["price_1"] == 0).all() and (reg["quantity_1"] == 100).all()
    assert back.loc[back["product"] != "Regulation", [f"quantity_{i}" for i in range(1, 11)]].isna().all().all()
