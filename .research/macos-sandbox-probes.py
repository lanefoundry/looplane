"""Diagnosis-only Seatbelt profiles; never modifies production policy."""

import json
import platform
import subprocess
import sys
from pathlib import Path

from looplane.execution.environment import sanitized_subprocess_env
from looplane.sandbox.macos import _macos_sandbox_profile
from looplane.sandbox.policy import python_runtime_read_roots

out = Path(".research/macos-sandbox-evidence").resolve()
out.mkdir(exist_ok=True)
workspace = out / "workspace"
workspace.mkdir(exist_ok=True)
base = _macos_sandbox_profile(workspace, read_roots=python_runtime_read_roots(), writable_roots=())
variants = {
    "original": "",
    "root_metadata": '(allow file-read-metadata (literal "/"))',
    "root_xattr": '(allow file-read-xattr (literal "/"))',
    "root_data": '(allow file-read-data (literal "/"))',
}
report = {"platform": platform.platform(), "python": sys.version, "cases": []}
for name, addition in variants.items():
    profile = out / f"{name}.sb"
    profile.write_text(base + "\n" + addition + "\n")
    for target, command in [
        ("sh", ["/bin/sh", "-c", "printf ready"]),
        ("python", [sys.executable, "-c", 'print("ready")']),
    ]:
        argv = ["/usr/bin/sandbox-exec", "-f", str(profile), *command]
        proc = subprocess.Popen(
            argv,
            cwd=workspace,
            env=sanitized_subprocess_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = proc.communicate(timeout=5)
        record = {
            "profile": name,
            "target": target,
            "pid": proc.pid,
            "argv": argv,
            "returncode": proc.returncode,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
        }
        report["cases"].append(record)
        print(json.dumps(record))
(out / "profile-matrix.json").write_text(json.dumps(report, indent=2) + "\n")
