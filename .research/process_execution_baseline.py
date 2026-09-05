"""Bounded local-only process measurements; no providers or network."""

import argparse
import importlib
import json
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--module", default="looplane.runtime")
parser.add_argument("--output", required=True)
args = parser.parse_args()
module = importlib.import_module(args.module)
run = getattr(module, "run_local_process", None) or module.run_bounded_command
cases = {
    "python_noop": ("pass", None, 4096),
    "dual_pipe_1mib": (
        "import os; os.write(1, b'x'*1048576); os.write(2, b'y'*1048576)",
        None,
        4096,
    ),
    "stdin_echo_1mib": (
        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        "x" * 1048576,
        4096,
    ),
}
report = {
    "module": args.module,
    "platform": platform.platform(),
    "python": sys.version,
    "repetitions": 15,
    "warmups": 2,
    "cases": {},
}
with tempfile.TemporaryDirectory(prefix="looplane-process-baseline-") as directory:
    for name, (script, stdin, bound) in cases.items():
        elapsed = []
        for iteration in range(17):
            start = time.perf_counter()
            result = run(
                (sys.executable, "-c", script),
                cwd=Path(directory),
                timeout_seconds=5,
                max_output_chars=bound,
                stdin=stdin,
            )
            duration = (time.perf_counter() - start) * 1000
            assert result.ok, result.stderr
            assert len(result.stdout.encode()) <= bound
            assert len(result.stderr.encode()) <= bound
            if iteration >= 2:
                elapsed.append(duration)
        report["cases"][name] = {
            "median_ms": statistics.median(elapsed),
            "min_ms": min(elapsed),
            "max_ms": max(elapsed),
            "samples_ms": elapsed,
            "stdout_bytes": result.stdout_bytes,
            "stderr_bytes": result.stderr_bytes,
            "retained_stdout_bytes": len(result.stdout.encode()),
            "retained_stderr_bytes": len(result.stderr.encode()),
        }
Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
print(
    json.dumps(
        {
            name: {key: value for key, value in values.items() if key != "samples_ms"}
            for name, values in report["cases"].items()
        },
        indent=2,
    )
)
