"""Command line interface.

    python -m bessbid generate --data-dir D --out OUT.xlsx --work-dir W \
        [--template T] [--strategy regulation|full] [--month 2026-11] [--config cfg.toml]
    python -m bessbid validate OUT.xlsx [--month 2026-11] [--config cfg.toml]
    python -m bessbid inspect --data-dir D --work-dir W

``generate`` builds the bids, writes them into a copy of EMA's template, validates the file
with the independent validator, reads it back and compares it with what was meant to be
written, and stores a JSON manifest (inputs, config, checks, information-audit summary,
output SHA-256) in the work dir. Exit code 0 on success, 2 if the file fails validation.
Nothing is written into the data dir.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np

from . import __version__
from .bids import to_table
from .config import load_config
from .strategy import StrategyError, available_strategies, check_complete, get_strategy
from .validate import errors, format_report, validate_xlsx
from .writer import read_back, write_submission


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _tables_equal(a, b) -> bool:
    if list(a.columns) != list(b.columns) or len(a) != len(b):
        return False
    for c in a.columns:
        x, y = a[c].to_numpy(), b[c].to_numpy()
        if x.dtype.kind == "f":
            if not np.array_equal(x, y, equal_nan=True):
                return False
        elif not (x == y).all():
            return False
    return True


def cmd_generate(args) -> int:
    t0 = time.time()
    cfg = load_config(args.config)
    if args.month:
        cfg = cfg.replace(month=args.month)
    month = cfg.month
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    out = Path(args.out)
    data_dir = Path(args.data_dir) if args.data_dir else None
    if data_dir is not None:
        work_r, data_r = work.resolve(), data_dir.resolve()
        out_r = out.resolve()
        if work_r == data_r or data_r in work_r.parents or data_r in out_r.parents:
            print("error: --work-dir and --out must be outside the data dir", file=sys.stderr)
            return 1

    strategy = get_strategy(args.strategy)
    from . import io

    if args.template:
        template = Path(args.template)
    elif data_dir is not None:
        template = io.find_template(data_dir)
    else:
        print("error: give --template or --data-dir", file=sys.stderr)
        return 1

    infoset, files_info = None, None
    if strategy.needs_data:
        if data_dir is None:
            print(f"error: strategy {strategy.name!r} needs --data-dir", file=sys.stderr)
            return 1
        from .infoset import InfoSet

        ds = io.load_all(data_dir, work)
        files_info = ds.files.describe()
        infoset = InfoSet.from_dataset(ds, cfg)
    elif data_dir is not None:
        try:
            files_info = io.find_files(data_dir).describe()
        except FileNotFoundError as e:  # the fallback does not need the data
            files_info = {"warning": str(e)}

    bids = strategy.generate(month, infoset, cfg)
    check_complete(bids, month)
    table = to_table(bids)
    write_submission(table, template, out, cfg, month)

    vs = validate_xlsx(out, cfg, month)
    back = read_back(out)
    roundtrip_ok = _tables_equal(back, table)
    audit = None
    if infoset is not None:
        infoset.assert_no_leak()
        audit = infoset.audit_summary()

    manifest = {
        "package_version": __version__,
        "strategy": strategy.name,
        "month": month,
        "output": out.name,
        "output_sha256": _sha256(out),
        "rows": int(len(table)),
        # environment-independent fingerprint of the bids themselves
        "table_sha256": hashlib.sha256(table.to_csv(index=False, float_format="%.6f").encode()).hexdigest(),
        "template": {"name": template.name, "sha256": _sha256(template)},
        "inputs": files_info,
        "config": cfg.to_dict(),
        "validation": {"errors": len(errors(vs)), "warnings": len(vs) - len(errors(vs)),
                       "messages": [str(v) for v in vs[:50]]},
        "roundtrip_ok": roundtrip_ok,
        "information_audit": audit,
        "offered_mw": {
            "energy_abs_total_mwh_equiv": float(np.nansum(np.abs(table.loc[table["product"] == "Energy", [f"quantity_{i}" for i in range(1, 11)]].to_numpy()))) * cfg.period_hours,
            "max_period_offer_mw": float(max(sum(abs(b.mw) for o in pb.offers().values() for b in o) for pb in bids)),
        },
    }
    mpath = work / f"{out.stem}.manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n")

    print(f"strategy={strategy.name} month={month} rows={len(table)} -> {out}")
    print(format_report(vs))
    print(f"read-back identical: {roundtrip_ok}; sha256 {manifest['output_sha256'][:16]}...; "
          f"manifest {mpath}; {time.time() - t0:.1f}s")
    if errors(vs) or not roundtrip_ok:
        return 2
    return 0


def cmd_validate(args) -> int:
    cfg = load_config(args.config)
    month = args.month or cfg.month
    vs = validate_xlsx(args.path, cfg, month)
    print(format_report(vs, limit=args.limit))
    return 2 if errors(vs) else 0


def cmd_inspect(args) -> int:
    from . import io

    files = io.find_files(args.data_dir)
    print(json.dumps(files.describe(), indent=2))
    ds = io.load_all(args.data_dir, args.work_dir)
    print("clearing", ds.clearing.shape, ds.clearing["ts"].min(), "->", ds.clearing["ts"].max())
    print("predispatch_train", ds.predispatch_train.shape)
    if ds.predispatch_test is not None:
        print("predispatch_test", ds.predispatch_test.shape, ds.predispatch_test["ts"].min(), "->", ds.predispatch_test["ts"].max())
    print("stacks", json.dumps(io.stack_report(ds.stacks), indent=2))
    cd = io.copied_dates(ds.stacks)
    if cd:
        print(f"stack dates that copy the data 28 days later (excluded from fits): {cd[0].date()} .. {cd[-1].date()} ({len(cd)})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m bessbid", description="EMA BESS bid generator")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="generate and validate a bid workbook")
    g.add_argument("--data-dir", help="folder holding EMA's data (searched recursively)")
    g.add_argument("--template", help="EMA template .xlsx (default: found in --data-dir)")
    g.add_argument("--out", required=True, help="output .xlsx path")
    g.add_argument("--work-dir", required=True, help="cache/manifest folder (outside the repo and data dir)")
    g.add_argument("--strategy", default="regulation", choices=available_strategies())
    g.add_argument("--month", default=None, help="YYYY-MM (default from config: 2026-11)")
    g.add_argument("--config", default=None, help="optional .toml/.json overrides")
    g.set_defaults(func=cmd_generate)

    v = sub.add_parser("validate", help="validate a bid workbook")
    v.add_argument("path")
    v.add_argument("--month", default=None)
    v.add_argument("--config", default=None)
    v.add_argument("--limit", type=int, default=30)
    v.set_defaults(func=cmd_validate)

    i = sub.add_parser("inspect", help="find and load the data, print a summary")
    i.add_argument("--data-dir", required=True)
    i.add_argument("--work-dir", required=True)
    i.set_defaults(func=cmd_inspect)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (StrategyError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
