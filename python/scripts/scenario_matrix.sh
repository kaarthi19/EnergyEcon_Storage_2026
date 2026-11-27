#!/usr/bin/env bash
# Regenerate the November workbook of every scenario config (config/scenarios/*.toml) and of
# the default config, validate each, and print a table of SHA-256 fingerprints.
#
# Usage (from anywhere):
#   python/scripts/scenario_matrix.sh --data-dir "/path/to/ema bidding comp" --work-dir /path/to/work \
#       [--python python3] [--only NAME ...] [--evaluate] [--jobs N]
#
#   --only NAME     run only these scenarios (file stem, e.g. s6_no_ramp; "default" = config/default.toml)
#   --evaluate      also run the Aug-Oct walk-forward (`evaluate --no-pf`) of each scenario in the
#                   evaluation mode of config/scenarios/README.md (about 10 min each)
#   --jobs N        run N scenarios at a time (default 1; each needs about 2 GB of memory)
#
# Outputs, in --work-dir (keep it outside the repository; everything derives from EMA's
# confidential data):
#   <name>/submission.xlsx, .plan.csv, .manifest.json, generate.log    per scenario
#   <name>/eval/evaluation_full.csv, eval.log                         with --evaluate
#   scenario_matrix.tsv                                              name, exit code, workbook SHA-256
set -euo pipefail
CALLER_DIR="$(pwd)"
cd "$(dirname "$0")/.."
abspath() { case "$1" in /*) printf '%s' "$1" ;; *) printf '%s/%s' "$CALLER_DIR" "$1" ;; esac; }

DATA_DIR=""
WORK_DIR=""
PY="${PYTHON:-python}"
ONLY=()
EVAL=0
JOBS=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --data-dir) DATA_DIR="$(abspath "$2")"; shift 2 ;;
    --work-dir) WORK_DIR="$(abspath "$2")"; shift 2 ;;
    --python) PY="$2"; shift 2 ;;
    --only) ONLY+=("$2"); shift 2 ;;
    --evaluate) EVAL=1; shift ;;
    --jobs) JOBS="$2"; shift 2 ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done
if [[ -z "$DATA_DIR" || -z "$WORK_DIR" ]]; then
  echo "error: --data-dir and --work-dir are required (see --help)" >&2
  exit 1
fi
mkdir -p "$WORK_DIR"
REPO_ROOT="$(cd .. && pwd -P)"
WORK_ABS="$(cd "$WORK_DIR" && pwd -P)"
case "$WORK_ABS/" in
  "$REPO_ROOT"/*) echo "error: --work-dir must be outside the repository" >&2; exit 1 ;;
esac

# soc policy of the walk-forward for each scenario (see config/scenarios/README.md)
policy_of() {
  case "$1" in
    s1_*clip*) echo clip ;;
    s1_*reject*) echo reject ;;
    *) echo none ;;
  esac
}

NAMES=(default)
for f in config/scenarios/*.toml; do NAMES+=("$(basename "$f" .toml)"); done
if [[ ${#ONLY[@]} -gt 0 ]]; then NAMES=("${ONLY[@]}"); fi

# parquet caches are shared by every scenario (built once by the first run)
mkdir -p "$WORK_ABS/cache"

run_one() {
  local name="$1" cfg out
  if [[ "$name" == "default" ]]; then cfg="config/default.toml"; else cfg="config/scenarios/$name.toml"; fi
  [[ -f "$cfg" ]] || { echo "error: no config $cfg" >&2; return 1; }
  out="$WORK_ABS/$name"
  mkdir -p "$out"
  ln -sfn "$WORK_ABS/cache" "$out/cache"
  local rc=0
  "$PY" -m bessbid generate --data-dir "$DATA_DIR" --work-dir "$out" --out "$out/submission.xlsx" \
    --plan-out "$out/submission.plan.csv" --config "$cfg" > "$out/generate.log" 2>&1 || rc=$?
  if [[ "$rc" -eq 0 ]]; then
    "$PY" -m bessbid validate "$out/submission.xlsx" --config "$cfg" >> "$out/generate.log" 2>&1 || rc=$?
  fi
  if [[ "$EVAL" -eq 1 ]]; then
    mkdir -p "$out/eval"
    ln -sfn "$WORK_ABS/cache" "$out/eval/cache"
    "$PY" -m bessbid evaluate --data-dir "$DATA_DIR" --work-dir "$out/eval" --no-pf \
      --soc-policy "$(policy_of "$name")" --config "$cfg" > "$out/eval/eval.log" 2>&1 || true
  fi
  local sha="-"
  if [[ -f "$out/submission.xlsx" ]]; then sha="$("$PY" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$out/submission.xlsx")"; fi
  printf '%s\t%s\t%s\n' "$name" "$rc" "$sha" > "$out/matrix_row.tsv"
  echo "$name: exit $rc, sha256 $sha"
}
export -f run_one policy_of
export DATA_DIR WORK_ABS PY EVAL

if [[ ! -e "$WORK_ABS/cache/.built" ]]; then
  # build the caches once, before running in parallel
  "$PY" -m bessbid inspect --data-dir "$DATA_DIR" --work-dir "$WORK_ABS" > /dev/null && touch "$WORK_ABS/cache/.built"
fi
printf '%s\n' "${NAMES[@]}" | xargs -P "$JOBS" -I{} bash -c 'run_one "$@"' _ {}

{
  printf 'scenario\texit\tsubmission_sha256\n'
  for n in "${NAMES[@]}"; do cat "$WORK_ABS/$n/matrix_row.tsv"; done
} > "$WORK_ABS/scenario_matrix.tsv"
echo "Matrix: $WORK_ABS/scenario_matrix.tsv"
column -t -s $'\t' "$WORK_ABS/scenario_matrix.tsv" 2>/dev/null || cat "$WORK_ABS/scenario_matrix.tsv"
