"""Paired local startup measurements with one interpreter and dependency set."""

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

out = Path(__file__).resolve().parent
candidate = out.parents[1]
baseline = Path((out / "baseline-path.txt").read_text().strip())
python = str(Path(sys.executable))
entry = str(Path(sys.executable).parent / "looplane")
scenarios = {
    "looplane --help": [entry, "--help"],
    "looplane config": [entry, "config"],
    "import looplane.tui": [python, "-c", "import looplane.tui"],
}
results = {"before": [], "after": []}
for name, command in scenarios.items():
    samples = {"before": [], "after": []}
    for iteration in range(18):
        pairs = [("before", baseline), ("after", candidate)]
        if iteration % 2:
            pairs.reverse()
        for label, root in pairs:
            env = dict(os.environ, PYTHONPATH=str(root / "src"))
            start = time.perf_counter()
            subprocess.run(
                command, cwd=root, env=env, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            elapsed = time.perf_counter() - start
            if iteration >= 3:
                samples[label].append(elapsed)
    for label in results:
        results[label].append({
            "command": name,
            "median": statistics.median(samples[label]),
            "times": samples[label],
            "runs": len(samples[label]),
        })
for label, rows in results.items():
    (out / f"startup-{label}.json").write_text(json.dumps({"results": rows}, indent=2) + "\n")
print("Paired 15-sample medians recorded for help, config, and TUI import.")
