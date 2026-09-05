#!/usr/bin/env bash
# Validate exactly the selected completed changes while other workers keep editing.
set -euo pipefail
workspace=/Users/xiaoxu/Projects/looplane
name=${1:?slice name required}
shift
if [ "$#" -eq 0 ]; then
  printf 'explicit scoped paths required\n' >&2
  exit 2
fi
case "$name" in
  *[!a-zA-Z0-9._-]*) printf 'invalid slice name\n' >&2; exit 2 ;;
esac
cd "$workspace"
out=$(mktemp -d "$workspace/.research/gate-${name}.XXXXXX")
baseline=$(mktemp -d "/tmp/looplane-${name}-base.XXXXXX")
snapshot=$(mktemp -d "/tmp/looplane-${name}-gate.XXXXXX")
printf '%s\n' "$out"
printf '%s\n' "$baseline" > "$out/baseline-path.txt"
printf '%s\n' "$snapshot" > "$out/snapshot-path.txt"
git rev-parse HEAD > "$out/base-revision.txt"
printf '%s\n' "$@" > "$out/scope.txt"
git archive HEAD | tar -x -C "$baseline"
cp -R "$baseline/." "$snapshot/"
git diff --binary HEAD -- "$@" > "$out/completed.patch"
if [ -s "$out/completed.patch" ]; then
  git -C "$snapshot" apply "$out/completed.patch"
fi
git ls-files --others --exclude-standard -z -- "$@" |
  tar --null -T - -cf - | tar -xf - -C "$snapshot"
export PATH="$workspace/.venv/bin:$PATH"
export PYTHONPATH="$snapshot/src"
cd "$snapshot"
ruff check . > "$out/ruff.log" 2>&1
python -m pytest -q -o addopts='' > "$out/pytest.log" 2>&1
uv build --wheel --sdist --out-dir "$out/build" > "$out/build.log" 2>&1
python scripts/check_distribution.py "$out/build" >> "$out/build.log" 2>&1
printf 'PASS %s\n' "$name"
tail -2 "$out/pytest.log"
tail -2 "$out/build.log"
printf 'Artifacts: %s\n' "$out"
