#!/usr/bin/env bash
#
# check_startup_regression.sh - Fail CI if startup got slower than baseline.
#
# Compares paired before/after startup benchmarks (hyperfine-shaped JSON as
# produced by bench_startup.sh) and exits non-zero when any scenario's median
# regressed beyond the allowed threshold.
#
# Usage:
#   check_startup_regression.sh BEFORE_JSON AFTER_JSON [THRESHOLD_PCT=10]
#
# M12 acceptance: a startup change must not regress the median by more than the
# threshold. Relative comparison only (no absolute time assertion), so it is
# portable across machines.

set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: $0 BEFORE_JSON AFTER_JSON [THRESHOLD_PCT=10]" >&2
  exit 2
fi

BEFORE="$1"
AFTER="$2"
THRESHOLD="${3:-10}"

if [ ! -f "$BEFORE" ]; then
  echo "baseline missing: $BEFORE (skip regression gate)" >&2
  exit 0
fi
if [ ! -f "$AFTER" ]; then
  echo "candidate missing: $AFTER" >&2
  exit 2
fi

python - "$BEFORE" "$AFTER" "$THRESHOLD" <<'PY'
import json, sys

before_path, after_path, threshold = sys.argv[1], sys.argv[2], float(sys.argv[3])

def load(path):
    with open(path) as fh:
        data = json.load(fh)
    return {r["command"]: r for r in data.get("results", [])}

a = load(before_path)
b = load(after_path)

failures = 0
checked = 0
for command, rb in b.items():
    if command not in a:
        continue
    ra = a[command]
    ma, mb = ra["median"], rb["median"]
    checked += 1
    if not ma:
        continue
    pct = (mb - ma) / ma * 100
    verdict = "SLOWER" if pct > 0 else "FASTER"
    print(f"{command!r}")
    print(f"  baseline median : {ma:.4f}s")
    print(f"  candidate median: {mb:.4f}s")
    print(f"  change          : {pct:+.1f}% ({verdict})")
    if pct > threshold:
        failures += 1
        print(f"  REGRESSION > {threshold:.0f}% threshold")

if checked == 0:
    print("no matching scenarios to compare", file=sys.stderr)
    sys.exit(0)

if failures:
    print(f"\n{failures} scenario(s) regressed beyond {threshold:.0f}%", file=sys.stderr)
    sys.exit(1)

print(f"\nno regression beyond {threshold:.0f}% across {checked} scenario(s)")
PY
