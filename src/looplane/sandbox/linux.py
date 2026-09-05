from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path

from looplane.sandbox.policy import _normalize_sandbox_roots


def _linux_bwrap_argv(
    argv: Sequence[str],
    cwd: Path,
    *,
    executable: str,
    read_roots: Sequence[Path],
    writable_roots: Sequence[Path],
) -> tuple[str, ...]:
    args: list[str] = [
        executable,
        "--die-with-parent",
        "--unshare-all",
        "--new-session",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
    ]
    for root in (
        Path("/usr"),
        Path("/bin"),
        Path("/lib"),
        Path("/lib64"),
        Path("/etc/ssl"),
        Path("/etc/ca-certificates"),
    ):
        args.extend(("--ro-bind-try", str(root), str(root)))
    normalized_write_roots = (*_normalize_sandbox_roots((cwd, *writable_roots), label="write"),)
    for root in normalized_write_roots:
        args.extend(("--bind", str(root), str(root)))
    for root in _normalize_sandbox_roots(read_roots, label="read"):
        if any(
            root == write_root or root.is_relative_to(write_root)
            for write_root in normalized_write_roots
        ):
            continue
        args.extend(("--ro-bind", str(root), str(root)))
    args.extend(("--chdir", str(cwd), "--", *argv))
    return tuple(args)


def _linux_landlock_argv(
    argv: Sequence[str],
    cwd: Path,
    *,
    read_roots: Sequence[Path],
    writable_roots: Sequence[Path],
) -> tuple[str, ...]:
    policy = json.dumps(
        {
            "cwd": str(cwd),
            "read_roots": [str(root) for root in read_roots],
            "writable_roots": [str(root) for root in writable_roots],
        },
        separators=(",", ":"),
    )
    wrapper = Path(__file__).with_name("landlock_run.py")
    return (sys.executable, str(wrapper), "--policy-json", policy, "--", *argv)
