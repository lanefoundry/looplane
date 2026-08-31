from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from looplane.landlock_run import landlock_available

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
class CommandSandbox:
    """OS-level command sandbox request for local verification commands."""

    mode: str
    profile: str = "verification"
    backend: str = "auto"
    read_roots: tuple[Path, ...] = ()
    writable_roots: tuple[Path, ...] = ()


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


def python_runtime_read_roots() -> tuple[Path, ...]:
    """Return narrow trusted roots required by this Python sandbox wrapper."""

    roots: list[Path] = []
    home = Path.home().resolve(strict=False)
    for value in (sys.prefix, sys.base_prefix):
        root = Path(value).resolve(strict=False)
        if root == Path(root.anchor) or home == root or home.is_relative_to(root):
            continue
        roots.append(root)
    return tuple(dict.fromkeys(roots))


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


def _drain_stdout_lines(
    pipe: object,
    capture: _BoundedCapture,
    callback: Callable[[str, bool], None],
    max_line_bytes: int,
) -> None:
    """Drain stdout while delivering complete, independently bounded lines.

    The full pipe is always drained even when a line or the total capture exceeds
    its retention bound. Callback failures are isolated from pipe draining so a
    UI consumer cannot deadlock the child process.
    """

    retained = bytearray()
    line_truncated = False

    def append(segment: bytes) -> None:
        nonlocal line_truncated
        remaining = max_line_bytes - len(retained)
        if remaining > 0:
            retained.extend(segment[:remaining])
        if len(segment) > remaining:
            line_truncated = True

    def emit() -> None:
        nonlocal line_truncated
        payload = bytes(retained)
        if payload.endswith(b"\r"):
            payload = payload[:-1]
        # The command runner owns process cleanup and must continue draining
        # even if an observational callback is faulty.
        with suppress(Exception):
            callback(payload.decode("utf-8", errors="replace"), line_truncated)
        retained.clear()
        line_truncated = False

    try:
        while True:
            # BufferedReader.read(size) may wait for the requested size or EOF;
            # os.read returns as soon as pipe bytes are available.
            chunk = os.read(pipe.fileno(), 64 * 1024)  # type: ignore[attr-defined]
            if not chunk:
                if retained or line_truncated:
                    emit()
                return
            capture.add(chunk)
            start = 0
            while True:
                newline = chunk.find(b"\n", start)
                if newline < 0:
                    append(chunk[start:])
                    break
                append(chunk[start:newline])
                emit()
                start = newline + 1
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


def _sandbox_path_literal(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def _normalize_sandbox_roots(roots: Sequence[Path], *, label: str) -> tuple[Path, ...]:
    normalized: list[Path] = []
    for root in roots:
        resolved = Path(root).expanduser().resolve(strict=False)
        if "\x00" in str(resolved):
            raise ValueError(f"sandbox {label} roots must be NUL-free")
        normalized.append(resolved)
    return tuple(dict.fromkeys(normalized))


def resolve_command_sandbox(
    *,
    profile: str | None = "verification",
    backend: str | None = "auto",
    cwd: Path,
    task_home: Path,
    extra_read_roots: Sequence[Path] = (),
) -> CommandSandbox:
    """Build a normalized verification sandbox request."""

    profile_name = profile or "verification"
    if profile_name != "verification":
        raise ValueError(f"unsupported command sandbox profile: {profile_name}")
    backend_name = backend or "auto"
    if backend_name not in {"auto", "bubblewrap", "landlock"}:
        raise ValueError(f"unsupported command sandbox backend: {backend_name}")
    task_home = task_home.expanduser().resolve(strict=False)
    read_roots = _normalize_sandbox_roots(
        (cwd, task_home, *extra_read_roots, *python_runtime_read_roots()),
        label="read",
    )
    return CommandSandbox(
        mode="workspace-write",
        profile=profile_name,
        backend=backend_name,
        read_roots=read_roots,
        writable_roots=(task_home,),
    )


def _macos_sandbox_profile(
    cwd: Path,
    *,
    read_roots: Sequence[Path],
    writable_roots: Sequence[Path],
) -> str:
    write_roots = []
    for root in (cwd, *writable_roots):
        resolved = Path(root).resolve(strict=False)
        if "\x00" in str(resolved):
            raise ValueError("sandbox writable roots must be NUL-free")
        write_roots.append(resolved)
    read_roots = (
        *_normalize_sandbox_roots((cwd, *read_roots, *writable_roots), label="read"),
        Path("/System"),
        Path("/Library"),
        Path("/usr"),
        Path("/bin"),
        Path("/sbin"),
        Path("/private/var/folders"),
        Path("/private/tmp"),
        Path("/tmp"),
    )
    read_rules = "\n".join(
        f'  (subpath "{_sandbox_path_literal(root)}")' for root in dict.fromkeys(read_roots)
    )
    writable_rules = "\n".join(
        f'  (subpath "{_sandbox_path_literal(root)}")' for root in dict.fromkeys(write_roots)
    )
    return f"""
(version 1)
(deny default)
(allow process*)
(allow sysctl-read)
(allow file-read-metadata)
(allow file-read*
{read_rules}
)
(allow file-write*
{writable_rules}
)
""".strip()


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


def sandboxed_command_argv(
    argv: Sequence[str],
    *,
    cwd: Path,
    sandbox: CommandSandbox | None,
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
        if sandbox.backend == "landlock" and not landlock_available():
            return "Linux landlock sandbox is unavailable"
        if sandbox.backend == "auto" and not landlock_available():
            return "Linux command sandbox is unavailable on this kernel"
        return _linux_landlock_argv(
            argv,
            cwd,
            read_roots=sandbox.read_roots,
            writable_roots=sandbox.writable_roots,
        )
    return "OS command sandbox is unavailable on this platform"


def run_bounded_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    max_output_chars: int,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
    cancel_event: threading.Event | None = None,
    stdout_line_callback: Callable[[str, bool], None] | None = None,
    max_stdout_line_bytes: int | None = None,
    sandbox: CommandSandbox | None = None,
) -> CommandResult:
    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise ValueError("argv must be a non-empty sequence of non-empty strings")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if max_output_chars <= 0:
        raise ValueError("max_output_chars must be positive")
    if max_stdout_line_bytes is not None and max_stdout_line_bytes <= 0:
        raise ValueError("max_stdout_line_bytes must be positive")
    sandboxed_argv = sandboxed_command_argv(argv, cwd=cwd, sandbox=sandbox)
    if isinstance(sandboxed_argv, str):
        return CommandResult(
            argv=tuple(argv),
            returncode=126,
            stdout="",
            stderr=sandboxed_argv,
        )

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        list(sandboxed_argv),
        cwd=cwd,
        env=dict(env) if env is not None else sanitized_subprocess_env(),
        stdin=subprocess.PIPE if isinstance(stdin, str) else subprocess.DEVNULL,
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
    stdout_reader = (
        threading.Thread(
            target=_drain_stdout_lines,
            args=(
                process.stdout,
                stdout_capture,
                stdout_line_callback,
                max_stdout_line_bytes or max_output_chars,
            ),
            daemon=True,
        )
        if stdout_line_callback is not None
        else threading.Thread(
            target=_drain_pipe,
            args=(process.stdout, stdout_capture),
            daemon=True,
        )
    )
    readers = (
        stdout_reader,
        threading.Thread(
            target=_drain_pipe,
            args=(process.stderr, stderr_capture),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()
    if stdin is not None and isinstance(stdin, str):
        assert process.stdin is not None
        threading.Thread(target=_write_stdin, args=(process.stdin, stdin), daemon=True).start()

    timed_out = False
    deadline = time.monotonic() + timeout_seconds
    while True:
        if cancel_event is not None and cancel_event.is_set():
            _stop_process_tree(process)
            returncode = 130
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _stop_process_tree(process)
            returncode = 124
            break
        try:
            returncode = process.wait(timeout=min(remaining, 0.05))
        except subprocess.TimeoutExpired:
            continue
        # Do not leave background descendants holding pipes or mutating the host.
        if os.name == "posix":
            _stop_process_tree(process)
        break
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
        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None

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
