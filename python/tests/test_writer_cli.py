import datetime as dt
import hashlib
import json
import zipfile

import numpy as np
import openpyxl
import pandas as pd
import pytest

from bessbid import cli, io
from bessbid.bids import energy_curve, energy_fixed, regulation_only, to_table
from bessbid.config import Config
from bessbid.periods import month_slots
from bessbid.strategy import StrategyError, check_complete, get_strategy, register_strategy
from bessbid.strategy_regulation import RegulationOnlyStrategy
from bessbid.validate import errors, format_report, read_workbook_table, validate_xlsx
from bessbid.writer import WriterError, read_back, write_submission

CFG = Config()
MONTH = "2026-11"


def mixed_bids():
    out = []
    for i, s in enumerate(month_slots(MONTH)):
        if i % 48 == 7:
            out.append(energy_fixed(s.date, s.period, -45.5))
        elif i % 48 == 35:
            out.append(energy_curve(s.date, s.period, CFG, discharge=[(0, 20), (450.5, 10)],
                                    regulation=[(0, 60), (5, 10)]))
        else:
            out.append(regulation_only(s.date, s.period))
    return out


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_write_read_back_roundtrip(template, tmp_path):
    bids = mixed_bids()
    out = write_submission(bids, template, tmp_path / "out.xlsx", CFG, MONTH)
    table = to_table(bids)
    back = read_back(out)
    pd.testing.assert_frame_equal(back, table)
    vs = validate_xlsx(out, CFG, MONTH)
    assert vs == [], format_report(vs)
    # the validator's own reader agrees with the writer's
    t2, v2 = read_workbook_table(out)
    assert v2 == []
    pd.testing.assert_frame_equal(t2.astype(back.dtypes.to_dict()), back)


def test_layout_matches_template(template, tmp_path):
    out = write_submission(mixed_bids(), template, tmp_path / "out.xlsx", CFG, MONTH)
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == ["Instructions", "Bid Submission", "Assumptions"]
    assert wb["Instructions"]["B2"].value == "Synthetic template for tests."
    assert wb["Assumptions"]["B3"].value == 100
    ws = wb["Bid Submission"]
    assert ws.max_row == 5761 and ws.freeze_panes == "D2" and ws.auto_filter.ref == "A1:W5761"
    a2 = ws["A2"]
    assert isinstance(a2.value, dt.datetime) and a2.number_format == "dd\\ mmm\\ yyyy"
    assert ws["B2"].value == 1 and ws["C2"].value == "Energy"
    # example values from the template are gone: period 1 Energy is blank
    assert all(ws.cell(row=2, column=c).value is None for c in range(4, 24))
    assert ws["C5"].value == "Regulation" and ws["D5"].value == 0.01 and ws["E5"].value == 100
    assert ws["D5"].number_format == "#,##0.00;[Red]\\(#,##0.00\\);\\-"
    assert ws["N3"].fill.fgColor.rgb == "FFF1F3F5"  # AS bands 6-10 keep the grey fill
    assert ws.row_dimensions[100].height == 23.1
    assert wb.properties.lastModifiedBy == "bessbid"


def test_deterministic_bytes(template, tmp_path):
    bids = mixed_bids()
    a = write_submission(bids, template, tmp_path / "a.xlsx", CFG, MONTH)
    b = write_submission(bids, template, tmp_path / "b.xlsx", CFG, MONTH)
    assert sha(a) == sha(b)
    with zipfile.ZipFile(a) as z:
        assert {i.date_time for i in z.infolist()} == {(2026, 11, 1, 0, 0, 0)}
        core = z.read("docProps/core.xml").decode()
        assert "2026-11-01T00:00:00Z" in core


def test_writer_refuses_invalid(template, tmp_path):
    t = to_table(mixed_bids())
    t.loc[3, "quantity_1"] = 150.0
    with pytest.raises(WriterError, match="validation"):
        write_submission(t, template, tmp_path / "bad.xlsx", CFG, MONTH)
    assert not (tmp_path / "bad.xlsx").exists()
    with pytest.raises(WriterError, match="overwrite"):
        write_submission(mixed_bids(), template, template, CFG, MONTH)


def test_validate_xlsx_catches_cell_problems(template, tmp_path):
    out = write_submission(mixed_bids(), template, tmp_path / "out.xlsx", CFG, MONTH)
    wb = openpyxl.load_workbook(out)
    ws = wb["Bid Submission"]
    ws["A2"] = "01 Nov 2026"
    ws["D5"] = "0"
    ws["B6"] = "2"
    ws["X10"] = 1
    ws["A1"] = "Date"
    wb.save(tmp_path / "bad.xlsx")
    vs = validate_xlsx(tmp_path / "bad.xlsx", CFG, MONTH)
    got = {v.code for v in vs}
    assert {"header"} <= got
    ws["A1"] = "Trading Date"
    wb.save(tmp_path / "bad.xlsx")
    got = {v.code for v in validate_xlsx(tmp_path / "bad.xlsx", CFG, MONTH)}
    assert {"date_cell", "number_cell", "period_cell", "extra_cells"} <= got
    del wb["Bid Submission"]
    wb.save(tmp_path / "nosheet.xlsx")
    assert {v.code for v in validate_xlsx(tmp_path / "nosheet.xlsx", CFG, MONTH)} == {"sheets"}


def test_validate_xlsx_blank_row(template, tmp_path):
    out = write_submission(mixed_bids(), template, tmp_path / "out.xlsx", CFG, MONTH)
    wb = openpyxl.load_workbook(out)
    ws = wb["Bid Submission"]
    for c in range(1, 24):
        ws.cell(row=10, column=c).value = None
    wb.save(tmp_path / "gap.xlsx")
    got = {v.code for v in validate_xlsx(tmp_path / "gap.xlsx", CFG, MONTH)}
    assert "blank_row" in got and "row_count" in got


def test_strategy_registry():
    s = get_strategy("regulation")
    assert isinstance(s, RegulationOnlyStrategy) and not s.needs_data
    bids = s.generate(MONTH, None, CFG)
    check_complete(bids, MONTH)
    assert len(bids) == 1440 and all(b.regulation[0].mw == 100 for b in bids)
    with pytest.raises(StrategyError):
        check_complete(bids[:-1], MONTH)
    with pytest.raises(StrategyError):
        check_complete(bids + bids[:1], MONTH)
    with pytest.raises(StrategyError, match="unknown"):
        get_strategy("nope")
    register_strategy("half", lambda: RegulationOnlyStrategy(name="half", mw=50))
    assert get_strategy("half").generate(MONTH, None, CFG)[0].regulation[0].mw == 50
    register_strategy("broken", "bessbid.no_such_module:X")
    with pytest.raises(StrategyError, match="not available"):
        get_strategy("broken")


def test_cli_generate_regulation(template, tmp_path, capsys):
    out = tmp_path / "out" / "sub.xlsx"
    work = tmp_path / "work"
    rc = cli.main(["generate", "--template", str(template), "--out", str(out), "--work-dir", str(work),
                   "--strategy", "regulation", "--month", MONTH])
    assert rc == 0, capsys.readouterr()
    man = json.loads((work / "sub.manifest.json").read_text())
    assert man["rows"] == 5760 and man["validation"]["errors"] == 0 and man["roundtrip_ok"]
    assert man["output_sha256"] == sha(out)
    assert man["offered_mw"]["max_period_offer_mw"] == 100
    back = read_back(out)
    assert (back.loc[back["product"] == "Regulation", "quantity_1"] == 100).all()
    assert back.loc[back["product"] != "Regulation", "quantity_1"].isna().all()
    assert cli.main(["validate", str(out), "--month", MONTH]) == 0
    assert cli.main(["validate", str(out), "--month", "2026-12"]) == 2
    # a second run gives the same bytes
    out2 = tmp_path / "out" / "sub2.xlsx"
    cli.main(["generate", "--template", str(template), "--out", str(out2), "--work-dir", str(work),
              "--strategy", "regulation"])
    assert sha(out) == sha(out2)


def test_cli_errors(template, tmp_path, capsys):
    rc = cli.main(["generate", "--out", str(tmp_path / "x.xlsx"), "--work-dir", str(tmp_path)])
    assert rc == 1
    register_strategy("needsdata", lambda: RegulationOnlyStrategy(name="needsdata", needs_data=True))
    # 'full' may not exist yet; either way the CLI must fail cleanly or succeed, never crash
    data = tmp_path / "data"
    data.mkdir()
    rc = cli.main(["generate", "--data-dir", str(data), "--out", str(data / "x.xlsx"), "--work-dir", str(tmp_path / "w")])
    assert rc == 1  # output inside the data dir is refused
    rc = cli.main(["generate", "--data-dir", str(data), "--out", str(tmp_path / "x.xlsx"),
                   "--work-dir", str(tmp_path / "w")])
    assert rc == 1  # no template in the data dir
    capsys.readouterr()


class _NeedsData(RegulationOnlyStrategy):
    pass


class _Broken:
    name = "broken_planner"
    needs_data = True

    def generate(self, month, infoset, cfg):
        raise RuntimeError("no feasible plan")


@pytest.fixture()
def synth_data(tmp_path, template):
    from synth import write_dataset

    root = write_dataset(tmp_path / "data", with_test=False)
    import shutil

    shutil.copy(template, root / "BESS_Competition_Template.xlsx")
    return root


def test_cli_fails_when_test_predispatch_is_missing(synth_data, tmp_path, capsys):
    """A data strategy must not run silently without the test-month pre-dispatch file."""
    register_strategy("needsdata2", lambda: _NeedsData(name="needsdata2", needs_data=True))
    work = tmp_path / "w"
    rc = cli.main(["generate", "--data-dir", str(synth_data), "--out", str(tmp_path / "o.xlsx"),
                   "--work-dir", str(work), "--strategy", "needsdata2"])
    err = capsys.readouterr().err
    assert rc == 1 and "error" in err and "pre-dispatch" in err and not (tmp_path / "o.xlsx").exists()
    rc = cli.main(["generate", "--data-dir", str(synth_data), "--out", str(tmp_path / "o2.xlsx"),
                   "--work-dir", str(work), "--strategy", "needsdata2", "--allow-incomplete-inputs"])
    err = capsys.readouterr().err
    assert rc == 0 and "WARNING" in err and "pre-dispatch" in err
    man = json.loads((work / "o2.manifest.json").read_text())
    cov = man["inputs"]["predispatch_test_coverage"]
    assert man["input_warnings"] and cov["covered"] == 0 and not cov["file"] and cov["first_missing"]


def test_cli_fails_when_test_predispatch_is_partial(tmp_path, template, capsys):
    """The synthetic test file covers one day only: generate for the month must stop."""
    from synth import write_dataset
    import shutil

    root = write_dataset(tmp_path / "data", with_test=True)
    shutil.copy(template, root / "BESS_Competition_Template.xlsx")
    register_strategy("needsdata3", lambda: _NeedsData(name="needsdata3", needs_data=True))
    rc = cli.main(["generate", "--data-dir", str(root), "--out", str(tmp_path / "o.xlsx"),
                   "--work-dir", str(tmp_path / "w"), "--strategy", "needsdata3", "--month", "2026-07"])
    err = capsys.readouterr().err
    assert rc == 1 and "only 48 of 1488 periods" in err and "2026-07-01 00:00" in err


def test_predispatch_coverage_counts_only_legal_vintages(tmp_path):
    from types import SimpleNamespace

    from synth import predispatch_frame

    from bessbid.cli import predispatch_coverage
    from bessbid.config import Config

    def cov(frame):
        p = tmp_path / "november_pre_dispatch_forecasts.csv"
        frame.to_csv(p, index=False)
        ds = SimpleNamespace(predispatch_test=io.load_predispatch([p], kind="test"))
        return predispatch_coverage(ds, Config(), "2026-07")

    full = predispatch_frame("2026-07-01 00:00", 31 * 48, train=False)
    c = cov(full)
    assert c["covered"] == c["periods"] == 1488 and c["first_missing"] is None
    c = cov(full[full["period_sgt"] != full["period_sgt"].iloc[400]])  # one period dropped
    assert c["covered"] == 1487 and c["first_missing"] is not None
    c = cov(predispatch_frame("2026-07-01 00:00", 31 * 48, train=False, leads=(1.0, 0.5)))
    assert c["covered"] == 0  # lead 1 h / 0.5 h are issued after the gate


def test_cli_falls_back_to_regulation_when_planning_fails(synth_data, tmp_path, capsys):
    register_strategy("broken_planner", lambda: _Broken())
    work = tmp_path / "w"
    out = tmp_path / "o.xlsx"
    rc = cli.main(["generate", "--data-dir", str(synth_data), "--out", str(out), "--work-dir", str(work),
                   "--strategy", "broken_planner", "--allow-incomplete-inputs"])
    err = capsys.readouterr().err
    assert rc == 0 and "fallback" in err
    man = json.loads((work / "o.manifest.json").read_text())
    assert man["fallback"].startswith("RuntimeError") and man["strategy"] == "regulation"
    back = read_back(out)
    assert (back.loc[back["product"] == "Regulation", "quantity_1"] == 100).all()
