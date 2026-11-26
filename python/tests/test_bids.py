import datetime as dt

import numpy as np
import pandas as pd
import pytest

from bessbid.bids import (
    PRODUCTS,
    TABLE_COLUMNS,
    Band,
    BidError,
    energy_curve,
    energy_fixed,
    from_table,
    make_period_bid,
    offered_capacity,
    regulation_only,
    round_mw,
    round_price,
    to_table,
)
from bessbid.config import Config

D = dt.date(2026, 11, 1)
CFG = Config()


def test_round_price():
    assert round_price(12.345) == 12.35
    assert round_price(-12.345) == -12.35
    assert round_price(0.004) == 0.0
    assert round_price(3000) == 3000.0
    with pytest.raises(BidError):
        round_price(float("nan"))


def test_round_mw_toward_zero():
    assert round_mw(49.99) == 49.9
    assert round_mw(-49.99) == -49.9
    assert round_mw(50.0) == 50.0
    assert round_mw(0.3) == 0.3  # float noise must not drop a unit
    assert round_mw(0.7, 0.1) == 0.7
    assert round_mw(12.3456, 0.001) == 12.345
    assert round_mw(0.04) == 0.0
    assert round_mw(7.9, 1.0) == 7.0
    for x in np.linspace(-100, 100, 2001):
        r = round_mw(float(x))
        assert abs(r) <= abs(x) + 1e-12 and abs(abs(x) - abs(r)) < 0.1 + 1e-9


def test_regulation_only():
    pb = regulation_only(D, 1)
    assert pb.regulation == (Band(0.0, 100.0),)
    assert pb.energy == () and pb.primary == () and pb.contingency == ()
    assert offered_capacity(pb) == 100
    assert [r.product for r in pb.rows()] == list(PRODUCTS)
    assert pb.start == pd.Timestamp("2026-11-01 00:00")
    pb = regulation_only(pd.Timestamp("2026-11-02"), 48, mw=60, price=1.234)
    assert pb.regulation == (Band(1.23, 60.0),) and pb.date == dt.date(2026, 11, 2)
    assert regulation_only(D, 2, mw=0).regulation == ()
    with pytest.raises(BidError):
        regulation_only(D, 1, mw=100.5)
    with pytest.raises(BidError):
        regulation_only(D, 49)


def test_energy_fixed():
    d = energy_fixed(D, 30, 40)
    assert d.energy == (Band(CFG.discharge_floor_price, 40.0),)
    assert d.regulation == (Band(0.0, 60.0),)
    c = energy_fixed(D, 8, -33.33)
    assert c.energy == (Band(CFG.charge_cap_price, -33.3),)
    assert c.regulation == (Band(0.0, 66.7),)
    assert offered_capacity(c) == pytest.approx(100.0)
    assert c.charge_mw == pytest.approx(33.3) and c.discharge_mw == 0
    n = energy_fixed(D, 8, -50, regulation_mw=None)
    assert n.regulation == ()
    z = energy_fixed(D, 8, 0)
    assert z.energy == () and z.regulation == (Band(0.0, 100.0),)
    with pytest.raises(BidError):
        energy_fixed(D, 8, -50, regulation_mw=60)


def test_energy_curve_sorts_merges_and_checks():
    pb = energy_curve(D, 1, CFG, charge=[(50, 10), (40, 5), (50, 5)], discharge=[(300, 20), (250.004, 10)])
    prices = [b.price for b in pb.energy]
    assert prices == sorted(prices) == [40.0, 50.0, 250.0, 300.0]
    assert [b.mw for b in pb.energy] == [-5.0, -15.0, 10.0, 20.0]
    with pytest.raises(BidError, match="below discharge"):
        energy_curve(D, 1, CFG, charge=[(300, 10)], discharge=[(250, 10)])
    with pytest.raises(BidError, match="share price"):
        energy_curve(D, 1, CFG, charge=[(250, 10)], discharge=[(250, 10)])
    with pytest.raises(BidError, match="bands"):
        energy_curve(D, 1, CFG, discharge=[(100 + i, 1) for i in range(11)])
    with pytest.raises(BidError, match="outside"):
        energy_curve(D, 1, CFG, discharge=[(5000, 1)])
    with pytest.raises(BidError, match="offered"):
        energy_curve(D, 1, CFG, charge=[(10, 60)], discharge=[(300, 50)])


def test_as_rules_and_clip():
    with pytest.raises(BidError, match=">= 0"):
        make_period_bid(D, 1, CFG, primary=[(1, -5)])
    with pytest.raises(BidError, match="bands"):
        make_period_bid(D, 1, CFG, regulation=[(i, 1) for i in range(6)])
    pb = make_period_bid(D, 1, CFG, energy=[(0, 30)], primary=[(1, 10)], contingency=[(2, 20)],
                         regulation=[(3, 30), (4, 30)], clip_as=True)
    assert offered_capacity(pb) == pytest.approx(100.0)
    assert pb.regulation == (Band(3.0, 30.0), Band(4.0, 10.0))
    pb = make_period_bid(D, 1, CFG, energy=[(0, 95)], primary=[(1, 10)], contingency=[(2, 20)],
                         regulation=[(3, 33.33)], clip_as=True)
    assert pb.regulation == () and pb.contingency == () and pb.primary == (Band(1.0, 5.0),)
    assert offered_capacity(pb) <= 100


def test_capacity_rules():
    pb = make_period_bid(D, 1, Config(capacity_rule="max_side"),
                         energy=[(10, -40), (300, 40)], regulation=[(0, 60)])
    assert offered_capacity(pb, "max_side") == 100
    assert offered_capacity(pb, "strict") == 140
    with pytest.raises(BidError):
        make_period_bid(D, 1, CFG, energy=[(10, -40), (300, 40)], regulation=[(0, 60)])


def test_table_roundtrip():
    bids = [regulation_only(D, k) for k in range(1, 49)]
    bids[5] = energy_fixed(D, 6, -25)
    bids[7] = energy_curve(D, 8, CFG, charge=[(10, 5)], discharge=[(400, 5), (500, 5)], primary=[(2, 3)])
    t = to_table(reversed(bids))  # sorted on output
    assert list(t.columns) == TABLE_COLUMNS and len(t) == 48 * 4
    assert list(t["product"][:4]) == list(PRODUCTS)
    assert t["trading_date"].dtype == np.dtype("datetime64[ns]")
    r = t[(t.trading_period == 8) & (t["product"] == "Energy")].iloc[0]
    assert (r.price_1, r.quantity_1, r.price_3, r.quantity_3) == (10, -5, 500, 5)
    assert np.isnan(r.price_4)
    back = from_table(t)
    assert [(b.date, b.period, b.energy, b.primary, b.contingency, b.regulation) for b in back] == [
        (b.date, b.period, b.energy, b.primary, b.contingency, b.regulation) for b in bids
    ]
