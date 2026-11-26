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
    assert c.discharge_floor_price == 0.01 and c.as_floor_price == 0.01 and c.charge_cap_price == 3000.0
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


# ---- config/default.toml and strategy parameters ------------------------------------------

DEFAULT_TOML = __import__("pathlib").Path(__file__).resolve().parents[1] / "config" / "default.toml"


def test_default_toml_equals_defaults():
    import dataclasses
    import re

    from bessbid.config import load_strategy_options
    from bessbid.strategy_full import FullParams
    from bessbid.strategy_regulation import RegulationOnlyStrategy

    assert load_config(DEFAULT_TOML) == Config()
    opts = load_strategy_options(DEFAULT_TOML)
    assert set(opts) == {"full", "regulation"}
    assert FullParams.from_mapping(opts["full"]) == FullParams()
    assert opts["regulation"] == {}
    # every key is documented, set or (for "not set" defaults) commented out
    text = DEFAULT_TOML.read_text()
    keys = set(re.findall(r"^#?\s*([a-z_0-9]+)\s*=", text, re.M))
    for cls in (Config, FullParams):
        missing = {f.name for f in dataclasses.fields(cls)} - keys
        assert not missing, f"{cls.__name__} keys missing from default.toml: {missing}"
    assert {"mw", "price"} <= keys
    assert {f.name for f in dataclasses.fields(RegulationOnlyStrategy)} - {"name", "needs_data"} == {"mw", "price"}


def test_strategy_options(tmp_path):
    from bessbid.config import load_strategy_options
    from bessbid.strategy import StrategyError, get_strategy
    from bessbid.strategy_full import FullParams

    t = tmp_path / "s.toml"
    t.write_text('soc_margin_mwh = 3.0\n[strategy.full]\ncharge_cap_mw = 40\nlevels = [-50, 0, 50]\n'
                 'ramp_mw = 30\n[strategy.regulation]\nprice = 0.02\n')
    assert load_config(t).soc_margin_mwh == 3.0  # the strategy table is not a Config key
    opts = load_strategy_options(t)
    p = FullParams.from_mapping(opts["full"])
    assert p.charge_cap_mw == 40.0 and isinstance(p.charge_cap_mw, float)
    assert p.levels == (-50.0, 0.0, 50.0) and p.ramp_mw == 30.0
    assert p.discharge_cap_mw == FullParams().discharge_cap_mw
    s = get_strategy("full", options=opts["full"])
    assert s.params == p
    r = get_strategy("regulation", options=opts["regulation"])
    assert r.price == 0.02
    with pytest.raises(ValueError, match="unknown"):
        FullParams.from_mapping({"charge_cap": 40})
    with pytest.raises(StrategyError, match="bad options"):
        get_strategy("full", options={"refine": "sometimes"})
    with pytest.raises(StrategyError, match="bad options"):
        get_strategy("regulation", options={"colour": 1})
    assert load_strategy_options(None) == {}
    bad = tmp_path / "b.toml"
    bad.write_text("[strategy]\ncharge_cap_mw = 40\n")
    with pytest.raises(ValueError, match="sub-table"):
        load_strategy_options(bad)


def test_cli_rejects_unknown_strategy_table(tmp_path, capsys):
    from bessbid.cli import main

    t = tmp_path / "s.toml"
    t.write_text("[strategy.fulll]\ncharge_cap_mw = 40\n")
    rc = main(["generate", "--out", str(tmp_path / "o.xlsx"), "--work-dir", str(tmp_path / "w"),
               "--strategy", "regulation", "--template", str(tmp_path / "missing.xlsx"), "--config", str(t)])
    assert rc == 1
    assert "unknown strategies ['fulll']" in capsys.readouterr().err
