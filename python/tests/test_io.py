import os
import shutil

import numpy as np
import pandas as pd
import pytest

from bessbid import io
from synth import clearing_frame, predispatch_frame, stack_rows, write_dataset, write_stack_csv


@pytest.fixture
def ds_root(tmp_path):
    return write_dataset(tmp_path / "data")


def test_find_files(ds_root, template):
    shutil.copy(template, ds_root / "Test_Dataset" / template.name)
    (ds_root / "Test_Dataset" / "~$BESS_Competition_Template.xlsx").write_bytes(b"lock")
    (ds_root / "november_market_clearing_prices.csv").write_text("x\n")  # must be ignored
    (ds_root / "readme.txt").write_text("x")
    f = io.find_files(ds_root)
    assert [p.name for p in f.clearing] == ["training_market_clearing_prices.csv"]
    assert [p.name for p in f.predispatch_train] == ["training_pre_dispatch_forecasts.csv"]
    assert [p.name for p in f.predispatch_test] == ["november_pre_dispatch_forecasts.csv"]
    assert sorted(p.name for p in f.stacks) == [
        "(Part 1) training_offer_stacks_with_delay.csv",
        "(Part 2)training_offer_stacks_with_delay.csv",
        "testing_delayed_offer_stacks.csv",
    ]
    assert f.template.name == "BESS_Competition_Template.xlsx"
    assert [p.name for p in f.ignored] == ["november_market_clearing_prices.csv"]
    assert io.find_template(ds_root) == f.template
    d = f.describe()
    assert d["template"]["name"] == template.name


def test_find_files_duplicates_and_missing(ds_root, tmp_path):
    # an identical copy elsewhere in the tree is ignored
    src = next((ds_root / "(Part 2) Training").rglob("*.csv"))
    (ds_root / "copy").mkdir()
    shutil.copy(src, ds_root / "copy" / src.name)
    f = io.find_files(ds_root)
    assert len(f.stacks) == 3 and len(f.duplicates) == 1
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        io.find_files(empty)
    with pytest.raises(FileNotFoundError):
        io.find_files(tmp_path / "nope")
    with pytest.raises(FileNotFoundError):
        io.find_template(empty)


def test_parse_sgt_mixed():
    vals = ["2026-07-01T00:00:00+08:00", "2026-07-01 00:30:00+08:00", "2026-06-30T16:30:00Z",
            "2026-07-01 01:00:00", "2026-07-01T00:00:00+08:00"]
    out = io.parse_sgt(vals)
    assert out.dtype == np.dtype("datetime64[ns]")
    exp = pd.to_datetime(["2026-07-01 00:00", "2026-07-01 00:30", "2026-07-01 00:30",
                          "2026-07-01 01:00", "2026-07-01 00:00"]).to_numpy()
    assert (out == exp).all()
    with pytest.raises(io.SchemaError):
        io.parse_sgt(["2026-13-01 00:00:00+08:00"])
    with pytest.raises(io.SchemaError):
        io.parse_sgt(["2026-07-01 00:00:00+08:00", None])


def test_rebuild_stack_ts():
    label = io.parse_sgt(["2026-06-03 08:00:00+08:00", "2026-07-01 00:00:00+08:00",
                          "2026-07-01 07:30:00+08:00", "2026-07-01 00:00:00+08:00"])
    period = np.array([1, 33, 48, 1])
    ts = io.rebuild_stack_ts(label, period)
    exp = pd.to_datetime(["2026-06-03 00:00", "2026-06-30 16:00", "2026-06-30 23:30", "2026-07-01 00:00"]).to_numpy()
    assert (ts == exp).all()


def test_load_stacks_fixes_shift(ds_root, tmp_path):
    f = io.find_files(ds_root)
    s = io.load_stacks(f)
    per_period = len(stack_rows(pd.Timestamp("2026-07-01")))
    # Jun 30 12:00 .. Jul 1 17:30 = 60 periods, no gaps, no duplicates
    assert s["ts"].nunique() == 60 and len(s) == 60 * per_period
    assert not s.duplicated(io.STACK_KEY).any()
    assert s["ts"].min() == pd.Timestamp("2026-06-30 12:00")
    assert s["ts"].max() == pd.Timestamp("2026-07-01 17:30")
    t = pd.DatetimeIndex(s["ts"])
    assert ((t.hour * 2 + t.minute // 30 + 1) == s["period"]).all()
    assert int(s["shifted"].sum()) == 24 * per_period
    assert ((s["pub"] - s["ts"]) == pd.Timedelta(days=28, hours=8)).all()
    rep = io.stack_report(s)
    assert rep["gaps"] == 0 and rep["shift_hours"] == [8.0] and rep["periods"] == 60
    assert list(rep["pub_lags"]) == ["28 days 08:00:00"]
    for c in ("ts", "pub", "ts_label", "date"):
        assert s[c].dtype == np.dtype("datetime64[ns]")


def test_stack_conflicting_duplicates_raise(tmp_path):
    t = pd.Timestamp("2026-07-01 00:00")
    a = stack_rows(t)
    b = stack_rows(t)
    b[0]["offer_price_sgd_per_mwh_equiv"] += 1.0
    p1 = write_stack_csv(tmp_path / "a_offer_stacks.csv", a)
    p2 = write_stack_csv(tmp_path / "b_offer_stacks.csv", b)
    with pytest.raises(io.SchemaError, match="conflicting"):
        io.load_stacks([p1, p2])
    # exact duplicates (same file content twice) are dropped
    p3 = write_stack_csv(tmp_path / "c_offer_stacks.csv", a)
    s = io.load_stacks([p1, p3])
    assert len(s) == len(a)


def test_stack_schema_errors(tmp_path):
    rows = stack_rows(pd.Timestamp("2026-07-01 00:00"))
    df = pd.DataFrame(rows).drop(columns=["available_mw"])
    p = tmp_path / "x_offer_stacks.csv"
    df.to_csv(p)
    with pytest.raises(io.SchemaError, match="available_mw"):
        io.load_stacks([p])
    rows[0]["product"] = "Reg"
    p = write_stack_csv(tmp_path / "y_offer_stacks.csv", rows)
    with pytest.raises(io.SchemaError, match="unknown products"):
        io.load_stacks([p])
    rows = stack_rows(pd.Timestamp("2026-07-01 00:00"))
    rows[0]["offered_mw"] = "abc"
    p = write_stack_csv(tmp_path / "z_offer_stacks.csv", rows)
    with pytest.raises(io.SchemaError, match="non-numeric"):
        io.load_stacks([p])


def test_load_clearing_and_predispatch(ds_root):
    f = io.find_files(ds_root)
    c = io.load_clearing(f)
    assert len(c) == 96 and c["ts"].is_monotonic_increasing
    assert c["ts"].iloc[0] == pd.Timestamp("2026-07-01 00:00") and "data_label" not in c
    pdt = io.load_predispatch(f, "train")
    assert len(pdt) == 96 * 4
    assert {"issue_time", "synthetic_actual_load_mw"} <= set(pdt.columns)
    assert ((pdt["ts"] - pdt["issue_time"]) == pd.to_timedelta(pdt["lead_hours"], unit="h")).all()
    pte = io.load_predispatch(f, "test")
    assert len(pte) == 48 * 4 and "synthetic_actual_load_mw" not in pte
    with pytest.raises(ValueError):
        io.load_predispatch(f, "nov")


def test_clearing_schema_errors(tmp_path):
    df = clearing_frame(n=4)
    df.loc[1, "period"] = 7
    p = tmp_path / "training_market_clearing_prices.csv"
    df.to_csv(p, index=False)
    with pytest.raises(io.SchemaError, match="period"):
        io.load_clearing([p])
    df = clearing_frame(n=4).drop(columns=["net_demand_mw"])
    df.to_csv(p, index=False)
    with pytest.raises(io.SchemaError, match="net_demand_mw"):
        io.load_clearing([p])
    df = clearing_frame(n=4)
    df.loc[1, "period_sgt"] = "2026-07-01T00:10:00+08:00"
    df.to_csv(p, index=False)
    with pytest.raises(io.SchemaError, match="grid"):
        io.load_clearing([p])


def test_predispatch_issue_mismatch_warns(tmp_path):
    df = predispatch_frame(n=2)
    df.loc[0, "issue_time_sgt"] = "2026-06-30T20:30:00+08:00"
    p = tmp_path / "training_pre_dispatch_forecasts.csv"
    df.to_csv(p, index=False)
    with pytest.warns(UserWarning, match="issue_time"):
        io.load_predispatch([p])


def test_cache(ds_root, tmp_path):
    work = tmp_path / "work"
    f = io.find_files(ds_root)
    s1 = io.load_stacks(f, work)
    files = sorted((work / "cache").glob("stacks_*.parquet"))
    assert len(files) == 1
    s2 = io.load_stacks(f, work)  # from cache
    pd.testing.assert_frame_equal(s1, s2)
    # touching a source file changes the key
    p = f.stacks[0]
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    io.load_stacks(f, work)
    assert len(list((work / "cache").glob("stacks_*.parquet"))) == 2
    ds = io.load_all(ds_root, work)
    assert ds.predispatch_test is not None and len(ds.clearing) == 96


def test_copied_dates(tmp_path):
    rows = []
    for t in pd.date_range("2026-06-03 00:00", periods=4, freq="30min"):
        r = stack_rows(t)
        for x in r:  # make June 3 identical in content to July 1
            x["offer_price_sgd_per_mwh_equiv"] -= 3 - 1
        rows += r
    for t in pd.date_range("2026-07-01 00:00", periods=4, freq="30min"):
        rows += stack_rows(t)
    for t in pd.date_range("2026-06-04 00:00", periods=4, freq="30min"):
        rows += stack_rows(t)
    p = write_stack_csv(tmp_path / "x_offer_stacks.csv", rows)
    s = io.load_stacks([p])
    assert io.copied_dates(s) == [pd.Timestamp("2026-06-03")]


def test_classify_by_whole_words_and_folders(tmp_path):
    from bessbid.io import _classify

    assert _classify("training_market_clearing_prices.csv") == "clearing"
    assert _classify("market_clearing_prices.csv") == "ignored"
    assert _classify("market_clearing_prices.csv", ("train",)) == "clearing"
    assert _classify("pre_dispatch_forecasts.csv", ("train",)) == "predispatch_train"
    assert _classify("pre_dispatch_forecasts.csv", ("Training", "test")) == "predispatch_test"
    assert _classify("pre_dispatch_forecasts.csv", ("Phase_1_Competition_Test_Dataset",)) == "predispatch_test"
    assert _classify("pre-dispatch forecasts (constrained).csv") == "predispatch_test"
    assert _classify("november_pre_dispatch_forecasts.csv", ("(Part 1) Training",)) == "predispatch_test"
    assert _classify("(Part 2)training_offer_stacks_with_delay.csv") == "stacks"
    # a generic layout: train/ and test/ folders, names without 'train'
    for d, name in (("train", "market_clearing_prices.csv"), ("train", "pre_dispatch_forecasts.csv"),
                    ("test", "pre_dispatch_forecasts.csv"), ("train", "offer_stacks.csv")):
        (tmp_path / d).mkdir(exist_ok=True)
        (tmp_path / d / name).write_text("x\n1\n")
    f = io.find_files(tmp_path)
    assert [p.parent.name for p in f.clearing] == ["train"]
    assert [p.parent.name for p in f.predispatch_train] == ["train"]
    assert [p.parent.name for p in f.predispatch_test] == ["test"]


def test_csv_header_whitespace_is_stripped(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("\ufeffts , period,  energy_price_sgd_mwh\n2026-07-01 00:00,1,100\n", encoding="utf-8")
    df = io._read_csv(p)
    assert list(df.columns) == ["ts", "period", "energy_price_sgd_mwh"]
