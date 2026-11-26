import datetime as dt
import json
import math

import pandas as pd
import pytest

from bessbid.config import Config, config_from_mapping, load_config
from bessbid.periods import month_days, month_slots, parse_month, period_of, period_start


def test_defaults():
    c = Config()
    assert c.power_mw == 100 and c.energy_mwh == 100
    assert c.soc_min_mwh == pytest.approx(5) and c.soc_max_mwh == pytest.approx(95)
    assert c.initial_soc_mwh == pytest.approx(50)
    assert c.capacity_rule == "strict" and c.gate_closure_min == 65 and c.stack_pub_rule == "column"
    assert c.discharge_floor_price == 0.0 and c.charge_cap_price > 2058.4
    convs = c.efficiency_conventions
    assert len(convs) == 3
    assert convs[0] == (1.0, 0.85) and convs[2] == (0.85, 1.0)
    assert convs[1][0] == pytest.approx(math.sqrt(0.85))
    for ec, ed in convs:
        assert ec * ed == pytest.approx(0.85)
    assert c.period_minutes == 30


def test_overrides_json_and_toml(tmp_path):
    j = tmp_path / "c.json"
    j.write_text(json.dumps({"soc_margin_mwh": 3.5, "bess": {"power_mw": 100.0}}))
    c = load_config(j)
    assert c.soc_margin_mwh == 3.5
    t = tmp_path / "c.toml"
    t.write_text('[rules]\ncapacity_rule = "max_side"\npredispatch_leads = [3.0]\n'
                 '[bess]\nefficiency_conventions = [[1.0, 0.85]]\n')
    c = load_config(t)
    assert c.capacity_rule == "max_side"
    assert c.predispatch_leads == (3.0,)
    assert c.efficiency_conventions == ((1.0, 0.85),)
    assert load_config(None) == Config()


def test_bad_config():
    with pytest.raises(ValueError, match="unknown config keys"):
        config_from_mapping({"soc_margn": 1})
    with pytest.raises(ValueError, match="capacity_rule"):
        Config(capacity_rule="loose")
    with pytest.raises(ValueError, match="stack_pub_rule"):
        Config(stack_pub_rule="never")
    with pytest.raises(ValueError, match="eta_c"):
        Config(efficiency_conventions=((0.9, 0.9),))
    with pytest.raises(ValueError, match="cents"):
        Config(charge_cap_price=3000.001)
    with pytest.raises(ValueError):
        config_from_mapping({"a": {"x": 1}, "b": {"x": 2}})


def test_to_dict_roundtrip():
    c = Config(soc_margin_mwh=4.0)
    assert config_from_mapping(c.to_dict()) == c


def test_periods():
    assert parse_month("2026-11") == (2026, 11)
    with pytest.raises(ValueError):
        parse_month("Nov 2026")
    assert len(month_days("2026-11")) == 30 and len(month_days("2026-12")) == 31
    slots = month_slots("2026-11")
    assert len(slots) == 1440
    assert slots[0].date == dt.date(2026, 11, 1) and slots[0].period == 1
    assert slots[0].start == pd.Timestamp("2026-11-01 00:00")
    assert slots[-1].start == pd.Timestamp("2026-11-30 23:30") and slots[-1].period == 48
    assert slots[-1].end == pd.Timestamp("2026-12-01 00:00")
    assert period_start(dt.date(2026, 11, 3), 33) == pd.Timestamp("2026-11-03 16:00")
    assert period_of(pd.Timestamp("2026-11-03 16:00")) == 33
    with pytest.raises(ValueError):
        period_of(pd.Timestamp("2026-11-03 16:10"))
    with pytest.raises(ValueError):
        period_start(dt.date(2026, 11, 3), 49)
