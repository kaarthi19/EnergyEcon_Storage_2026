#!/usr/bin/env bash
# End-to-end pipeline: unit tests (no data) -> November bids -> validation -> figures.
#
# Usage:
#   ./run_all.sh --data-dir "/path/to/ema bidding comp" --work-dir /path/to/work \
#       [--config config/default.toml] [--python python3] [--skip-tests] [--skip-figures]
#
# Outputs, all in --work-dir (keep it outside the repository; they derive from EMA's
# confidential data):
#   submission.xlsx            the bid workbook in EMA's template
#   submission.plan.csv        per-period plan: energy MW, Regulation MW, SOC, forecasts
#   submission.manifest.json   inputs and hashes, config, checks, information audit, run record
#   figures/                   fig01_stress_window.*, figS01_november_plan.* and *_numbers.json
set -euo pipefail
CALLER_DIR="$(pwd)"
cd "$(dirname "$0")"
# Relative paths given on the command line are relative to where the script was called from.
abspath() { case "$1" in /*) printf '%s' "$1" ;; *) printf '%s/%s' "$CALLER_DIR" "$1" ;; esac; }

DATA_DIR=""
WORK_DIR=""
CONFIG="config/default.toml"
PY="${PYTHON:-python}"
SKIP_TESTS=0
SKIP_FIGURES=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --data-dir) DATA_DIR="$(abspath "$2")"; shift 2 ;;
    --work-dir) WORK_DIR="$(abspath "$2")"; shift 2 ;;
    --config) CONFIG="$(abspath "$2")"; shift 2 ;;
    --python) PY="$2"; shift 2 ;;
    --skip-tests) SKIP_TESTS=1; shift ;;
    --skip-figures) SKIP_FIGURES=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done
if [[ -z "$DATA_DIR" || -z "$WORK_DIR" ]]; then
  echo "error: --data-dir and --work-dir are required (see --help)" >&2
  exit 1
fi
CREATED=0
if [[ ! -d "$WORK_DIR" ]]; then mkdir -p "$WORK_DIR"; CREATED=1; fi
REPO_ROOT="$(cd .. && pwd -P)"
WORK_ABS="$(cd "$WORK_DIR" && pwd -P)"
case "$WORK_ABS/" in
  "$REPO_ROOT"/*)
    if [[ "$CREATED" -eq 1 ]]; then rmdir "$WORK_DIR"; fi
    echo "error: --work-dir must be outside the repository (outputs derive from EMA's confidential data)" >&2
    exit 1 ;;
esac

OUT="$WORK_DIR/submission.xlsx"
PLAN="$WORK_DIR/submission.plan.csv"

if [[ "$SKIP_TESTS" -eq 0 ]]; then
  echo "== 1. Unit tests (synthetic data only)"
  env -u BESSBID_DATA_DIR -u BESSBID_WORK_DIR "$PY" -m pytest -q
fi

echo "== 2. Generate the November bids"
"$PY" -m bessbid generate --data-dir "$DATA_DIR" --work-dir "$WORK_DIR" --out "$OUT" \
  --plan-out "$PLAN" --config "$CONFIG"

echo "== 3. Validate the workbook against the template rules"
"$PY" -m bessbid validate "$OUT" --config "$CONFIG"

if [[ "$SKIP_FIGURES" -eq 0 ]]; then
  echo "== 4. Figures"
  if ! "$PY" -c "import matplotlib" 2>/dev/null; then
    echo "error: matplotlib is missing; pip install -r requirements-figures.txt, or pass --skip-figures" >&2
    exit 1
  fi
  "$PY" plots/fig_november_plan.py "$PLAN" "$WORK_DIR/figures"
fi

echo "Done. Workbook: $OUT"
echo "      Plan: $PLAN; manifest: $WORK_DIR/submission.manifest.json; figures: $WORK_DIR/figures/"
