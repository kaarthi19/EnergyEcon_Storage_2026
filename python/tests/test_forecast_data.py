"""Forecast pipeline on EMA's data (run with BESSBID_DATA_DIR; skipped otherwise)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from perturb import frames_equal, perturb_clearing, perturb_predispatch, perturb_stacks

from bessbid import io
from bessbid.calendar import learn_calendar, outage_states
from bessbid.config import Config
from bessbid.forecast import KEYS, Forecaster, evaluate, walk_forward
from bessbid.infoset import InfoSet

pytestmark = pytest.mark.data
T = pd.Timestamp


@pytest.fixture(scope="module")
def ds(data_dir, data_work_dir):
    return io.load_all(data_dir, data_work_dir)


@pytest.fixture(scope="module")
def info(ds):
    return InfoSet.from_dataset(ds, Config())


@pytest.mark.parametrize("cut", ["2026-07-28", "2026-08-31", "2026-10-31 22:55"])
def test_calendar_real(ds, info, cut):
    cal = learn_calendar(info, cut)
    assert [(c.period_days, c.duration_periods) for c in cal.cycles] == [(11, 33), (19, 120)]
    assert [c.anchor.strftime("%H:%M") for c in cal.cycles] == ["15:00", "12:00"]
    w = cal.contingency
    assert (w.cycle, w.offset_periods, w.duration_periods, w.value, w.base) == ("cycle19d", 24, 72, 550.0, 480.0)
    assert (cal.surge.cycle, cal.surge.offset_periods, cal.surge.duration_periods) == ("cycle19d", 24, 72)
    assert 600 < cal.surge.value < 750
    # every training and November known-outage state above 400 MW is explained, 0 exceptions
    assert len(cal.exceptions(outage_states(ds.predispatch_train))) == 0
    assert len(cal.exceptions(outage_states(ds.predispatch_test))) == 0
    c = ds.clearing
    agree = (cal.contingency_req(c["ts"].to_numpy()) == c["contingency_requirement_mw"].to_numpy()).mean()
    assert agree > 0.999
    ev = cal.events_table("2026-11-01", "2026-11-30")
    on19 = ev.loc[ev["cycle"] == "cycle19d", "onset"].tolist()
    assert T("2026-11-18 12:00") in on19
    assert ev.loc[ev["cycle"] == "cycle11d", "onset"].dt.day.tolist() == [3, 14, 25]


def test_walk_forward_october(info):
    wf = walk_forward(info, None, "2026-10-01", "2026-10-31 23:30")
    ev = evaluate(wf).set_index(["kind", "product"])
    assert ev.loc[("lead2", "E"), "MAE"] < 23  # October holds a scarcity event
    assert ev.loc[("day_ahead", "E"), "MAE"] < 28
    assert ev.loc[("lead2", "R"), "MAE"] < 5
    assert ev.loc[("lead2", "E"), "spike_recall"] > 0.9
    info.assert_no_leak()


@pytest.mark.parametrize("target", ["2026-09-21 16:00", "2026-10-12 17:30", "2026-10-28 09:00"])
def test_no_leak_property_real(ds, target):
    """Perturb every row not legal at the decision time: forecasts are bit-identical."""
    Tt = T(target)
    cfg = Config()
    base = InfoSet.from_dataset(ds, cfg)
    d = base.cutoff(Tt)
    cut = d.normalize() - pd.Timedelta(days=1)
    pert = InfoSet(
        cfg, perturb_clearing(ds.clearing, d), perturb_predispatch(ds.predispatch_train, d),
        perturb_predispatch(ds.predispatch_test, d), perturb_stacks(ds.stacks, d),
    )
    a = Forecaster(base).fit(cut, residual_start=cut - pd.Timedelta(days=12))
    b = Forecaster(pert).fit(cut, residual_start=cut - pd.Timedelta(days=12))
    cols = list(KEYS) + ["nd", "outage", "sP", "sC", "sR"]
    ra, rb = a.lead2(Tt), b.lead2(Tt)
    assert all(ra[c] == rb[c] for c in cols)
    day = (Tt + pd.Timedelta(days=1)).normalize()
    assert frames_equal(a.day_ahead(day, d), b.day_ahead(day, d), cols)
    assert np.array_equal(a.scenarios([Tt], kind="lead2").values, b.scenarios([Tt], kind="lead2").values)
    assert np.array_equal(a.scenarios(day=day, kind="day_ahead", decision_time=d).values,
                          b.scenarios(day=day, kind="day_ahead", decision_time=d).values)
    base.assert_no_leak()
    pert.assert_no_leak()


def test_november_outlook(info):
    f = Forecaster(info).fit("2026-10-31 00:00")
    nov19 = f.day_ahead("2026-11-19")
    nov7 = f.day_ahead("2026-11-07")
    assert nov19["surge"].all()
    assert nov19["E"].max() > 400 > nov7["E"].max()
    assert nov7["E"].mean() < nov19["E"].mean()
    l2 = f.lead2_many(pd.date_range("2026-11-19 10:00", "2026-11-19 20:00", freq="30min"))
    assert (l2["E"] > 400).sum() >= 5
    assert (l2["lead_hours"] == 2.0).all()
    assert (l2["issue_time"] <= l2.index - pd.Timedelta(minutes=65)).all()
    info.assert_no_leak()
