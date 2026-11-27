"""Event-window bias correction on EMA's data (run with BESSBID_DATA_DIR; skipped otherwise)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from perturb import perturb_clearing, perturb_predispatch, perturb_stacks

from bessbid import io
from bessbid.config import Config
from bessbid.eventbias import event_keys, fit_event_bias
from bessbid.forecast import Forecaster
from bessbid.infoset import InfoSet

pytestmark = pytest.mark.data
CUT = pd.Timestamp("2026-10-31")
CFG = Config()


@pytest.fixture(scope="module")
def ds(data_dir, data_work_dir):
    return io.load_all(data_dir, data_work_dir)


def _fit(info, cut=CUT):
    fc = Forecaster(info, CFG, nd_history_cap=True).fit(cut)
    return fc, fit_event_bias(fc, cut, nodes=fc.nd.nodes_da)


@pytest.fixture(scope="module")
def november(ds):
    info = InfoSet.from_dataset(ds, CFG)
    fc, eb = _fit(info)
    info.assert_no_leak()
    return info, fc, eb


def test_november_correction_learned_from_july_to_october(november):
    info, fc, eb = november
    t = eb.table
    e = [x for x in info.audit if x.accessor == "eventbias.fit"]
    assert len(e) == 1 and e[0].decision_time == CUT and e[0].max_time_used < CUT
    # the surge nights are corrected upwards, the 11-day windows and the onset afternoon down
    for h in ("h00", "h03", "h06"):
        assert t.loc[f"cycle19d:d1:s:{h}", "delta"] > 20 and t.loc[f"cycle19d:d2:s:{h}", "delta"] > 20
    assert (t.loc[[k for k in t.index if k.startswith("cycle11d")], "delta"] < 0).all()
    assert t.loc["cycle19d:d0:n:h12", "delta"] < 0
    # July to October: five 19-day events give the X+1 surge-night residuals
    assert t.loc["cycle19d:d1:s:h03", "days"] == 5
    Ts = pd.date_range("2026-11-01", "2026-11-30 23:30", freq="30min")
    d = eb.delta(fc.calendar, Ts)
    ev = np.asarray(fc.calendar.in_event(Ts.to_numpy(dtype="datetime64[ns]")), bool)
    assert not d[~ev].any() and np.count_nonzero(d) == ev.sum() == 267  # no overlaps in November
    assert set(event_keys(fc.calendar, Ts)) - {""} <= set(t.index)


def test_august_correction_uses_july_only(ds):
    cut = pd.Timestamp("2026-07-31")
    info = InfoSet.from_dataset(ds, CFG)
    _, eb = _fit(info, cut)
    info2 = InfoSet(CFG, perturb_clearing(ds.clearing, cut), perturb_predispatch(ds.predispatch_train, cut),
                    perturb_predispatch(ds.predispatch_test, cut), perturb_stacks(ds.stacks, cut))
    _, eb2 = _fit(info2, cut)
    pd.testing.assert_frame_equal(eb.table, eb2.table)
    assert eb.table["days"].max() <= 2  # two July events of each cycle at most
