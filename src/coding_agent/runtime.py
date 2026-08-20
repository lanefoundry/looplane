from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_SAFE_ENV_KEYS = {
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "TZ",
}
_SENSITIVE_ENV_MARKERS = ("API", "AUTH", "CREDENTIAL", "GITHUB", "PASSWORD", "SECRET", "TOKEN")


class WorkspacePreparationError(RuntimeError):
    """Raised when a disposable Git workspace cannot be prepared safely."""


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.returncode == 0


def bounded_text(value: str, max_chars: int) -> str:
    """Bound UTF-8 output bytes while retaining useful content from both ends."""

    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    encoded = value.encode("utf-8")
    if len(encoded) <= max_chars:
        return value
    marker = f"\n... output truncated ({len(encoded) - max_chars} bytes omitted) ...\n"
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) >= max_chars:
        return marker_bytes[:max_chars].decode("utf-8", errors="ignore")
    available = max_chars - len(marker_bytes)
    head = available // 2
    tail = available - head
    prefix = encoded[:head].decode("utf-8", errors="ignore")
    suffix = encoded[-tail:].decode("utf-8", errors="ignore") if tail else ""
    return f"{prefix}{marker}{suffix}"


def sanitized_subprocess_env(*, task_home: Path | None = None) -> dict[str, str]:
    """Build a minimal environment that excludes host credentials and API secrets."""

    env = {key: value for key, value in os.environ.items() if key in _SAFE_ENV_KEYS}
    env["PATH"] = env.get("PATH", os.defpath)
    env.update(
        {
            "GIT_ASKPASS": "/usr/bin/false",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    if task_home is not None:
        task_home.mkdir(parents=True, exist_ok=True)
        task_tmp = task_home / "tmp"
        task_tmp.mkdir(parents=True, exist_ok=True)
        env["CODING_AGENT_TASK_HOME"] = str(task_home)
        env["XDG_CACHE_HOME"] = str(task_home / "cache")
        env["XDG_CONFIG_HOME"] = str(task_home / "config")
        env["PIP_CACHE_DIR"] = str(task_home / "pip-cache")
        env["UV_CACHE_DIR"] = str(task_home / "uv-cache")
        env["TMPDIR"] = str(task_tmp)
    assert not any(marker in key.upper() for key in env for marker in _SENSITIVE_ENV_MARKERS)
    return env


class _BoundedCapture:
    """Drain a pipe fully while retaining only bounded head and tail bytes."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self._head_limit = max_bytes // 2
        self._tail_limit = max_bytes - self._head_limit
        self._head = bytearray()
        self._tail = bytearray()
        self.total_bytes = 0

    def add(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        remaining_head = self._head_limit - len(self._head)
        if remaining_head > 0:
            self._head.extend(chunk[:remaining_head])
            chunk = chunk[remaining_head:]
        if chunk and self._tail_limit:
            self._tail.extend(chunk)
            if len(self._tail) > self._tail_limit:
                del self._tail[: len(self._tail) - self._tail_limit]

    @property
    def truncated(self) -> bool:
        return self.total_bytes > self.max_bytes

    def text(self) -> str:
        if not self.truncated:
            return bytes(self._head + self._tail).decode("utf-8", errors="replace")
        marker = f"\n... output truncated ({self.total_bytes - self.max_bytes} bytes omitted) ...\n"
        marker_bytes = marker.encode("utf-8")
        available = max(0, self.max_bytes - len(marker_bytes))
        head_size = available // 2
        tail_size = available - head_size
        payload = bytes(self._head[:head_size]) + marker_bytes
        if tail_size:
            payload += bytes(self._tail[-tail_size:])
        return payload[: self.max_bytes].decode("utf-8", errors="replace")


def _drain_pipe(pipe: object, capture: _BoundedCapture) -> None:
    try:
        while True:
            chunk = pipe.read(64 * 1024)  # type: ignore[attr-defined]
            if not chunk:
                return
            capture.add(chunk)
    finally:
        pipe.close()  # type: ignore[attr-defined]


def _write_stdin(pipe: object, value: str) -> None:
    try:
        pipe.write(value.encode("utf-8"))  # type: ignore[attr-defined]
        pipe.flush()  # type: ignore[attr-defined]
    except (BrokenPipeError, OSError):
        pass
    finally:
        pipe.close()  # type: ignore[attr-defined]


def _signal_process_group(process: subprocess.Popen[bytes], sig: int) -> None:
    if os.name == "posix":
        with suppress(ProcessLookupError):
            os.killpg(process.pid, sig)
        return
    if process.poll() is None:
        if sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()


def _stop_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Best-effort process-tree cleanup, with a real process group on POSIX."""

    _signal_process_group(process, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=0.5)
    # The group may still contain grandchildren even if the leader exited.
    _signal_process_group(process, getattr(signal, "SIGKILL", signal.SIGTERM))
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_bounded_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    max_output_chars: int,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
) -> CommandResult:
    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise ValueError("argv must be a non-empty sequence of non-empty strings")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(env) if env is not None else sanitized_subprocess_env(),
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=os.name == "posix",
        creationflags=creationflags,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_capture = _BoundedCapture(max_output_chars)
    stderr_capture = _BoundedCapture(max_output_chars)
    readers = (
        threading.Thread(
            target=_drain_pipe,
            args=(process.stdout, stdout_capture),
            daemon=True,
        ),
        threading.Thread(
            target=_drain_pipe,
            args=(process.stderr, stderr_capture),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()
    if stdin is not None:
        assert process.stdin is not None
        threading.Thread(target=_write_stdin, args=(process.stdin, stdin), daemon=True).start()

    timed_out = False
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _stop_process_tree(process)
        returncode = 124
    else:
        # Do not leave background descendants holding pipes or mutating the host.
        if os.name == "posix":
            _stop_process_tree(process)
    for reader in readers:
        reader.join(timeout=1.0)
    if any(reader.is_alive() for reader in readers):
        _stop_process_tree(process)
        for pipe in (process.stdout, process.stderr):
            with suppress(OSError):
                pipe.close()
        for reader in readers:
            reader.join(timeout=1.0)

    return CommandResult(
        argv=tuple(argv),
        returncode=returncode,
        stdout=stdout_capture.text(),
        stderr=stderr_capture.text(),
        timed_out=timed_out,
        stdout_bytes=stdout_capture.total_bytes,
        stderr_bytes=stderr_capture.total_bytes,
        stdout_truncated=stdout_capture.truncated,
        stderr_truncated=stderr_capture.truncated,
    )


@dataclass
class LocalGitWorkspace:
    source_repo: Path
    run_dir: Path
    base_sha: str
    workspace_name: str = "workspace"
    git_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        self.source_repo = Path(self.source_repo).resolve(strict=False)
        self.run_dir = Path(self.run_dir).resolve(strict=False)
        if not self.workspace_name or Path(self.workspace_name).name != self.workspace_name:
            raise ValueError("workspace_name must be one path segment")
        if not _FULL_SHA.fullmatch(self.base_sha):
            raise ValueError("base_sha must be a full 40-character Git commit SHA")

    @property
    def workspace_path(self) -> Path:
        return self.run_dir / self.workspace_name

    def _git(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        return run_bounded_command(
            ("git", *argv),
            cwd=cwd,
            timeout_seconds=min(self.git_timeout_seconds, timeout_seconds)
            if timeout_seconds is not None
            else self.git_timeout_seconds,
            max_output_chars=20_000,
            env=sanitized_subprocess_env(task_home=self.run_dir / ".task-env"),
        )

    def prepare(self, *, timeout_seconds: float | None = None) -> Path:
        deadline = (
            time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        )

        def remaining() -> float | None:
            if deadline is None:
                return None
            value = deadline - time.monotonic()
            if value <= 0:
                raise WorkspacePreparationError(
                    "workspace preparation exceeded the harness timeout"
                )
            return value

        source = self.source_repo.resolve(strict=True)
        if not source.is_dir():
            raise WorkspacePreparationError(f"source repository is not a directory: {source}")

        run_dir = self.run_dir.resolve(strict=False)
        try:
            run_dir.relative_to(source)
        except ValueError:
            pass
        else:
            raise WorkspacePreparationError("run_dir must not be inside the source repository")

        if self.workspace_path.exists():
            raise WorkspacePreparationError(f"workspace already exists: {self.workspace_path}")
        self.run_dir.mkdir(parents=True, exist_ok=True)

        resolved = self._git(
            ("-C", str(source), "rev-parse", "--verify", f"{self.base_sha}^{{commit}}"),
            cwd=self.run_dir,
            timeout_seconds=remaining(),
        )
        if not resolved.ok or resolved.stdout.strip().lower() != self.base_sha.lower():
            raise WorkspacePreparationError(
                "base_sha is not an exact commit in the source repository"
            )

        cloned = self._git(
            (
                "clone",
                "--no-hardlinks",
                "--no-checkout",
                "--",
                str(source),
                str(self.workspace_path),
            ),
            cwd=self.run_dir,
            timeout_seconds=remaining(),
        )
        if not cloned.ok:
            raise WorkspacePreparationError(f"git clone failed: {cloned.stderr.strip()}")

        checked_out = self._git(
            ("checkout", "--detach", self.base_sha),
            cwd=self.workspace_path,
            timeout_seconds=remaining(),
        )
        if not checked_out.ok:
            raise WorkspacePreparationError(f"git checkout failed: {checked_out.stderr.strip()}")
        head = self._git(
            ("rev-parse", "HEAD"),
            cwd=self.workspace_path,
            timeout_seconds=remaining(),
        )
        if not head.ok or head.stdout.strip().lower() != self.base_sha.lower():
            raise WorkspacePreparationError("disposable workspace HEAD does not match base_sha")
        return self.workspace_path
