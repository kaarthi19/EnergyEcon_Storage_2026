"""Command line interface.

    python -m bessbid generate --data-dir D --out OUT.xlsx --work-dir W \
        [--template T] [--strategy full|regulation] [--month 2026-11] [--config cfg.toml] \
        [--plan-out PLAN.csv] [--allow-incomplete-inputs]
    python -m bessbid validate OUT.xlsx [--month 2026-11] [--config cfg.toml]
    python -m bessbid inspect --data-dir D --work-dir W
    python -m bessbid evaluate --data-dir D --work-dir W [--month M ...] [--no-pf] \
        [--soc-policy none|clip|reject]
    python -m bessbid backtest --data-dir D --work-dir W (--bids X.xlsx | --strategy NAME) \
        [--month 2026-10 ...] [--mode maker|taker|both] [--convention sqrt|charge|discharge|all] \
        [--soc-policy none|clip|reject] [--tie strict|inclusive]

``generate`` builds the bids, writes them into a copy of EMA's template, validates the file
with the independent validator, reads it back and compares it with what was meant to be
written, and stores a JSON manifest in the work dir: input names, sizes and SHA-256, config
and strategy options, checks, information-audit summary, output and plan SHA-256, and the
run record (``run_info``: git commit and dirty flag, Python, package and HiGHS versions,
platform, runtime). The default strategy is ``full`` (Regulation plus the planned
energy overlay); it also writes the per-period plan (dispatch, SOC under every efficiency
convention, forecasts) to ``--plan-out`` (default ``<work>/<out stem>.plan.csv``).
Exit code 0 on success, 2 if the file fails validation.
Nothing is written into the data dir.

Input checks: a strategy that reads market data stops with exit code 1 when the test-month
pre-dispatch file is missing or does not give every period of the month a legal vintage
(lead 2 h / 3 h, issued by the period's gate); the manifest records the coverage
(``inputs.predispatch_test_coverage``). ``--allow-incomplete-inputs`` turns that into a
warning (diagnostics only, not for a submission). The full strategy also checks its event
calendar against the pre-dispatch vintages (manifest ``plan.info.calendar_check``, one line
on stderr). If the full strategy cannot plan at all, ``generate`` warns loudly and writes the
Regulation-only fallback instead of nothing (manifest ``fallback``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import __version__
from .bids import to_table
from .config import load_config, load_strategy_options
from .strategy import StrategyError, available_strategies, check_complete, get_strategy
from .validate import errors, format_report, validate_xlsx
from .writer import read_back, write_submission


def _sha256(p: Path) -> str:
    from .io import file_sha256

    return file_sha256(p)


def _strategy_options(path, name: str | None = None):
    """``[strategy.<name>]`` tables of the config file; all of them when ``name`` is None.
    A table for an unknown strategy is an error (it would otherwise be ignored silently)."""
    opts = load_strategy_options(path)
    unknown = sorted(set(opts) - set(available_strategies()))
    if unknown:
        raise StrategyError(f"config file has [strategy] tables for unknown strategies {unknown}; "
                            f"known: {available_strategies()}")
    return opts if name is None else opts.get(name, {})


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


def predispatch_coverage(ds, cfg, month: str) -> dict:
    """How many periods of ``month`` have a legal pre-dispatch vintage in the test-month
    file: a row of the period itself with a configured lead (2 h / 3 h) issued by the
    period's gate (``T - gate_closure_min``)."""
    from .periods import month_slots

    Ts = np.array([s.start for s in month_slots(month)], dtype="datetime64[ns]")
    out = {"periods": int(len(Ts)), "covered": 0, "file": bool(ds.predispatch_test is not None),
           "first_missing": None}
    p = ds.predispatch_test
    covered = np.zeros(len(Ts), bool)
    if p is not None and len(p):
        leads = set(float(x) for x in cfg.predispatch_leads)
        gate = np.timedelta64(int(cfg.gate_closure_min), "m")
        ts = p["ts"].to_numpy(dtype="datetime64[ns]")
        it = p["issue_time"].to_numpy(dtype="datetime64[ns]")
        ok = p["lead_hours"].astype(float).isin(leads).to_numpy() & (it <= ts - gate)
        covered = np.isin(Ts, ts[ok])
        out["covered"] = int(covered.sum())
    if not covered.all():
        out["first_missing"] = str(pd.Timestamp(Ts[np.argmin(covered)]))
    return out


def cmd_generate(args) -> int:
    t0 = time.time()
    started = dt.datetime.now(dt.timezone.utc)
    cfg = load_config(args.config)
    strategy_options = _strategy_options(args.config)
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

    strategy = get_strategy(args.strategy, options=strategy_options.get(args.strategy))
    from . import io

    if args.template:
        template = Path(args.template)
    elif data_dir is not None:
        template = io.find_template(data_dir)
    else:
        print("error: give --template or --data-dir", file=sys.stderr)
        return 1

    infoset, files_info = None, None
    input_warnings: list[str] = []
    if strategy.needs_data:
        if data_dir is None:
            print(f"error: strategy {strategy.name!r} needs --data-dir", file=sys.stderr)
            return 1
        from .infoset import InfoSet

        ds = io.load_all(data_dir, work)
        files_info = ds.files.describe(hashes=True)
        cov = predispatch_coverage(ds, cfg, month)
        files_info["predispatch_test_coverage"] = cov
        if not cov["file"]:
            input_warnings.append(f"no test-month pre-dispatch file found under {data_dir} (looked for "
                                  "'*pre_dispatch*.csv' / '*predispatch*.csv' that is not training data "
                                  "by its name or folder)")
        elif cov["covered"] < cov["periods"]:
            input_warnings.append(f"the test-month pre-dispatch file gives a legal lead-2h/3h vintage for only "
                                  f"{cov['covered']} of {cov['periods']} periods of {month} (first missing: "
                                  f"{cov['first_missing']})")
        if input_warnings and not args.allow_incomplete_inputs:
            for w in input_warnings:
                print(f"error: {w}", file=sys.stderr)
            print("error: the November pre-dispatch forecasts are required (each period's lead-2h "
                  "charge guard uses its own vintage); add the file to --data-dir. "
                  "--allow-incomplete-inputs runs anyway (diagnostics only).", file=sys.stderr)
            return 1
        for w in input_warnings:
            print(f"WARNING: {w}. The lead-2h charge guard falls back to the day-ahead inputs.",
                  file=sys.stderr)
        infoset = InfoSet.from_dataset(ds, cfg)
    elif data_dir is not None:
        try:
            files_info = io.find_files(data_dir).describe(hashes=True)
        except FileNotFoundError as e:  # the fallback does not need the data
            files_info = {"warning": str(e)}

    fallback = None
    try:
        bids = strategy.generate(month, infoset, cfg)
    except (RuntimeError, ValueError) as e:
        from .infoset import LeakError

        if isinstance(e, LeakError) or strategy.name == "regulation":
            raise
        fallback = f"{type(e).__name__}: {e}"
        print(f"WARNING: strategy {strategy.name!r} failed ({fallback}); writing the Regulation-only "
              "fallback instead", file=sys.stderr)
        strategy = get_strategy("regulation", options=strategy_options.get("regulation"))
        bids = strategy.generate(month, None, cfg)
    check_complete(bids, month)
    plan_info = getattr(strategy, "plan_info", None) or {}
    cc = plan_info.get("calendar_check")
    if cc is not None and cc.get("ok"):
        print(f"calendar check: {cc['messages'][0]}", file=sys.stderr)
    for w in plan_info.get("warnings", []):
        print(f"WARNING: {w}", file=sys.stderr)
    table = to_table(bids)
    write_submission(table, template, out, cfg, month)

    vs = validate_xlsx(out, cfg, month)
    back = read_back(out)
    roundtrip_ok = _tables_equal(back, table)
    audit = None
    if infoset is not None:
        infoset.assert_no_leak()
        audit = infoset.audit_summary()

    plan_path = None
    plan_frame = getattr(strategy, "plan_frame", None)
    if plan_frame is not None:
        plan_path = Path(args.plan_out) if args.plan_out else work / f"{out.stem}.plan.csv"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_frame.to_csv(plan_path, index=False, float_format="%.4f", date_format="%Y-%m-%d %H:%M:%S")

    manifest = {
        "package_version": __version__,
        "strategy": strategy.name,
        "strategy_options": strategy_options,
        "month": month,
        "output": out.name,
        "output_sha256": _sha256(out),
        "rows": int(len(table)),
        # environment-independent fingerprint of the bids themselves
        "table_sha256": hashlib.sha256(table.to_csv(index=False, float_format="%.6f").encode()).hexdigest(),
        "template": {"name": template.name, "sha256": _sha256(template)},
        "inputs": files_info,
        "input_warnings": input_warnings,
        "fallback": fallback,
        "config": cfg.to_dict(),
        "validation": {"errors": len(errors(vs)), "warnings": len(vs) - len(errors(vs)),
                       "messages": [str(v) for v in vs[:50]]},
        "roundtrip_ok": roundtrip_ok,
        "information_audit": audit,
        "plan": None if plan_path is None else {"path": str(plan_path), "sha256": _sha256(plan_path),
                                                "info": getattr(strategy, "plan_info", None)},
        "offered_mw": {
            "energy_abs_total_mwh_equiv": float(np.nansum(np.abs(table.loc[table["product"] == "Energy", [f"quantity_{i}" for i in range(1, 11)]].to_numpy()))) * cfg.period_hours,
            "max_period_offer_mw": float(max(sum(abs(b.mw) for o in pb.offers().values() for b in o) for pb in bids)),
        },
    }
    from .runinfo import run_info

    manifest["run_info"] = run_info(started, time.time() - t0)
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


def cmd_backtest(args) -> int:
    """Replay bids on training months with the EMA-replica engine; summary to stdout and to
    ``<work>/backtest_<label>.json`` (per-period frames as ``.parquet`` with --frames)."""
    from . import io
    from .backtest import MarketData, run_months
    from .bids import to_table as _to_table

    cfg = load_config(args.config)
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    data_r = Path(args.data_dir).resolve()
    if work.resolve() == data_r or data_r in work.resolve().parents:
        print("error: --work-dir must be outside the data dir", file=sys.stderr)
        return 1
    ds = io.load_all(args.data_dir, work)
    md = MarketData.from_dataset(ds, start=cfg.stack_fit_start)
    if bool(args.bids) == bool(args.strategy):
        print("error: give exactly one of --bids or --strategy", file=sys.stderr)
        return 1
    if args.bids:
        table = read_back(args.bids)
        bids_for_month = lambda m: table  # noqa: E731
        label = Path(args.bids).stem
        allow_missing = True  # a workbook covers one month; other periods carry no offer
    else:
        strategy = get_strategy(args.strategy, options=_strategy_options(args.config, args.strategy))
        infoset = None
        if strategy.needs_data:
            from .infoset import InfoSet

            infoset = InfoSet.from_dataset(ds, cfg)

        def bids_for_month(m):
            bids = strategy.generate(m, infoset, cfg.replace(month=m))
            check_complete(bids, m)
            return _to_table(bids)

        label = strategy.name
        allow_missing = False
    modes = ("maker", "taker") if args.mode == "both" else (args.mode,)
    convs = ("discharge", "sqrt", "charge") if args.convention == "all" else (args.convention,)
    try:
        res = run_months(bids_for_month, md, months=args.month, modes=modes, conventions=convs, cfg=cfg,
                         soc_policy=args.soc_policy, tie=args.tie, allow_missing=allow_missing)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    cols = ["month", "mode", "convention", "revenue", "revenue_per_30d", "rev_E", "rev_P", "rev_C", "rev_R",
            "breach_periods", "clipped_periods", "rejected_periods", "runtime_s"]
    with __import__("pandas").option_context("display.width", 200, "display.max_columns", 20):
        print(res[[c for c in cols if c in res.columns]].round(1).to_string(index=False))
    out = work / f"backtest_{label}.json"
    out.write_text(json.dumps(res.to_dict(orient="records"), indent=2, default=str) + "\n")
    print(f"summary -> {out}")
    return 0


def cmd_evaluate(args) -> int:
    """Walk-forward evaluation of the full strategy on pseudo-Novembers (see
    :mod:`bessbid.evaluate`); table to stdout, ``<work>/evaluation_full.csv`` and the plans as
    ``<work>/evaluation_plan_<month>.csv``."""
    import pandas as pd

    from . import io
    from .evaluate import evaluate

    cfg = load_config(args.config)
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    data_r = Path(args.data_dir).resolve()
    if work.resolve() == data_r or data_r in work.resolve().parents:
        print("error: --work-dir must be outside the data dir", file=sys.stderr)
        return 1
    from .strategy_full import FullParams

    params = FullParams.from_mapping(_strategy_options(args.config, "full"))
    ds = io.load_all(args.data_dir, work)
    caps = () if args.no_pf else (50.0, 100.0)
    res, plans = evaluate(ds, cfg, months=args.month, params=params, perfect_foresight=caps,
                          progress=lambda msg: print(msg, flush=True), soc_policy=args.soc_policy)
    out = work / "evaluation_full.csv"
    res.to_csv(out, index=False, float_format="%.4f")
    from .evaluate import single_miss_exposure

    miss = []
    for m, f in plans.items():
        f.to_csv(work / f"evaluation_plan_{m}.csv", index=False, float_format="%.4f",
                 date_format="%Y-%m-%d %H:%M:%S")
        sm = single_miss_exposure(f["energy_mw"].to_numpy(), cfg.replace(month=m))
        sm.insert(0, "month", m)
        miss.append(sm)
    cols = ["policy", "month", "mode", "convention", "revenue", "revenue_per_30d", "uplift", "uplift_per_30d",
            "rev_E", "rev_C", "rev_R", "soc_min", "soc_max", "breach_periods"]
    cols += [c for c in ("clipped_periods", "rejected_periods") if c in res.columns]
    with pd.option_context("display.width", 250, "display.max_columns", 30, "display.max_rows", 500):
        print(res[cols].round(1).to_string(index=False))
        if miss:
            mf = pd.concat(miss, ignore_index=True)
            mf.to_csv(work / "evaluation_single_miss.csv", index=False, float_format="%.4f")
            print("single missed band (one energy band fails to clear, all others clear):")
            print(mf.round(2).to_string(index=False))
    print(f"summary -> {out}")
    full = res[(res["policy"] == "full") & (res["month"] != "ALL")]
    return 0 if int(full["breach_periods"].sum()) == 0 else 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m bessbid", description="EMA BESS bid generator")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="generate and validate a bid workbook")
    g.add_argument("--data-dir", help="folder holding EMA's data (searched recursively)")
    g.add_argument("--template", help="EMA template .xlsx (default: found in --data-dir)")
    g.add_argument("--out", required=True, help="output .xlsx path")
    g.add_argument("--work-dir", required=True, help="cache/manifest folder (outside the repo and data dir)")
    g.add_argument("--strategy", default="full", choices=available_strategies(),
                   help="full (default): Regulation + planned energy overlay; regulation: fallback")
    g.add_argument("--month", default=None, help="YYYY-MM (default from config: 2026-11)")
    g.add_argument("--config", default=None,
                   help="optional .toml/.json overrides (see config/default.toml; [strategy.full] sets the "
                        "strategy parameters)")
    g.add_argument("--plan-out", default=None, help="CSV for the per-period plan (strategies that make one)")
    g.add_argument("--allow-incomplete-inputs", action="store_true",
                   help="warn instead of failing (exit 1) when the test-month pre-dispatch is missing or "
                        "partial (diagnostics only; not for a submission)")
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

    b = sub.add_parser("backtest", help="replay bids on training months with the EMA-replica engine")
    b.add_argument("--data-dir", required=True)
    b.add_argument("--work-dir", required=True)
    b.add_argument("--bids", help="bid workbook to replay (its dates must be in the months)")
    b.add_argument("--strategy", help="strategy name; bids are generated for each month")
    b.add_argument("--month", nargs="+", default=["2026-08", "2026-09", "2026-10"])
    b.add_argument("--mode", choices=["maker", "taker", "both"], default="both")
    b.add_argument("--convention", choices=["sqrt", "charge", "discharge", "all"], default="sqrt")
    b.add_argument("--soc-policy", choices=["none", "clip", "reject"], default="none")
    b.add_argument("--tie", choices=["strict", "inclusive"], default="strict")
    b.add_argument("--config", default=None)
    b.set_defaults(func=cmd_backtest)

    e = sub.add_parser("evaluate", help="walk-forward pseudo-November evaluation of the full strategy")
    e.add_argument("--data-dir", required=True)
    e.add_argument("--work-dir", required=True)
    e.add_argument("--month", nargs="+", default=["2026-08", "2026-09", "2026-10"])
    e.add_argument("--no-pf", action="store_true", help="skip the perfect-foresight bounds")
    e.add_argument("--soc-policy", choices=["none", "clip", "reject"], default="none",
                   help="how the evaluator treats an award that would leave the SOC limits (default none)")
    e.add_argument("--config", default=None)
    e.set_defaults(func=cmd_evaluate)
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
