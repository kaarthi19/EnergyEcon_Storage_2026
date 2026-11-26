"""Fill EMA's submission template.

The template workbook is copied as is: the 'Instructions' and 'Assumptions' sheets, the
column widths, the header row, the freeze pane and the document properties are kept. On the
'Bid Submission' sheet the example rows are removed and our rows are written in the
template's own cell format:

* Trading Date: a datetime cell with the template's ``dd mmm yyyy`` number format;
* Trading Period: an integer; Product: the exact label;
* Price k / Quantity k: numbers, blank (empty) cells for unused bands;
* lastModifiedBy is set to "bessbid" (the template's value names a person);
* each row takes the cell styles of the template's example row for the same product, and
  the autofilter is extended to the last row.

Output is byte-for-byte deterministic: workbook created/modified times are set to
``cfg.fixed_timestamp`` and every zip entry gets the same fixed date.
"""

from __future__ import annotations

import copy
import datetime as dt
import os
import re
import zipfile
from pathlib import Path
from typing import Iterable

import pandas as pd

from .bids import PRODUCTS, TABLE_COLUMNS, PeriodBid, to_table
from .config import Config
from .validate import EXPECTED_HEADERS, SHEET_NAME, errors, format_report, validate_table

__all__ = ["write_submission", "read_back", "WriterError"]

N_COLS = len(EXPECTED_HEADERS)
DATE_FORMAT = "dd\\ mmm\\ yyyy"
NUMBER_FORMAT = "#,##0.00;[Red]\\(#,##0.00\\);\\-"


class WriterError(RuntimeError):
    pass


def _capture_styles(ws) -> dict[str, list]:
    """StyleArray per column, taken from the first template row of each product."""
    styles: dict[str, list] = {}
    for r in range(2, ws.max_row + 1):
        prod = ws.cell(row=r, column=3).value
        if prod in PRODUCTS and prod not in styles:
            styles[prod] = [copy.copy(ws.cell(row=r, column=c)._style) for c in range(1, N_COLS + 1)]
    return styles


def _fixed_dt(cfg: Config) -> dt.datetime:
    return dt.datetime.fromisoformat(cfg.fixed_timestamp)


def _normalize_zip(src: Path, dst: Path, when: dt.datetime) -> None:
    """Rewrite the xlsx zip with fixed entry dates and fixed core.xml timestamps."""
    stamp = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_time = (when.year, when.month, when.day, when.hour, when.minute, when.second)
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "docProps/core.xml":
                text = data.decode("utf-8")
                for tag in ("dcterms:created", "dcterms:modified"):
                    text = re.sub(rf"(<{tag}[^>]*>)[^<]*(</{tag}>)", rf"\g<1>{stamp}\g<2>", text)
                data = text.encode("utf-8")
            zi = zipfile.ZipInfo(info.filename, date_time=date_time)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o600 << 16
            zout.writestr(zi, data, compresslevel=6)


def write_submission(
    bids: Iterable[PeriodBid] | pd.DataFrame,
    template_path: str | Path,
    out_path: str | Path,
    cfg: Config | None = None,
    month: str | None = None,
    validate: bool = True,
) -> Path:
    """Write ``bids`` (PeriodBids or a canonical table) into a copy of the template.

    With ``validate=True`` (default) the table is checked first and any error-level
    violation raises :class:`WriterError`, so an invalid file is never written.
    """
    import openpyxl

    cfg = cfg or Config()
    table = bids if isinstance(bids, pd.DataFrame) else to_table(bids)
    table = table[TABLE_COLUMNS]
    if validate:
        errs = errors(validate_table(table, cfg, month))
        if errs:
            raise WriterError("bid table fails validation:\n" + format_report(errs))

    template_path, out_path = Path(template_path), Path(out_path)
    if template_path.resolve() == out_path.resolve():
        raise WriterError("refusing to overwrite the template")
    wb = openpyxl.load_workbook(template_path)
    if SHEET_NAME not in wb.sheetnames:
        raise WriterError(f"template has no {SHEET_NAME!r} sheet")
    ws = wb[SHEET_NAME]
    header = [ws.cell(row=1, column=c).value for c in range(1, N_COLS + 1)]
    if header != EXPECTED_HEADERS:
        raise WriterError(f"template header differs from the expected layout: {header}")
    styles = _capture_styles(ws)
    height = ws.row_dimensions[2].height if 2 in ws.row_dimensions else None

    # Clear the example rows (values and formats) before writing.
    if ws.max_row >= 2:
        ws.delete_rows(2, ws.max_row - 1)
    for r in list(ws.row_dimensions.keys()):
        if r >= 2:
            del ws.row_dimensions[r]

    recs = table.to_dict("records")
    for i, rec in enumerate(recs):
        r = i + 2
        prod = rec["product"]
        vals = [
            pd.Timestamp(rec["trading_date"]).to_pydatetime().replace(tzinfo=None),
            int(rec["trading_period"]),
            str(prod),
        ]
        for k in TABLE_COLUMNS[3:]:
            v = rec[k]
            vals.append(None if pd.isna(v) else float(v))
        st = styles.get(prod)
        for c, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=c)
            if v is not None:
                cell.value = v
            if st is not None:
                cell._style = copy.copy(st[c - 1])
            elif c == 1:
                cell.number_format = DATE_FORMAT
            elif c >= 4:
                cell.number_format = NUMBER_FORMAT
        if height is not None:
            ws.row_dimensions[r].height = height
    ws.auto_filter.ref = f"A1:{openpyxl.utils.get_column_letter(N_COLS)}{len(recs) + 1}"

    when = _fixed_dt(cfg)
    wb.properties.created = when
    wb.properties.modified = when
    wb.properties.lastModifiedBy = "bessbid"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(f".{out_path.name}.tmp{os.getpid()}")
    tmp2 = out_path.with_name(f".{out_path.name}.tmp2{os.getpid()}")
    try:
        wb.save(tmp)
        _normalize_zip(tmp, tmp2, when)
        os.replace(tmp2, out_path)
    finally:
        for p in (tmp, tmp2):
            if p.exists():
                p.unlink()
    return out_path


def read_back(path: str | Path) -> pd.DataFrame:
    """Read the 'Bid Submission' sheet back into a canonical bid table."""
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[SHEET_NAME]
    recs = []
    for vals in ws.iter_rows(min_row=2, max_col=N_COLS, values_only=True):
        if all(v is None for v in vals):
            continue
        rec = [pd.Timestamp(vals[0]), vals[1], vals[2]]
        rec += [float("nan") if v is None else float(v) for v in vals[3:]]
        recs.append(rec)
    wb.close()
    df = pd.DataFrame(recs, columns=TABLE_COLUMNS)
    df["trading_date"] = pd.to_datetime(df["trading_date"]).astype("datetime64[ns]")
    df["trading_period"] = df["trading_period"].astype("int64")
    for c in TABLE_COLUMNS[3:]:
        df[c] = df[c].astype("float64")
    return df
