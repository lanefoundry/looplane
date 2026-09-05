from __future__ import annotations

import shutil
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from looplane.sandbox.landlock_run import landlock_available
from looplane.sandbox.linux import _linux_bwrap_argv, _linux_landlock_argv
from looplane.sandbox.macos import _macos_sandbox_profile
from looplane.sandbox.policy import CommandSandbox


def sandboxed_command_argv(
    argv: Sequence[str],
    *,
    cwd: Path,
    sandbox: CommandSandbox | None,
    _landlock_probe: Callable[[], bool] | None = None,
) -> tuple[str, ...] | str:
    """Return argv wrapped in an OS sandbox, or an error string when unavailable."""

    if sandbox is None:
        return tuple(argv)
    if sandbox.mode != "workspace-write":
        raise ValueError("unsupported command sandbox mode")
    if sandbox.profile != "verification":
        raise ValueError("unsupported command sandbox profile")
    if sandbox.backend not in {"auto", "bubblewrap", "landlock"}:
        raise ValueError("unsupported command sandbox backend")
    if sys.platform == "darwin":
        if sandbox.backend not in {"auto"}:
            raise ValueError("unsupported command sandbox backend on macOS")
        executable = shutil.which("sandbox-exec")
        if executable is None:
            return "macOS sandbox-exec is unavailable"
        profile = _macos_sandbox_profile(
            cwd,
            read_roots=sandbox.read_roots,
            writable_roots=sandbox.writable_roots,
        )
        return (executable, "-p", profile, *argv)
    if sys.platform.startswith("linux"):
        if sandbox.backend in {"auto", "bubblewrap"}:
            executable = shutil.which("bwrap")
            if executable is not None:
                return _linux_bwrap_argv(
                    argv,
                    cwd,
                    executable=executable,
                    read_roots=sandbox.read_roots,
                    writable_roots=sandbox.writable_roots,
                )
            if sandbox.backend == "bubblewrap":
                return "Linux bubblewrap sandbox is unavailable"
        if sandbox.backend == "landlock" and not (_landlock_probe or landlock_available)():
            return "Linux landlock sandbox is unavailable"
        if sandbox.backend == "auto" and not (_landlock_probe or landlock_available)():
            return "Linux command sandbox is unavailable on this kernel"
        return _linux_landlock_argv(
            argv,
            cwd,
            read_roots=sandbox.read_roots,
            writable_roots=sandbox.writable_roots,
        )
    return "OS command sandbox is unavailable on this platform"
