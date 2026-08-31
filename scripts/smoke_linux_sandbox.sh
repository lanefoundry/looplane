#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Linux sandbox smoke skipped: host is not Linux"
  exit 0
fi

root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT

workspace="$root/workspace"
task_home="$root/task-home"
outside="$root/outside"
mkdir -p "$workspace" "$task_home" "$outside"
printf 'readable\n' > "$workspace/input.txt"

policy="$(python - "$workspace" "$task_home" <<'PY'
import json
import sys

workspace, task_home = sys.argv[1], sys.argv[2]
print(json.dumps({
    "cwd": workspace,
    "read_roots": [workspace, task_home],
    "writable_roots": [task_home],
}, separators=(",", ":")))
PY
)"

uv run python src/looplane/landlock_run.py --policy-json "$policy" -- \
  python - "$task_home" <<'PY'
import os
from pathlib import Path
import sys

Path("input.txt").read_text()
Path(sys.argv[1], "ok.txt").write_text("ok")
with open(os.devnull, encoding="utf-8") as handle:
    handle.read()
with open(os.devnull, "w", encoding="utf-8") as handle:
    handle.write("discarded")
PY

if uv run python src/looplane/landlock_run.py --policy-json "$policy" -- \
  python - "$outside/should-not-exist" <<'PY'
from pathlib import Path
import sys

Path(sys.argv[1]).write_text("escaped")
PY
then
  echo "Linux sandbox smoke failed: write outside policy unexpectedly succeeded" >&2
  exit 1
fi

test ! -e "$outside/should-not-exist"
echo "Linux Landlock/seccomp smoke passed"
