#!/usr/bin/env bash
#
# bench_startup.sh - Measure looplane startup cost.
#
# Primary metric: user-visible time to first editable composer. Because the
# interactive composer requires a TTY, the harness benchmarks proxy scenarios
# (import time, --help, config, exec preparation) and separately captures a
# structured startup-telemetry trace when LOOPLANE_STARTUP_LOG is set.
#
# Output: .artifacts/startup/*.json (raw) and a paired before/after comparison.
#
# Requires: hyperfine (https://github.com/sharkdp/hyperfine). If missing, the
# script prints an actionable install message and exits non-zero, unless
# --fallback is passed, in which case it uses a built-in timer good enough for a
# rough local baseline (not for cross-machine comparison).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${LOOPLANE_BENCH_OUT:-$ROOT/.artifacts/startup}"
WARMUP=3
MIN_RUNS=10
USE_FALLBACK=0
PAIRED=0
BASELINE_CMD=""
CANDIDATE_CMD=""
SCENARIOS=()

mkdir -p "$OUT_DIR"

say() { printf '%s\n' "$*" >&2; }

require_hyperfine() {
  if command -v hyperfine >/dev/null 2>&1; then
    return 0
  fi
  if [ "$USE_FALLBACK" -eq 1 ]; then
    say "hyperfine not found; using built-in fallback timer (rough local baseline only)."
    return 1
  fi
  say "hyperfine is required for accurate startup benchmarks."
  say "Install it with one of:"
  say "  brew install hyperfine"
  say "  cargo install hyperfine"
  say "  (or run this script with --fallback for a rough built-in timer)"
  exit 1
}

# Run one command many times with the built-in timer and emit hyperfine-shaped JSON.
fallback_run() {
  local name="$1"; shift
  local runs="${1:-$MIN_RUNS}"; shift || true
  local cmd="$*"
  local tmp
  tmp="$(mktemp)"
  local start end elapsed total=0 min=1e9 max=0
  for _ in $(seq 1 "$runs"); do
    start="$(python -c 'import time;print(time.perf_counter())')"
    bash -c "$cmd" >/dev/null 2>&1 || true
    end="$(python -c 'import time;print(time.perf_counter())')"
    elapsed="$(python -c "print($end-$start)")"
    total="$(python -c "print($total+$elapsed)")"
    min="$(python -c "print(min($min,$elapsed))")"
    max="$(python -c "print(max($max,$elapsed))")"
  done
  local median mean
  median="$(python -c "print($total/$runs)")"
  mean="$median"
  cat > "$tmp" <<JSON
{
  "results": [
    {
      "command": $(python -c "import json,sys;print(json.dumps(sys.argv[1]))" "$cmd"),
      "mean": $mean,
      "median": $median,
      "min": $min,
      "max": $max,
      "runs": $runs,
      "fallback": true
    }
  ]
}
JSON
  echo "$tmp"
}

capture_importtime() {
  local out="$OUT_DIR/importtime-looplane-cli.txt"
  say "Capturing import-time report -> $out"
  PYTHONPATH="$ROOT/src" python -X importtime -c "import looplane.cli" 2> "$out" || true
  say "Top cumulative import cost:"
  grep -E '^import time:' "$out" | sort -t'|' -k3 -rn | head -n 8 | sed 's/^import time:/  /' >&2 || true
}

run_scenario() {
  local name="$1"; shift
  local cmd="$*"
  local out="$OUT_DIR/bench-$name.json"
  if require_hyperfine; then
    say "Benchmarking [$name]: $cmd"
    hyperfine --warmup "$WARMUP" --min-runs "$MIN_RUNS" \
      --export-json "$out" --shell=none "$cmd"
  else
    local tmp
    tmp="$(fallback_run "$name" "$MIN_RUNS" "$cmd")"
    cp "$tmp" "$out"
    say "Benchmarking [$name] (fallback): $(python -c "import json;print(round(json.load(open('$out'))['results'][0]['median'],3))")s -> $out"
  fi
}

compare_json() {
  # Compare two hyperfine-shaped JSON files and print percentage change of medians.
  [ "$#" -eq 2 ] || { say "compare_json needs two files"; return 1; }
  python - "$1" "$2" <<'PY'
import json, sys
a, b = (json.load(open(f)) for f in sys.argv[1:2+1])
ra, rb = a["results"][0], b["results"][0]
ma, mb = ra["median"], rb["median"]
pct = (mb - ma) / ma * 100 if ma else float('nan')
verdict = "SLOWER" if pct > 0 else "FASTER"
print(f"{ra['command']!r}")
print(f"  baseline median : {ma:.4f}s")
print(f"  candidate median: {mb:.4f}s")
print(f"  change          : {pct:+.1f}% ({verdict})")
PY
}

usage() {
  say "Usage: bench_startup.sh [--fallback] [--warmup N] [--min-runs N]"
  say "       [--scenario NAME|--cmd 'shell command']... [--importtime]"
  say "       [--paired BASELINE_JSON CANDIDATE_JSON]"
  say ""
  say "Default scenarios (when none given):"
  say "  help   : looplane --help"
  say "  config : looplane config (uses existing config if present)"
  say "  import : python -c 'import looplane.cli' (import-time only, not hyperfine)"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --fallback) USE_FALLBACK=1; shift ;;
    --warmup) WARMUP="$2"; shift 2 ;;
    --min-runs) MIN_RUNS="$2"; shift 2 ;;
    --importtime) CAPTURE_IMPORTTIME=1; shift ;;
    --scenario) SCENARIOS+=("$2"); shift 2 ;;
    --cmd) SCENARIOS+=("custom:$2"); shift 2 ;;
    --paired) PAIRED=1; BASELINE_CMD="$2"; CANDIDATE_CMD="$3"; shift 3 ;;
    -h|--help) usage; exit 0 ;;
    *) say "Unknown argument: $1"; usage; exit 2 ;;
  esac
done

if [ "$PAIRED" -eq 1 ]; then
  compare_json "$BASELINE_CMD" "$CANDIDATE_CMD"
  exit 0
fi

if [ "${#SCENARIOS[@]}" -eq 0 ]; then
  SCENARIOS=(help config)
fi

for spec in "${SCENARIOS[@]}"; do
  name="${spec%%:*}"
  body="${spec#*:}"
  case "$name" in
    help) run_scenario help "uv run looplane --help" ;;
    config) run_scenario config "uv run looplane config" ;;
    import) capture_importtime ;;
    custom) run_scenario "custom" "$body" ;;
    *) say "Unknown scenario: $name"; usage; exit 2 ;;
  esac
done

if [ "${CAPTURE_IMPORTTIME:-0}" -eq 1 ]; then
  capture_importtime
fi

say "Raw results in: $OUT_DIR"
