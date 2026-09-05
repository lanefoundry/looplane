"""Compatibility facade for local processes, sandbox requests and Git workspaces.

Canonical modules never import this facade. Explicit call forwarding keeps the
legacy sandbox probe, environment and process termination monkeypatch surfaces.
"""

from __future__ import annotations

import os as os
import shutil as shutil
import signal as signal
import subprocess
import sys as sys
import threading
import time as time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from looplane.execution import local_process as _process
from looplane.execution.capture import (
    _BoundedCapture as _BoundedCapture,
)
from looplane.execution.capture import (
    _drain_pipe as _drain_pipe,
)
from looplane.execution.capture import (
    _drain_stdout_lines as _drain_stdout_lines,
)
from looplane.execution.capture import (
    _write_stdin as _write_stdin,
)
from looplane.execution.capture import (
    bounded_text as bounded_text,
)
from looplane.execution.environment import (
    _SAFE_ENV_KEYS as _SAFE_ENV_KEYS,
)
from looplane.execution.environment import (
    _SENSITIVE_ENV_MARKERS as _SENSITIVE_ENV_MARKERS,
)
from looplane.execution.environment import (
    sanitized_subprocess_env as sanitized_subprocess_env,
)
from looplane.execution.local_process import _signal_process_group as _signal_process_group
from looplane.execution.types import DEFAULT_MAX_STDIN_BYTES as DEFAULT_MAX_STDIN_BYTES
from looplane.execution.types import CommandResult as CommandResult
from looplane.landlock_run import landlock_available as landlock_available
from looplane.sandbox import launcher as _launcher
from looplane.sandbox import policy as _policy
from looplane.sandbox.linux import (
    _linux_bwrap_argv as _linux_bwrap_argv,
)
from looplane.sandbox.linux import (
    _linux_landlock_argv as _linux_landlock_argv,
)
from looplane.sandbox.macos import (
    _macos_sandbox_profile as _macos_sandbox_profile,
)
from looplane.sandbox.macos import (
    _sandbox_path_literal as _sandbox_path_literal,
)
from looplane.sandbox.policy import (
    CommandSandbox as CommandSandbox,
)
from looplane.sandbox.policy import (
    _normalize_sandbox_roots as _normalize_sandbox_roots,
)
from looplane.sandbox.policy import (
    python_runtime_read_roots as python_runtime_read_roots,
)
from looplane.workspace.local_git import (
    _FULL_SHA as _FULL_SHA,
)
from looplane.workspace.local_git import (
    LocalGitWorkspace as _LocalGitWorkspace,
)
from looplane.workspace.local_git import (
    WorkspacePreparationError as WorkspacePreparationError,
)


def _stop_process_tree(process: subprocess.Popen[bytes]) -> None:
    _process._stop_process_tree(process, _signal_group=_signal_process_group)


def sandboxed_command_argv(
    argv: Sequence[str],
    *,
    cwd: Path,
    sandbox: CommandSandbox | None,
) -> tuple[str, ...] | str:
    return _launcher.sandboxed_command_argv(
        argv,
        cwd=cwd,
        sandbox=sandbox,
        _landlock_probe=landlock_available,
    )


def resolve_command_sandbox(
    *,
    profile: str | None = "verification",
    backend: str | None = "auto",
    cwd: Path,
    task_home: Path,
    extra_read_roots: Sequence[Path] = (),
) -> CommandSandbox:
    return _policy.resolve_command_sandbox(
        profile=profile,
        backend=backend,
        cwd=cwd,
        task_home=task_home,
        extra_read_roots=extra_read_roots,
        _runtime_read_roots=python_runtime_read_roots,
    )


def run_bounded_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    max_output_chars: int,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
    max_stdin_bytes: int = DEFAULT_MAX_STDIN_BYTES,
    cancel_event: threading.Event | None = None,
    stdout_line_callback: Callable[[str, bool], None] | None = None,
    max_stdout_line_bytes: int | None = None,
    sandbox: CommandSandbox | None = None,
) -> CommandResult:
    return _process.run_local_process(
        argv,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
        env=env,
        stdin=stdin,
        max_stdin_bytes=max_stdin_bytes,
        cancel_event=cancel_event,
        stdout_line_callback=stdout_line_callback,
        max_stdout_line_bytes=max_stdout_line_bytes,
        sandbox=sandbox,
        _sandbox_launcher=sandboxed_command_argv,
        _stop_process=_stop_process_tree,
        _env_factory=sanitized_subprocess_env,
    )


run_local_process = _process.run_local_process


class LocalGitWorkspace(_LocalGitWorkspace):
    """Legacy workspace path retaining run_bounded_command interception."""

    @staticmethod
    def _run_command(*args, **kwargs) -> CommandResult:
        return run_bounded_command(*args, **kwargs)
