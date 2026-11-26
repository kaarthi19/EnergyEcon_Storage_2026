
import numpy as np
import pandas as pd
import pytest

from bessbid.bids import energy_curve, energy_fixed, regulation_only, to_table
from bessbid.config import Config
from bessbid.periods import month_slots
from bessbid.validate import errors, format_report, validate_table

CFG = Config()
MONTH = "2026-11"


def month_table(month=MONTH):
    bids = []
    for i, s in enumerate(month_slots(month)):
        if i % 48 == 6:
            bids.append(energy_fixed(s.date, s.period, -40.0))
        elif i % 48 == 30:
            bids.append(energy_curve(s.date, s.period, CFG, discharge=[(0, 30), (300, 20)], regulation=[(0, 50)]))
        else:
            bids.append(regulation_only(s.date, s.period))
    return to_table(bids)


@pytest.fixture(scope="module")
def good():
    return month_table()


def codes(vs):
    return {v.code for v in vs}


def test_valid_month(good):
    vs = validate_table(good, CFG, MONTH)
    assert vs == [], format_report(vs)
    assert len(good) == 30 * 48 * 4
    assert validate_table(good, CFG) == []  # layout derived from the dates
    assert format_report([]) == "OK: no violations"


def _mut(good, row, **kv):
    t = good.copy()
    for k, v in kv.items():
        t[k] = t[k].astype(object)
        t.loc[row, k] = v
    return t


@pytest.mark.parametrize(
    "row,changes,code",
    [
        (3, {"price_6": 1.0, "quantity_6": 1.0}, "as_band_6_10"),
        (3, {"price_2": 5.0}, "band_pair"),
        (3, {"quantity_1": -5.0}, "as_sign"),
        (3, {"price_1": 0.001}, "cents"),
        (3, {"price_1": 9999.0}, "price_bounds"),
        (3, {"quantity_1": 100.5}, "product_capacity"),
        (3, {"quantity_1": np.inf}, "not_number"),
        (0, {"price_1": 100.0, "quantity_1": 10.0, "price_3": 120.0, "quantity_3": 5.0}, "band_gap"),
        (0, {"price_1": 100.0, "quantity_1": 10.0, "price_2": 90.0, "quantity_2": 5.0}, "ascending"),
        (0, {"price_1": 100.0, "quantity_1": 10.0, "price_2": 100.0, "quantity_2": 5.0}, "ascending"),
        (0, {"price_1": 100.0, "quantity_1": 5.0, "price_2": 200.0, "quantity_2": -5.0}, "charge_below_discharge"),
        (0, {"price_1": 100.0, "quantity_1": 5.0}, "capacity"),
        (0, {"price_1": 100.0, "quantity_1": 0.0}, "zero_quantity"),
        (0, {"price_1": 100.0, "quantity_1": 0.05}, "mw_resolution"),
        (0, {"product": "Primary"}, "product"),
        (0, {"trading_period": 49}, "period"),
        (0, {"trading_period": 1.5}, "period"),
        (0, {"trading_date": pd.Timestamp("2026-11-01 12:00")}, "date"),
        (0, {"trading_date": pd.NaT}, "date"),
    ],
)
def test_mutations(good, row, changes, code):
    vs = validate_table(_mut(good, row, **changes), CFG, MONTH)
    assert code in codes(vs), format_report(vs)


def test_warnings_are_not_errors(good):
    vs = validate_table(_mut(good, 3, quantity_1=99.95), CFG, MONTH)
    assert codes(vs) == {"mw_resolution"} and errors(vs) == []


def test_row_count_and_order(good):
    vs = validate_table(good.iloc[:-4], CFG, MONTH)
    assert "row_count" in codes(vs)
    swapped = good.copy()
    swapped.iloc[[0, 1]] = swapped.iloc[[1, 0]].to_numpy()
    assert "order" in codes(validate_table(swapped, CFG, MONTH))
    assert "order" in codes(validate_table(swapped, CFG))
    dup = pd.concat([good.iloc[:4], good.iloc[:4]], ignore_index=True)
    assert "duplicate" in codes(validate_table(dup, CFG))
    assert "row_count" in codes(validate_table(month_table("2026-12"), CFG, MONTH))
    gap = good[good["trading_date"] != pd.Timestamp("2026-11-02")].reset_index(drop=True)
    assert "dates" in codes(validate_table(gap, CFG))


def test_capacity_rule_modes(good):
    t = _mut(good, 0, price_1=10.0, quantity_1=-40.0, price_2=300.0, quantity_2=40.0)
    t.loc[3, "quantity_1"] = 60.0
    assert "capacity" in codes(validate_table(t, CFG, MONTH))
    assert "capacity" not in codes(validate_table(t, Config(capacity_rule="max_side"), MONTH))


def test_missing_columns():
    vs = validate_table(pd.DataFrame({"x": [1]}), CFG)
    assert codes(vs) == {"columns"}
