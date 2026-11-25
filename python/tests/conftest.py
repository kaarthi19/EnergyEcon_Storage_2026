"""Shared fixtures. Unit tests use tiny synthetic frames and a synthetic copy of the
template layout; tests marked ``data`` run only when BESSBID_DATA_DIR points at EMA's data."""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bessbid.validate import EXPECTED_HEADERS  # noqa: E402

DATA_DIR = os.environ.get("BESSBID_DATA_DIR")


def pytest_configure(config):
    config.addinivalue_line("markers", "data: needs EMA's data (set BESSBID_DATA_DIR)")


def pytest_collection_modifyitems(config, items):
    if DATA_DIR and Path(DATA_DIR).is_dir():
        return
    skip = pytest.mark.skip(reason="BESSBID_DATA_DIR not set")
    for item in items:
        if "data" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return Path(DATA_DIR)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def data_work_dir(tmp_path_factory) -> Path:
    """Cache dir for data tests: BESSBID_WORK_DIR if set (reuses caches), else a temp dir."""
    w = os.environ.get("BESSBID_WORK_DIR")
    return Path(w) if w else tmp_path_factory.mktemp("work")


def make_template(path: Path) -> Path:
    """A workbook with the same layout as EMA's template (sheets, header, example rows,
    formats, fills, freeze pane, autofilter)."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill

    wb = openpyxl.Workbook()
    ins = wb.active
    ins.title = "Instructions"
    ins["A1"], ins["B1"] = "Item", "Guidance"
    ins["A2"], ins["B2"] = "Purpose", "Synthetic template for tests."
    ws = wb.create_sheet("Bid Submission")
    for c, h in enumerate(EXPECTED_HEADERS, start=1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.font = Font(b=True, color="FFFFFFFF")
        cell.fill = PatternFill("solid", fgColor="FF17365D")
    ex = [
        ("Energy", [100, 20, 150, 25, 220, 25]),
        ("Primary Reserve", [10, 5, 20, 5]),
        ("Contingency Reserve", [15, 5, 25, 5]),
        ("Regulation", [20, 5, 35, 5]),
    ]
    numfmt = "#,##0.00;[Red]\\(#,##0.00\\);\\-"
    r = 2
    for period in (1, 2):
        for prod, vals in ex:
            ws.cell(row=r, column=1, value=dt.datetime(2026, 11, 1)).number_format = "dd\\ mmm\\ yyyy"
            ws.cell(row=r, column=1).fill = PatternFill("solid", fgColor="FFEAF2F8")
            ws.cell(row=r, column=2, value=period)
            ws.cell(row=r, column=3, value=prod)
            for c in range(4, 24):
                cell = ws.cell(row=r, column=c)
                cell.number_format = numfmt
                if prod != "Energy" and c >= 14:
                    cell.fill = PatternFill("solid", fgColor="FFF1F3F5")
            if period == 1:
                for i, v in enumerate(vals):
                    ws.cell(row=r, column=4 + i, value=v)
            ws.row_dimensions[r].height = 23.1
            r += 1
    ws.freeze_panes = "D2"
    ws.auto_filter.ref = "A1:W9"
    a = wb.create_sheet("Assumptions")
    a["A1"] = "Illustrative BESS assumptions"
    a["A3"], a["B3"] = "Rated power", 100
    wb.save(path)
    return path


@pytest.fixture
def template(tmp_path) -> Path:
    return make_template(tmp_path / "BESS_Competition_Template.xlsx")
