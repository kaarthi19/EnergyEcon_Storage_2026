"""Locate and load EMA's competition files.

Every loader returns a tidy DataFrame in naive Singapore time (``datetime64[ns]``):

* clearing:    ``ts, period, energy_price_sgd_mwh, ..., raw_capacity_margin_mw``
* pre-dispatch: ``ts, period, issue_time, lead_hours, forecast_load_mw, forecast_solar_mw,
  known_outage_mw_at_issue`` (+ ``synthetic_actual_load_mw, synthetic_actual_solar_mw`` in
  training)
* stacks:      ``ts, date, period, facility_id, product, band, offered_mw, price,
  available_mw, outage_status, pub, ts_label, shifted, src, idx``

Files are found by name pattern anywhere below ``data_dir`` (EMA's folder layout nests
folders and prefixes names with "(Part 1)"). Nothing is ever written into the data folder;
parquet caches go to ``<work_dir>/cache`` and are keyed by file name, size and mtime.

Stack timestamps: in the first 28 days of the Part-1 stack file ``period_sgt`` is 8 hours
later than the trading period named by the ``period`` column. The true start time is rebuilt
from the label's trading date and ``period`` (``date + (period-1)*30 min``), taking the
calendar day that puts it nearest to the label. Duplicates are dropped only after that fix,
and the (ts, facility, product, band) key must then be unique.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np
import pandas as pd

from .periods import PERIOD

__all__ = [
    "DataFiles",
    "Dataset",
    "SchemaError",
    "find_files",
    "find_template",
    "parse_sgt",
    "load_clearing",
    "load_predispatch",
    "load_stacks",
    "load_all",
    "rebuild_stack_ts",
    "copied_dates",
    "stack_report",
    "STACK_PRODUCTS",
]

CACHE_VERSION = "io-v1"
SGT_OFFSET = pd.Timedelta(hours=8)
STACK_PRODUCTS = ("Energy", "Primary", "Contingency", "Regulation")


class SchemaError(ValueError):
    """An input file does not have the expected columns or values."""


# --------------------------------------------------------------------------------------
# File discovery
# --------------------------------------------------------------------------------------


@dataclass
class DataFiles:
    clearing: list[Path] = field(default_factory=list)
    predispatch_train: list[Path] = field(default_factory=list)
    predispatch_test: list[Path] = field(default_factory=list)
    stacks: list[Path] = field(default_factory=list)
    template: Path | None = None
    ignored: list[Path] = field(default_factory=list)
    duplicates: list[Path] = field(default_factory=list)

    def describe(self) -> dict:
        def info(p: Path) -> dict:
            st = p.stat()
            return {"name": p.name, "size": st.st_size}

        return {
            "clearing": [info(p) for p in self.clearing],
            "predispatch_train": [info(p) for p in self.predispatch_train],
            "predispatch_test": [info(p) for p in self.predispatch_test],
            "stacks": [info(p) for p in self.stacks],
            "template": info(self.template) if self.template else None,
            "ignored": [p.name for p in self.ignored],
            "duplicates": [p.name for p in self.duplicates],
        }


def _norm_name(name: str) -> str:
    n = name.lower()
    n = re.sub(r"\(part\s*\d+\)", " ", n)
    n = re.sub(r"[\s\-]+", "_", n).strip("_")
    return n


def _is_csv(n: str) -> bool:
    return n.endswith(".csv") or n.endswith(".csv.gz")


def _classify(name: str) -> str | None:
    n = _norm_name(name)
    if n.startswith("~$") or n.startswith("."):
        return None
    if n.endswith(".xlsx") and "template" in n:
        return "template"
    if not _is_csv(n):
        return None
    if "offer_stack" in n:
        return "stacks"
    if "pre_dispatch" in n or "predispatch" in n:
        return "predispatch_train" if "train" in n else "predispatch_test"
    if "clearing" in n and "price" in n:
        # Only training clearing prices may be used. Any other clearing file (e.g. actual
        # November results added later) is ignored on purpose.
        return "clearing" if "train" in n else "ignored"
    return None


def find_template(data_dir: str | Path) -> Path:
    """The template workbook below ``data_dir`` (needs no other data file)."""
    root = Path(data_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"data dir not found: {root}")
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__MACOSX")
        hits += [Path(dirpath) / f for f in sorted(filenames) if _classify(f) == "template"]
    hits.sort(key=str)
    uniq = {(_norm_name(p.name), p.stat().st_size): p for p in reversed(hits)}
    if not uniq:
        raise FileNotFoundError(f"no template workbook (*template*.xlsx) under {root}")
    if len(uniq) > 1:
        raise FileNotFoundError(f"more than one template workbook under {root}: {[p.name for p in hits]}; pass --template")
    return next(iter(uniq.values()))


def find_files(data_dir: str | Path) -> DataFiles:
    """Find EMA's files anywhere below ``data_dir`` by name pattern.

    Files with the same normalised name and size found twice are treated as copies (the
    first in sorted path order is kept). Raises ``FileNotFoundError`` if no stack, training
    pre-dispatch or clearing file is present.
    """
    root = Path(data_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"data dir not found: {root}")
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__MACOSX")
        for f in sorted(filenames):
            found.append(Path(dirpath) / f)
    found.sort(key=lambda p: str(p))

    out = DataFiles()
    seen: dict[tuple[str, str, int], Path] = {}
    for p in found:
        role = _classify(p.name)
        if role is None:
            continue
        if role == "ignored":
            out.ignored.append(p)
            continue
        key = (role, _norm_name(p.name), p.stat().st_size)
        if key in seen:
            out.duplicates.append(p)
            continue
        seen[key] = p
        if role == "template":
            if out.template is not None:
                raise FileNotFoundError(
                    f"more than one template workbook: {out.template} and {p}; pass --template"
                )
            out.template = p
        else:
            getattr(out, role).append(p)

    missing = [r for r in ("clearing", "predispatch_train", "stacks") if not getattr(out, r)]
    if missing:
        raise FileNotFoundError(f"no {', '.join(missing)} file(s) found under {root}")
    if len(out.predispatch_test) > 1:
        names = [p.name for p in out.predispatch_test]
        raise FileNotFoundError(f"more than one test pre-dispatch file: {names}")
    return out


# --------------------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------------------

_OFFSET_RE = re.compile(r"(?:[+-]\d{2}:?\d{2}|Z)$")


def _to_ns(x) -> np.ndarray:
    return np.asarray(x, dtype="datetime64[ns]")


def parse_sgt(values: Iterable, what: str = "timestamp") -> np.ndarray:
    """Parse mixed ISO timestamps ('T' or space separated, with or without offset) to
    naive SGT ``datetime64[ns]``. Values with an offset are converted to UTC+8; values
    without one are taken as SGT already. Unparseable or missing values raise."""
    s = pd.Series(values)
    codes, uniq = pd.factorize(s, sort=False)
    if (codes < 0).any():
        raise SchemaError(f"{what}: {int((codes < 0).sum())} missing values")
    u = pd.Series(np.asarray(uniq, dtype=object)).astype(str).str.strip()
    out = np.full(len(u), np.datetime64("NaT", "ns"), dtype="datetime64[ns]")
    has_off = u.str.contains(_OFFSET_RE).to_numpy()
    try:
        if has_off.any():
            t = pd.to_datetime(u[has_off], format="ISO8601", utc=True)
            out[has_off] = _to_ns(t.dt.tz_convert(None) + SGT_OFFSET)
        if (~has_off).any():
            out[~has_off] = _to_ns(pd.to_datetime(u[~has_off], format="ISO8601"))
    except (ValueError, TypeError) as e:
        raise SchemaError(f"{what}: cannot parse timestamps ({e})") from e
    if np.isnat(out).any():
        bad = u[np.isnat(out)].head(3).tolist()
        raise SchemaError(f"{what}: unparseable timestamps, e.g. {bad}")
    return out[codes]


def _require(df: pd.DataFrame, cols: Sequence[str], src: str) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise SchemaError(f"{src}: missing columns {missing}; has {list(df.columns)}")


def _numeric(df: pd.DataFrame, cols: Sequence[str], src: str) -> None:
    for c in cols:
        v = pd.to_numeric(df[c], errors="coerce")
        bad = v.isna() & df[c].notna()
        if bad.any():
            raise SchemaError(f"{src}: column {c!r} has non-numeric values, e.g. {df[c][bad].head(3).tolist()}")
        if v.isna().any():
            raise SchemaError(f"{src}: column {c!r} has {int(v.isna().sum())} missing values")
        df[c] = v.astype("float64")


def _int_col(df: pd.DataFrame, col: str, src: str, lo: int | None = None, hi: int | None = None) -> None:
    v = pd.to_numeric(df[col], errors="coerce")
    if v.isna().any() or (v != np.round(v)).any():
        raise SchemaError(f"{src}: column {col!r} must hold integers")
    v = v.astype("int64")
    if lo is not None and (v < lo).any() or hi is not None and (v > hi).any():
        raise SchemaError(f"{src}: column {col!r} outside [{lo}, {hi}]")
    df[col] = v


def _period_of(ts: np.ndarray) -> np.ndarray:
    t = pd.DatetimeIndex(ts)
    return (t.hour * 2 + t.minute // 30 + 1).to_numpy()


def _check_grid(ts: np.ndarray, src: str) -> None:
    t = pd.DatetimeIndex(ts)
    off = (t.minute % 30 != 0) | (t.second != 0) | (t.microsecond != 0) | (t.nanosecond != 0)
    if off.any():
        raise SchemaError(f"{src}: timestamps not on the 30-minute grid, e.g. {t[off][:3].tolist()}")


# --------------------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------------------


def _cache_key(kind: str, paths: Sequence[Path]) -> str:
    items = []
    for p in sorted(paths, key=lambda q: q.name):
        st = p.stat()
        items.append([p.name, st.st_size, st.st_mtime_ns])
    blob = json.dumps([CACHE_VERSION, pd.__version__.split(".")[0], kind, items]).encode()
    return hashlib.sha256(blob).hexdigest()[:20]


def _cached(kind: str, paths: Sequence[Path], work_dir: str | Path | None, build: Callable[[], pd.DataFrame]) -> pd.DataFrame:
    if work_dir is None:
        return build()
    cdir = Path(work_dir) / "cache"
    cdir.mkdir(parents=True, exist_ok=True)
    f = cdir / f"{kind}_{_cache_key(kind, paths)}.parquet"
    if f.exists():
        try:
            return pd.read_parquet(f)
        except Exception:  # noqa: BLE001 - a broken cache is rebuilt
            f.unlink(missing_ok=True)
    df = build()
    tmp = f.with_suffix(f".tmp{os.getpid()}")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, f)
    return df


def _read_csv(p: Path) -> pd.DataFrame:
    return pd.read_csv(p, dtype=str, keep_default_na=True)


# --------------------------------------------------------------------------------------
# Clearing prices
# --------------------------------------------------------------------------------------

CLEARING_NUMERIC = [
    "energy_price_sgd_mwh",
    "primary_price_sgd_mwh_equiv",
    "contingency_price_sgd_mwh_equiv",
    "regulation_price_sgd_mwh_equiv",
    "load_mw",
    "solar_mw",
    "net_demand_mw",
]
CLEARING_OPTIONAL = [
    "unserved_energy_mw",
    "unmet_primary_mw",
    "unmet_contingency_mw",
    "unmet_regulation_mw",
    "primary_requirement_mw",
    "contingency_requirement_mw",
    "regulation_requirement_mw",
    "raw_capacity_margin_mw",
]


def _build_clearing(paths: Sequence[Path]) -> pd.DataFrame:
    frames = []
    for p in paths:
        df = _read_csv(p)
        _require(df, ["period_sgt", "period", *CLEARING_NUMERIC], p.name)
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    src = "clearing"
    ts = parse_sgt(df["period_sgt"], f"{src}.period_sgt")
    _check_grid(ts, src)
    _int_col(df, "period", src, 1, 48)
    opt = [c for c in CLEARING_OPTIONAL if c in df.columns]
    _numeric(df, CLEARING_NUMERIC + opt, src)
    out = pd.DataFrame({"ts": ts, "period": df["period"].to_numpy()})
    for c in CLEARING_NUMERIC + opt:
        out[c] = df[c].to_numpy()
    if (out["period"].to_numpy() != _period_of(ts)).any():
        raise SchemaError(f"{src}: 'period' disagrees with period_sgt")
    out = out.drop_duplicates()
    if out["ts"].duplicated().any():
        raise SchemaError(f"{src}: conflicting rows for {out['ts'][out['ts'].duplicated()].head(3).tolist()}")
    return out.sort_values("ts", kind="mergesort").reset_index(drop=True)


def load_clearing(files: DataFiles | Sequence[Path], work_dir: str | Path | None = None) -> pd.DataFrame:
    paths = files.clearing if isinstance(files, DataFiles) else [Path(p) for p in files]
    if not paths:
        raise FileNotFoundError("no clearing price file")
    return _cached("clearing", paths, work_dir, lambda: _build_clearing(paths))


# --------------------------------------------------------------------------------------
# Pre-dispatch forecasts
# --------------------------------------------------------------------------------------

PD_NUMERIC = ["lead_hours", "forecast_load_mw", "forecast_solar_mw", "known_outage_mw_at_issue"]
PD_ACTUALS = ["synthetic_actual_load_mw", "synthetic_actual_solar_mw"]


def _build_predispatch(paths: Sequence[Path], kind: str) -> pd.DataFrame:
    frames = []
    for p in paths:
        df = _read_csv(p)
        _require(df, ["period_sgt", "period", "issue_time_sgt", *PD_NUMERIC], p.name)
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    src = f"predispatch_{kind}"
    ts = parse_sgt(df["period_sgt"], f"{src}.period_sgt")
    issue = parse_sgt(df["issue_time_sgt"], f"{src}.issue_time_sgt")
    _check_grid(ts, src)
    _int_col(df, "period", src, 1, 48)
    acts = [c for c in PD_ACTUALS if c in df.columns]
    _numeric(df, PD_NUMERIC + acts, src)
    out = pd.DataFrame({"ts": ts, "period": df["period"].to_numpy(), "issue_time": issue})
    for c in PD_NUMERIC + acts:
        out[c] = df[c].to_numpy()
    if (out["period"].to_numpy() != _period_of(ts)).any():
        raise SchemaError(f"{src}: 'period' disagrees with period_sgt")
    if (out["issue_time"] > out["ts"]).any():
        raise SchemaError(f"{src}: forecasts issued after their trading period starts")
    implied = out["ts"] - pd.to_timedelta(out["lead_hours"], unit="h")
    n_bad = int((implied != out["issue_time"]).sum())
    if n_bad:
        warnings.warn(f"{src}: {n_bad} rows where issue_time != ts - lead_hours; issue_time is used")
    out = out.drop_duplicates()
    dup = out.duplicated(["ts", "issue_time"])
    if dup.any():
        raise SchemaError(f"{src}: {int(dup.sum())} conflicting rows for the same (ts, issue_time)")
    return out.sort_values(["ts", "issue_time"], kind="mergesort").reset_index(drop=True)


def load_predispatch(
    files: DataFiles | Sequence[Path], kind: str = "train", work_dir: str | Path | None = None
) -> pd.DataFrame:
    """Load training (``kind='train'``) or test (``kind='test'``) pre-dispatch forecasts."""
    if kind not in ("train", "test"):
        raise ValueError("kind must be 'train' or 'test'")
    if isinstance(files, DataFiles):
        paths = files.predispatch_train if kind == "train" else files.predispatch_test
    else:
        paths = [Path(p) for p in files]
    if not paths:
        raise FileNotFoundError(f"no {kind} pre-dispatch file")
    return _cached(f"predispatch_{kind}", paths, work_dir, lambda: _build_predispatch(paths, kind))


# --------------------------------------------------------------------------------------
# Offer stacks
# --------------------------------------------------------------------------------------

STACK_REQUIRED = [
    "period_sgt",
    "period",
    "facility_id",
    "product",
    "band",
    "offered_mw",
    "offer_price_sgd_per_mwh_equiv",
    "available_mw",
    "outage_status",
    "assumed_publication_time_sgt",
]
STACK_KEY = ["ts", "facility_id", "product", "band"]


def rebuild_stack_ts(label: np.ndarray, period: np.ndarray) -> np.ndarray:
    """True period start from the label's trading date and the ``period`` column.

    ``date(label) + (period-1)*30min`` is shifted by whole days so that it lies within 12 h
    of the label (a label 8 h late, e.g. 'Jul 1 00:00' for period 33, maps to Jun 30 16:00).
    """
    lab = _to_ns(label)
    day = lab.astype("datetime64[D]").astype("datetime64[ns]")
    cand = day + (np.asarray(period, dtype="int64") - 1) * np.timedelta64(30, "m")
    delta = (lab - cand).astype("timedelta64[m]").astype("int64")  # minutes, label - cand
    half = 12 * 60
    if (np.abs(delta) == half).any():
        raise SchemaError("stacks: period_sgt is exactly 12 h from its period; cannot resolve the day")
    shift_days = np.where(delta > half, 1, np.where(delta < -half, -1, 0))
    return cand + shift_days * np.timedelta64(1, "D")


def _build_stacks(paths: Sequence[Path]) -> pd.DataFrame:
    frames = []
    for p in paths:
        df = _read_csv(p)
        _require(df, STACK_REQUIRED, p.name)
        idx_col = df.columns[0] if df.columns[0].startswith("Unnamed") or df.columns[0] == "" else None
        part = pd.DataFrame({c: df[c] for c in STACK_REQUIRED})
        part["idx"] = pd.to_numeric(df[idx_col], errors="coerce") if idx_col else np.arange(len(df))
        part["src"] = p.name
        frames.append(part)
    df = pd.concat(frames, ignore_index=True)
    src = "stacks"
    _int_col(df, "period", src, 1, 48)
    _int_col(df, "band", src, 1, 10)
    _numeric(df, ["offered_mw", "offer_price_sgd_per_mwh_equiv", "available_mw"], src)
    label = parse_sgt(df["period_sgt"], f"{src}.period_sgt")
    pub = parse_sgt(df["assumed_publication_time_sgt"], f"{src}.assumed_publication_time_sgt")
    _check_grid(label, src)
    ts = rebuild_stack_ts(label, df["period"].to_numpy())
    prod = df["product"].astype(str).str.strip()
    bad = ~prod.isin(STACK_PRODUCTS)
    if bad.any():
        raise SchemaError(f"{src}: unknown products {sorted(prod[bad].unique())[:5]}")
    out = pd.DataFrame(
        {
            "ts": ts,
            "date": ts.astype("datetime64[D]").astype("datetime64[ns]"),
            "period": df["period"].to_numpy(),
            "facility_id": df["facility_id"].astype(str).str.strip().to_numpy(),
            "product": prod.to_numpy(),
            "band": df["band"].to_numpy(),
            "offered_mw": df["offered_mw"].to_numpy(),
            "price": df["offer_price_sgd_per_mwh_equiv"].to_numpy(),
            "available_mw": df["available_mw"].to_numpy(),
            "outage_status": df["outage_status"].astype(str).str.strip().to_numpy(),
            "pub": pub,
            "ts_label": label,
            "shifted": ts != label,
            "src": df["src"].to_numpy(),
            "idx": df["idx"].to_numpy(),
        }
    )
    if (out["period"].to_numpy() != _period_of(out["ts"].to_numpy())).any():
        raise SchemaError(f"{src}: period/time mismatch remains after the shift fix")
    content = [c for c in out.columns if c not in ("ts_label", "shifted", "src", "idx")]
    out = out.drop_duplicates(subset=content, keep="first")
    dup = out.duplicated(STACK_KEY)
    if dup.any():
        ex = out.loc[dup, STACK_KEY].head(3).to_dict("records")
        raise SchemaError(f"{src}: {int(dup.sum())} conflicting duplicate rows after the shift fix, e.g. {ex}")
    if (out["pub"] < out["ts"]).any():
        raise SchemaError(f"{src}: publication time before the trading period")
    out = out.sort_values(STACK_KEY, kind="mergesort").reset_index(drop=True)
    for c in ("facility_id", "product", "outage_status", "src"):
        out[c] = out[c].astype("category")
    return out


def load_stacks(files: DataFiles | Sequence[Path], work_dir: str | Path | None = None) -> pd.DataFrame:
    """Concatenate all offer-stack files, fix the Part-1 time shift and validate."""
    paths = files.stacks if isinstance(files, DataFiles) else [Path(p) for p in files]
    if not paths:
        raise FileNotFoundError("no offer-stack files")
    return _cached("stacks", paths, work_dir, lambda: _build_stacks(paths))


def stack_report(stacks: pd.DataFrame) -> dict:
    """Summary of the stack load: periods, shift fix, publication lags."""
    ts = stacks["ts"]
    u = np.sort(ts.unique())
    gaps = int((np.diff(u) != np.timedelta64(30, "m")).sum()) if len(u) > 1 else 0
    lag = (stacks["pub"] - stacks["ts"]).value_counts()
    sh = stacks.loc[stacks["shifted"]]
    return {
        "rows": int(len(stacks)),
        "periods": int(len(u)),
        "first": str(pd.Timestamp(u[0])) if len(u) else None,
        "last": str(pd.Timestamp(u[-1])) if len(u) else None,
        "gaps": gaps,
        "shifted_rows": int(len(sh)),
        "shift_hours": sorted({float(x) for x in ((sh["ts_label"] - sh["ts"]).dt.total_seconds() / 3600).unique()}),
        "pub_lags": {str(k): int(v) for k, v in lag.items()},
    }


def copied_dates(stacks: pd.DataFrame, lag_days: int = 28) -> list[pd.Timestamp]:
    """Trading dates whose whole stack equals the stack ``lag_days`` later (June == July)."""
    cols = ["period", "facility_id", "product", "band", "offered_mw", "price", "available_mw", "outage_status"]
    tmp = stacks[cols].copy()
    for c in ("facility_id", "product", "outage_status"):
        tmp[c] = tmp[c].astype(str)
    h = pd.util.hash_pandas_object(tmp, index=False).to_numpy().astype("uint64")
    per = pd.DataFrame({"date": stacks["date"].to_numpy(), "h": h})
    g = per.groupby("date")["h"].agg(lambda x: int(np.bitwise_xor.reduce(np.sort(x.to_numpy()))) ^ len(x))
    cnt = per.groupby("date").size()
    out = []
    for d, hv in g.items():
        d2 = d + pd.Timedelta(days=lag_days)
        if d2 in g.index and g[d2] == hv and cnt[d2] == cnt[d]:
            out.append(pd.Timestamp(d))
    return out


# --------------------------------------------------------------------------------------
# Everything at once
# --------------------------------------------------------------------------------------


@dataclass
class Dataset:
    files: DataFiles
    clearing: pd.DataFrame
    predispatch_train: pd.DataFrame
    predispatch_test: pd.DataFrame | None
    stacks: pd.DataFrame


def load_all(data_dir: str | Path, work_dir: str | Path | None = None) -> Dataset:
    files = find_files(data_dir)
    return Dataset(
        files=files,
        clearing=load_clearing(files, work_dir),
        predispatch_train=load_predispatch(files, "train", work_dir),
        predispatch_test=load_predispatch(files, "test", work_dir) if files.predispatch_test else None,
        stacks=load_stacks(files, work_dir),
    )


# Re-exported for modules that need the period length.
PERIOD_LENGTH = PERIOD
