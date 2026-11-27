"""Independent validator for a bid table or a submission workbook.

It does not reuse the builders' or the writer's logic: it re-reads the workbook with its own
code and checks every template rule:

* sheet and header layout; Trading Date is a real date cell, Trading Period an integer
  1..48, Product one of the four exact labels;
* row count = days x 48 x 4, ordered by date, period and Energy / Primary Reserve /
  Contingency Reserve / Regulation;
* each band has both price and quantity or neither, no gaps between bands, no NaN/inf,
  prices in whole cents and within bounds, strictly ascending prices;
* AS rows: bands 6-10 blank, positive quantities; Energy: charge (negative MW) prices below
  discharge (positive MW) prices;
* the capacity rule per period (strict: sum |energy MW| + sum AS MW <= 100).

Returns a list of :class:`Violation`. Severity ``error`` means the file must not be
submitted; ``warning`` flags our own conventions (e.g. MW resolution).
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from .config import Config
from .periods import PERIODS_PER_DAY, month_days

__all__ = [
    "Violation",
    "EXPECTED_HEADERS",
    "SHEET_NAME",
    "PRODUCT_LABELS",
    "validate_table",
    "validate_xlsx",
    "read_workbook_table",
    "errors",
    "format_report",
]

SHEET_NAME = "Bid Submission"
PRODUCT_LABELS = ("Energy", "Primary Reserve", "Contingency Reserve", "Regulation")
N_BANDS = 10
N_AS_BANDS = 5
EXPECTED_HEADERS = ["Trading Date", "Trading Period", "Product"] + [
    h for i in range(1, N_BANDS + 1) for h in (f"Price {i} ($/MWh)", f"Quantity {i} (MW)")
]
_COLS = ["trading_date", "trading_period", "product"] + [
    f"{k}_{i}" for i in range(1, N_BANDS + 1) for k in ("price", "quantity")
]


@dataclass(frozen=True)
class Violation:
    code: str
    message: str
    row: int | None = None  # worksheet row number (header = 1), when known
    severity: str = "error"

    def __str__(self) -> str:
        where = f"row {self.row}: " if self.row is not None else ""
        return f"[{self.severity}:{self.code}] {where}{self.message}"


def errors(vs: Iterable[Violation]) -> list[Violation]:
    return [v for v in vs if v.severity == "error"]


def format_report(vs: list[Violation], limit: int = 30) -> str:
    if not vs:
        return "OK: no violations"
    counts: dict[str, int] = defaultdict(int)
    for v in vs:
        counts[f"{v.severity}:{v.code}"] += 1
    lines = [f"{len(vs)} violations: " + ", ".join(f"{k} x{n}" for k, n in sorted(counts.items()))]
    lines += [str(v) for v in vs[:limit]]
    if len(vs) > limit:
        lines.append(f"... {len(vs) - limit} more")
    return "\n".join(lines)


def _num(x) -> float | None:
    if x is None:
        return None
    if isinstance(x, float) and math.isnan(x):
        return None
    if isinstance(x, (bool, np.bool_)):
        return float("nan")  # booleans are not numbers here
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def _is_cents(p: float) -> bool:
    return abs(p * 100 - round(p * 100)) < 1e-6


def _on_grid(q: float, res: float) -> bool:
    u = abs(q) / res
    return abs(u - round(u)) < 1e-6


def validate_table(df: pd.DataFrame, cfg: Config | None = None, month: str | None = None,
                   first_row: int = 2) -> list[Violation]:
    """Check a canonical bid table (see :func:`bessbid.bids.to_table`).

    ``first_row`` is the worksheet row number of the table's first row (2 below the header).
    """
    cfg = cfg or Config()
    vs: list[Violation] = []
    missing = [c for c in _COLS if c not in df.columns]
    if missing:
        return [Violation("columns", f"missing columns {missing}")]
    extra = [c for c in df.columns if c not in _COLS]
    if extra:
        vs.append(Violation("columns", f"unexpected columns {extra}", severity="warning"))

    n = len(df)
    # ---- expected sequence --------------------------------------------------------------
    if month is not None:
        days = month_days(month)
        exp_n = len(days) * PERIODS_PER_DAY * len(PRODUCT_LABELS)
        if n != exp_n:
            vs.append(Violation("row_count", f"{n} rows, expected {exp_n} ({len(days)} days x 48 x 4)"))
        expected = [(d, k, p) for d in days for k in range(1, PERIODS_PER_DAY + 1) for p in PRODUCT_LABELS]
    else:
        expected = None

    rows = df[_COLS].to_dict("records")
    keys: list[tuple | None] = []
    n_order = 0
    period_cap: dict[tuple, float] = defaultdict(float)
    period_side: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for i, r in enumerate(rows):
        rn = first_row + i
        # ---- keys ------------------------------------------------------------------------
        d = r["trading_date"]
        dd = None
        if d is None or (isinstance(d, float) and math.isnan(d)) or d is pd.NaT:
            vs.append(Violation("date", "missing Trading Date", rn))
        else:
            try:
                ts = pd.Timestamp(d)
                if pd.isna(ts):
                    raise ValueError
                if ts != ts.normalize():
                    vs.append(Violation("date", f"Trading Date has a time part: {ts}", rn))
                dd = ts.date()
            except (ValueError, TypeError):
                vs.append(Violation("date", f"Trading Date not a date: {d!r}", rn))
        per = _num(r["trading_period"])
        kk = None
        if per is None or not math.isfinite(per) or per != int(per) or not 1 <= per <= PERIODS_PER_DAY:
            vs.append(Violation("period", f"Trading Period must be an integer 1..48, got {r['trading_period']!r}", rn))
        else:
            kk = int(per)
        prod = r["product"]
        if prod not in PRODUCT_LABELS:
            vs.append(Violation("product", f"Product label {prod!r} not one of {PRODUCT_LABELS}", rn))
        key = (dd, kk, prod)
        keys.append(key)
        if expected is not None and i < len(expected) and key != expected[i]:
            n_order += 1
            if n_order <= 5:
                vs.append(Violation("order", f"found {key}, expected {expected[i]}", rn))

        # ---- bands -----------------------------------------------------------------------
        is_energy = prod == "Energy"
        filled: list[tuple[int, float, float]] = []
        blank_seen = False
        for b in range(1, N_BANDS + 1):
            p, q = _num(r[f"price_{b}"]), _num(r[f"quantity_{b}"])
            if p is None and q is None:
                blank_seen = True
                continue
            if not is_energy and b > N_AS_BANDS:
                vs.append(Violation("as_band_6_10", f"{prod}: band {b} must be blank", rn))
            if p is None or q is None:
                vs.append(Violation("band_pair", f"band {b} has only one of price/quantity", rn))
                continue
            if not (math.isfinite(p) and math.isfinite(q)):
                vs.append(Violation("not_number", f"band {b} price/quantity not a finite number", rn))
                continue
            if blank_seen:
                vs.append(Violation("band_gap", f"band {b} filled after a blank band", rn))
            filled.append((b, p, q))
        for b, p, q in filled:
            if not _is_cents(p):
                vs.append(Violation("cents", f"band {b} price {p} not in whole cents", rn))
            if not cfg.offer_price_min <= p <= cfg.offer_price_max:
                vs.append(Violation("price_bounds", f"band {b} price {p} outside [{cfg.offer_price_min}, {cfg.offer_price_max}]", rn))
            if q == 0:
                vs.append(Violation("zero_quantity", f"band {b} has 0 MW", rn, "warning"))
            if not is_energy and q < 0:
                vs.append(Violation("as_sign", f"{prod}: band {b} quantity {q} must be positive", rn))
            if not _on_grid(q, cfg.mw_resolution):
                vs.append(Violation("mw_resolution", f"band {b} quantity {q} not a multiple of {cfg.mw_resolution}", rn, "warning"))
        prices = [p for _, p, _ in filled]
        for a, c in zip(prices, prices[1:]):
            if c < a or (cfg.strict_ascending and c == a):
                vs.append(Violation("ascending", f"prices not ascending: {prices}", rn))
                break
        if is_energy:
            ch = [p for _, p, q in filled if q < 0]
            ds = [p for _, p, q in filled if q > 0]
            if ch and ds and max(ch) >= min(ds):
                vs.append(Violation("charge_below_discharge", f"charge prices {ch} not all below discharge prices {ds}", rn))
        tot_row = sum(abs(q) for _, _, q in filled)
        if tot_row > cfg.power_mw + 1e-6:
            vs.append(Violation("product_capacity", f"{prod} offers {tot_row:.3f} MW > {cfg.power_mw}", rn))
        if dd is not None and kk is not None:
            pk = (dd, kk)
            period_cap[pk] += tot_row
            side = period_side[pk]
            if is_energy:
                side[0] += sum(-q for _, _, q in filled if q < 0)
                side[1] += sum(q for _, _, q in filled if q > 0)
            else:
                side[2] += tot_row
                if prod == "Regulation":
                    side[3] += tot_row

    if n_order > 5:
        vs.append(Violation("order", f"{n_order} rows out of the expected order in total"))
    if expected is None:
        # Derive the expected layout from the dates present.
        dates = sorted({k[0] for k in keys if k and k[0] is not None})
        if dates:
            span = (dates[-1] - dates[0]).days + 1
            if span != len(dates):
                vs.append(Violation("dates", f"dates are not contiguous: {len(dates)} dates over {span} days"))
            exp = [(d, k, p) for d in dates for k in range(1, PERIODS_PER_DAY + 1) for p in PRODUCT_LABELS]
            if keys != exp:
                vs.append(Violation("order", "rows are not one per (date, period, product) in submission order"))
    # duplicates
    seen: dict[tuple, int] = {}
    for i, k in enumerate(keys):
        if k is None or None in k:
            continue
        if k in seen:
            vs.append(Violation("duplicate", f"duplicate row for {k} (first at row {seen[k]})", first_row + i))
        else:
            seen[k] = first_row + i
    # capacity rule
    for pk, tot in period_cap.items():
        ch, ds, asm, reg = period_side[pk]
        if cfg.capacity_rule == "strict":
            used = tot
        elif cfg.capacity_rule == "physical":
            used = max(ds + asm, ch + reg)
        else:
            used = max(ch, ds) + asm
        if used > cfg.power_mw + 1e-6:
            vs.append(Violation("capacity", f"{pk[0]} P{pk[1]}: offered {used:.3f} MW > {cfg.power_mw} MW ({cfg.capacity_rule} rule)"))
    return vs


# --------------------------------------------------------------------------------------
# Workbook
# --------------------------------------------------------------------------------------


def read_workbook_table(path: str | Path) -> tuple[pd.DataFrame, list[Violation]]:
    """Read the 'Bid Submission' sheet into a canonical table, checking cell types."""
    import openpyxl

    vs: list[Violation] = []
    wb = openpyxl.load_workbook(path, data_only=False)
    for name in ("Instructions", "Assumptions"):
        if name not in wb.sheetnames:
            vs.append(Violation("sheets", f"sheet {name!r} missing", severity="warning"))
    if SHEET_NAME not in wb.sheetnames:
        return pd.DataFrame(columns=_COLS), vs + [Violation("sheets", f"sheet {SHEET_NAME!r} missing")]
    ws = wb[SHEET_NAME]
    header = [ws.cell(row=1, column=c).value for c in range(1, len(EXPECTED_HEADERS) + 1)]
    if header != EXPECTED_HEADERS:
        vs.append(Violation("header", f"header row differs from the template: {header}", 1))
    if ws.max_column > len(EXPECTED_HEADERS):
        for row in ws.iter_rows(min_row=1, min_col=len(EXPECTED_HEADERS) + 1):
            for c in row:
                if c.value is not None:
                    vs.append(Violation("extra_cells", f"value beyond column W at {c.coordinate}", c.row))
                    break
    recs = []
    last_nonempty = 1
    for row in ws.iter_rows(min_row=2, max_col=len(EXPECTED_HEADERS)):
        vals = [c.value for c in row]
        rn = row[0].row
        if all(v is None for v in vals):
            recs.append(None)
            continue
        last_nonempty = rn
        a = row[0]
        if not isinstance(a.value, (dt.datetime, dt.date)):
            vs.append(Violation("date_cell", f"Trading Date cell is {type(a.value).__name__}, not a date", rn))
        elif "y" not in a.number_format.lower() or "d" not in a.number_format.lower():
            vs.append(Violation("date_format", f"Trading Date number format {a.number_format!r}", rn, "warning"))
        b = row[1].value
        if not isinstance(b, int) or isinstance(b, bool):
            if not (isinstance(b, float) and b == int(b)):
                vs.append(Violation("period_cell", f"Trading Period cell {b!r} is not an integer", rn))
        if not isinstance(row[2].value, str):
            vs.append(Violation("product_cell", f"Product cell {row[2].value!r} is not text", rn))
        for c in row[3:]:
            if c.value is not None and (isinstance(c.value, bool) or not isinstance(c.value, (int, float))):
                vs.append(Violation("number_cell", f"{c.coordinate} holds {c.value!r}, not a number", rn))
        rec = {}
        rec["trading_date"] = pd.Timestamp(a.value) if isinstance(a.value, (dt.datetime, dt.date)) else a.value
        rec["trading_period"] = b
        rec["product"] = row[2].value
        for i, k in enumerate(_COLS[3:]):
            v = vals[3 + i]
            rec[k] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else (np.nan if v is None else v)
        recs.append(rec)
    # blank rows inside the data are errors; trailing blank rows are ignored
    n_data = last_nonempty - 1
    out = []
    for i, r in enumerate(recs[:n_data]):
        if r is None:
            vs.append(Violation("blank_row", "empty row inside the bid table", i + 2))
        else:
            out.append(r)
    df = pd.DataFrame(out, columns=_COLS)
    if len(df):
        if df["trading_date"].map(lambda x: isinstance(x, pd.Timestamp)).all():
            df["trading_date"] = pd.to_datetime(df["trading_date"]).astype("datetime64[ns]")
    if ws.auto_filter.ref and n_data >= 1:
        want = f"A1:W{n_data + 1}"
        if ws.auto_filter.ref != want:
            vs.append(Violation("autofilter", f"autofilter {ws.auto_filter.ref} does not cover {want}", severity="warning"))
    return df, vs


def validate_xlsx(path: str | Path, cfg: Config | None = None, month: str | None = None) -> list[Violation]:
    """Validate a submission workbook: cell types and layout, then every table rule."""
    df, vs = read_workbook_table(path)
    if any(v.code in ("sheets", "header") and v.severity == "error" for v in vs):
        return vs
    return vs + validate_table(df, cfg, month)
