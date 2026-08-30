#!/usr/bin/env python3
"""M13 live-capture harness for OpenCode / Pi / OMP.

Why this exists
---------------
The OpenCode/Pi/OMP normalizers in ``src/looplane/*_backend.py`` are deliberately permissive
placeholders; their field names are assumptions until proven against a real capture. This script
runs each *installed* CLI exactly as the backend would (same argv via ``_argv``), in a throwaway
temp workspace, on a read-only low-cost task, and records:

  .artifacts/m13-captures/<runtime>.jsonl      raw stdout (the JSON event stream)
  .artifacts/m13-captures/<runtime>.stderr.txt captured stderr
  .artifacts/m13-captures/<runtime>.meta.json  argv, returncode, model, timestamp
  .artifacts/m13-captures/<runtime>.normalized.json  events the current normalizer produced

This is the evidence Slice 2/3/4 + the M13 stage report require. It does NOT require the CLIs to
be installed (skipped with a note) and runs each once.

SAFETY
------
* Runs only CLIs resolvable on PATH (skips otherwise) — never fabricates executables.
* Uses a read-only instruction ("list the files here") in a temporary directory.
* Still: a real CLI may need a login and may cost tokens / call tools. Review before running on
  a machine with a paid login. Prefer a free/local-model login for the first capture.
* If a flag is wrong (e.g. ``pi --mode json``), the subprocess fails and we capture stderr —
  that error text is itself the signal to fix the argv/normalizer.

Usage
-----
  python scripts/m13_capture_runtimes.py                # all installed runtimes
  python scripts/m13_capture_runtimes.py --runtime pi   # one runtime
  python scripts/m13_capture_runtimes.py --task "..."    # override the instruction
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from looplane.omp_backend import OmpBackend
from looplane.opencode_backend import OpenCodeBackend
from looplane.pi_backend import PiBackend

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / ".artifacts" / "m13-captures"

DEFAULT_TASK = (
    "List the files in the current directory and reply with only their names, nothing else."
)

BACKENDS = {
    "opencode": OpenCodeBackend,
    "pi": PiBackend,
    "omp": OmpBackend,
}


def capture_one(name: str, task: str, timeout: int, model: str | None = None) -> dict:
    backend_cls = BACKENDS[name]
    backend = backend_cls(model=model)
    executable = backend.executable
    resolved = shutil.which(executable)
    record: dict = {"runtime": name, "executable": executable, "resolved": resolved}
    if resolved is None:
        record["status"] = "skipped_no_executable"
        print(f"[skip] {name}: executable '{executable}' not on PATH")
        return record

    argv = list(backend._argv(resolved, task))
    record["argv"] = argv
    out_path = OUT_DIR / f"{name}.jsonl"
    stderr_path = OUT_DIR / f"{name}.stderr.txt"
    meta_path = OUT_DIR / f"{name}.meta.json"
    normalized_path = OUT_DIR / f"{name}.normalized.json"

    with tempfile.TemporaryDirectory(prefix=f"m13-cap-{name}-") as work:
        env = dict(__import__("os").environ)
        env["GIT_ASKPASS"] = "/usr/bin/false"
        env["GIT_TERMINAL_PROMPT"] = "0"
        started = time.time()
        try:
            proc = subprocess.run(
                argv,
                cwd=work,
                env=env,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
            )
            status = "ok" if proc.returncode == 0 else "nonzero_exit"
        except subprocess.TimeoutExpired as exc:
            status = "timeout"
            proc = exc  # type: ignore[assignment]
        except Exception as exc:  # pragma: no cover - defensive
            record["status"] = "error"
            record["error"] = repr(exc)
            print(f"[error] {name}: {exc!r}")
            return record
        elapsed = time.time() - started

        stdout = getattr(proc, "stdout", "") or ""
        stderr = getattr(proc, "stderr", "") or ""
        returncode = getattr(proc, "returncode", None)

        out_path.write_text(stdout)
        stderr_path.write_text(stderr)
        record.update(
            {
                "status": status,
                "returncode": returncode,
                "elapsed_s": round(elapsed, 2),
                "stdout_bytes": len(stdout.encode("utf-8")),
                "stderr_bytes": len(stderr.encode("utf-8")),
                "artifacts": {
                    "stdout": str(out_path),
                    "stderr": str(stderr_path),
                    "normalized": str(normalized_path),
                },
            }
        )

        events, malformed = backend._normalize(stdout)
        normalized_path.write_text(
            json.dumps(
                {
                    "malformed_stream": malformed,
                    "event_count": len(events),
                    "events": [e.model_dump(mode="json") for e in events],
                },
                indent=2,
            )
        )
        record["normalized_event_count"] = len(events)
        record["malformed_stream"] = malformed
        meta_path.write_text(json.dumps(record, indent=2))

        kinds = {}
        for e in events:
            kinds[e.event_type] = kinds.get(e.event_type, 0) + 1
        print(
            f"[{status}] {name}: rc={returncode} events={len(events)} "
            f"({kinds}) malformed={malformed} -> {out_path.name}"
        )
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description="M13 live capture for OpenCode/Pi/OMP")
    parser.add_argument("--runtime", choices=sorted(BACKENDS), default=None)
    parser.add_argument("--task", default=DEFAULT_TASK)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument(
        "--model",
        default=None,
        help="Free/local model id passed to the backend (e.g. openrouter/<id>:free, hy3-free).",
    )
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    names = [args.runtime] if args.runtime else sorted(BACKENDS)
    summary = [capture_one(name, args.task, args.timeout, args.model) for name in names]
    (OUT_DIR / "_summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\nWrote summary -> {OUT_DIR / '_summary.json'}")


if __name__ == "__main__":
    main()
