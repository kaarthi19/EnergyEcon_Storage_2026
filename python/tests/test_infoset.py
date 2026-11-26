import numpy as np
import pandas as pd
import pytest

from bessbid import io
from bessbid.config import Config
from bessbid.infoset import AuditEntry, InfoSet, LeakError
from synth import write_dataset


@pytest.fixture
def info(tmp_path):
    root = write_dataset(tmp_path / "data")
    ds = io.load_all(root)
    return InfoSet.from_dataset(ds, Config())


def T(s):
    return pd.Timestamp(s)


def test_cutoff(info):
    assert info.cutoff("2026-07-01 12:00") == T("2026-07-01 10:55")
    assert info.cutoff(pd.Timestamp("2026-07-01 12:00", tz="Asia/Singapore")) == T("2026-07-01 10:55")


def test_predispatch_for_prefers_lead2(info):
    r = info.predispatch_for("2026-07-01 12:00")
    assert r["lead_hours"] == 2.0 and r["issue_time"] == T("2026-07-01 10:00")
    assert "synthetic_actual_load_mw" not in r.index
    v = info.predispatch_vintages("2026-07-01 12:00")
    assert list(v["lead_hours"]) == [2.0, 3.0]  # lead 1 h / 0.5 h are after the gate
    # non-strict: any own vintage issued by the cutoff, latest first (same answer at 65 min)
    assert info.predispatch_for("2026-07-01 12:00", strict=False)["lead_hours"] == 2.0


def test_predispatch_fallback_to_lead3(tmp_path):
    root = write_dataset(tmp_path / "data")
    ds = io.load_all(root)
    pdt = ds.predispatch_train
    pdt = pdt[~((pdt["ts"] == T("2026-07-01 12:00")) & (pdt["lead_hours"] == 2.0))]
    info = InfoSet(Config(), ds.clearing, pdt, None, ds.stacks)
    assert info.predispatch_for("2026-07-01 12:00")["lead_hours"] == 3.0
    # gate of 125 min: lead 2 h (issued T-120) is now illegal
    info2 = InfoSet(Config(gate_closure_min=125), ds.clearing, ds.predispatch_train)
    assert info2.predispatch_for("2026-07-01 12:00")["lead_hours"] == 3.0
    # nothing legal -> None
    info3 = InfoSet(Config(gate_closure_min=200), ds.clearing, ds.predispatch_train)
    assert info3.predispatch_for("2026-07-01 12:00") is None


def test_predispatch_for_many(info):
    Ts = pd.date_range("2026-07-01 00:00", periods=96, freq="30min")
    many = info.predispatch_for_many(Ts)
    assert len(many) == 96 and (many["lead_hours"] == 2.0).all()
    assert (many["issue_time"] <= many.index - pd.Timedelta(minutes=65)).all()
    one = info.predispatch_for(Ts[10])
    assert many.loc[Ts[10], "forecast_load_mw"] == one["forecast_load_mw"]
    # test file periods are found as well
    m2 = info.predispatch_for_many(pd.date_range("2026-07-03 00:00", periods=48, freq="30min"))
    assert len(m2) == 48 and set(m2["source"]) == {"test"}


def test_predispatch_known_at_masks_actuals(info):
    t = T("2026-07-01 06:00")
    k = info.predispatch_known_at(t)
    assert (k["issue_time"] <= t).all()
    ended = (k["ts"] + pd.Timedelta(minutes=30)) < t  # strict: ended before t
    assert k.loc[ended, "synthetic_actual_load_mw"].notna().all()
    assert k.loc[~ended, "synthetic_actual_load_mw"].isna().all()
    assert (~ended).any()


def test_stacks_known_at(info):
    t = T("2026-07-29 20:00")  # pub = ts + 28d 8h -> stacks up to Jul 1 12:00
    s = info.stacks_known_at(t)
    assert (s["pub"] <= t).all() and s["ts"].max() == T("2026-07-01 12:00")
    s2 = info.stacks_for(T("2026-07-29 21:05"))
    assert s2["ts"].max() == T("2026-07-01 12:00")
    assert info.latest_stack_ts_known_at(t) == T("2026-07-01 12:00")
    assert len(info.stacks_known_at(T("2026-01-01"))) == 0
    assert info.latest_stack_ts_known_at(T("2026-01-01")) is None


def test_stack_pub_rules(tmp_path):
    root = write_dataset(tmp_path / "data")
    ds = io.load_all(root)
    t = T("2026-07-29 04:00")  # ts + 28 d covers Jul 1 04:00; column rule only Jun 30 20:00 (inclusive)
    a = InfoSet(Config(stack_pub_rule="column"), stacks=ds.stacks).stacks_known_at(t)
    b = InfoSet(Config(stack_pub_rule="ts_plus_28d"), stacks=ds.stacks).stacks_known_at(t)
    c = InfoSet(Config(stack_pub_rule="latest"), stacks=ds.stacks).stacks_known_at(t)
    assert a["ts"].max() == T("2026-06-30 20:00")
    assert b["ts"].max() == T("2026-07-01 04:00")
    assert len(c) == len(a)


def test_clearing_known_at(info):
    c = info.clearing_known_at(T("2026-07-01 10:00"))
    # strict: the 09:30-10:00 period ends exactly at the decision time and is not used
    assert c["ts"].max() == T("2026-07-01 09:00") and len(c) == 19
    assert len(info.clearing_known_at(T("2026-07-01 10:00:01"))) == 20
    assert len(info.clearing_for(T("2026-07-01 12:00"))) == 21  # cut 10:55 -> through 10:00-10:30


def test_audit_log_and_leak(info):
    info.clear_audit()
    info.predispatch_for("2026-07-01 12:00")
    info.stacks_known_at(T("2026-07-29 20:00"))
    info.clearing_known_at(T("2026-07-01 10:00"))
    df = info.audit_frame()
    assert list(df["accessor"]) == ["predispatch_for", "stacks_known_at", "clearing_known_at"]
    assert (df["max_time_used"] <= df["decision_time"]).all()
    info.assert_no_leak()
    s = info.audit_summary()
    assert s["leaks"] == 0 and s["min_margin_min"] >= 0
    with pytest.raises(LeakError):
        info._record("x", T("2026-07-01 00:00"), T("2026-07-01 00:30"), 1)
    assert not AuditEntry("x", T("2026-07-01"), T("2026-07-02"), 1).ok


def test_property_random_periods(info):
    """For random decision periods, nothing returned is newer than the period's cutoff."""
    rng = np.random.default_rng(0)
    base = T("2026-06-30 00:00")
    for _ in range(100):
        Tp = base + pd.Timedelta(minutes=30 * int(rng.integers(0, 24 * 2 * 40)))
        cut = info.cutoff(Tp)
        r = info.predispatch_for(Tp)
        if r is not None:
            assert r["issue_time"] <= cut and r["ts"] == Tp
        assert (info.stacks_for(Tp)["avail_time"] <= cut).all()
        assert ((info.clearing_for(Tp)["ts"] + pd.Timedelta(minutes=30)) <= cut).all()
        k = info.predispatch_known_at(cut)
        assert (k["issue_time"] <= cut).all()
    info.assert_no_leak()


def test_missing_data_errors():
    info = InfoSet(Config())
    with pytest.raises(ValueError):
        info.stacks_known_at(T("2026-07-01"))
    with pytest.raises(ValueError):
        info.clearing_known_at(T("2026-07-01"))
    with pytest.raises(ValueError):
        info.predispatch_for(T("2026-07-01"))
