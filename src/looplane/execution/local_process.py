"""Bounded local process capture, input, callbacks and process-group cleanup."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path

from looplane.execution.capture import (
    _BoundedCapture,
    _drain_pipe,
    _drain_stdout_lines,
    _validate_stdin,
    _write_stdin,
)
from looplane.execution.environment import sanitized_subprocess_env
from looplane.execution.types import DEFAULT_MAX_STDIN_BYTES, CommandResult
from looplane.sandbox.launcher import sandboxed_command_argv
from looplane.sandbox.policy import CommandSandbox

# A Python callback cannot be forcibly terminated. Bound outstanding callback
# readers across calls, including callbacks abandoned at a deadline.
_CALLBACK_SLOTS = threading.BoundedSemaphore(8)
_IO_SHUTDOWN_SECONDS = 0.25


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


def _stop_process_tree(
    process: subprocess.Popen[bytes],
    *,
    _signal_group: Callable[[subprocess.Popen[bytes], int], None] | None = None,
) -> None:
    """Best-effort group cleanup; no unbounded final wait after escalation."""

    (_signal_group or _signal_process_group)(process, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=0.5)
    # The group may still contain grandchildren even if the leader exited.
    (_signal_group or _signal_process_group)(process, getattr(signal, "SIGKILL", signal.SIGTERM))
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1.0)


def run_local_process(
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
    _sandbox_launcher: Callable[..., tuple[str, ...] | str] | None = None,
    _stop_process: Callable[[subprocess.Popen[bytes]], None] | None = None,
    _env_factory: Callable[[], dict[str, str]] | None = None,
) -> CommandResult:
    """Run with UTF-8 byte bounds and a deadline covering I/O and callbacks.

    Stdin is rejected before spawn if its UTF-8 size exceeds max_stdin_bytes;
    encoding/writing uses bounded chunks. POSIX pipe I/O is interruptible.
    Callbacks remain ordered on one reader, applying bounded pipe backpressure.
    A callback already executing may outlive cancellation, but no later line is
    dispatched and its capacity slot remains held until it exits. Exhausted
    callback capacity refuses a new process with code 125.
    """
    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise ValueError("argv must be a non-empty sequence of non-empty strings")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive and finite")
    if not isinstance(max_output_chars, int) or max_output_chars <= 0:
        raise ValueError("max_output_chars must be a positive integer")
    if not isinstance(max_stdin_bytes, int) or max_stdin_bytes <= 0:
        raise ValueError("max_stdin_bytes must be a positive integer")
    if max_stdout_line_bytes is not None and (
        not isinstance(max_stdout_line_bytes, int) or max_stdout_line_bytes <= 0
    ):
        raise ValueError("max_stdout_line_bytes must be a positive integer")
    deadline = time.monotonic() + timeout_seconds
    _validate_stdin(stdin, max_stdin_bytes)

    def refused(returncode: int, stderr: str = "", *, timed_out: bool = False) -> CommandResult:
        capture = _BoundedCapture(max_output_chars)
        capture.add(stderr.encode("utf-8"))
        return CommandResult(
            argv=tuple(argv),
            returncode=returncode,
            stdout="",
            stderr=capture.text(),
            timed_out=timed_out,
            stderr_bytes=capture.total_bytes,
            stderr_truncated=capture.truncated,
        )

    sandboxed_argv = (_sandbox_launcher or sandboxed_command_argv)(argv, cwd=cwd, sandbox=sandbox)
    if isinstance(sandboxed_argv, str):
        return refused(126, sandboxed_argv)
    if cancel_event is not None and cancel_event.is_set():
        return refused(130)
    if time.monotonic() >= deadline:
        return refused(124, timed_out=True)

    callback_slots = _CALLBACK_SLOTS
    callback_reserved = stdout_line_callback is not None
    if callback_reserved and not callback_slots.acquire(blocking=False):
        return refused(125, "stdout callback capacity is exhausted")

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        process = subprocess.Popen(
            list(sandboxed_argv),
            cwd=cwd,
            env=dict(env) if env is not None else (_env_factory or sanitized_subprocess_env)(),
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=os.name == "posix",
            creationflags=creationflags,
            bufsize=0,
        )
    except BaseException:
        if callback_reserved:
            callback_slots.release()
        raise

    assert process.stdout is not None
    assert process.stderr is not None
    stop_io = threading.Event()
    stdout_capture = _BoundedCapture(max_output_chars)
    stderr_capture = _BoundedCapture(max_output_chars)
    worker_errors: list[BaseException] = []

    def stdout_work() -> None:
        try:
            if stdout_line_callback is not None:
                _drain_stdout_lines(
                    process.stdout,
                    stdout_capture,
                    stdout_line_callback,
                    max_stdout_line_bytes or max_output_chars,
                    stop_event=stop_io,
                )
            else:
                _drain_pipe(process.stdout, stdout_capture, stop_event=stop_io)
        except BaseException as exc:
            worker_errors.append(exc)
        finally:
            if callback_reserved:
                callback_slots.release()

    def stderr_work() -> None:
        try:
            _drain_pipe(process.stderr, stderr_capture, stop_event=stop_io)
        except BaseException as exc:
            worker_errors.append(exc)

    def stdin_work() -> None:
        assert process.stdin is not None and stdin is not None
        try:
            _write_stdin(process.stdin, stdin, stop_event=stop_io)
        except BaseException as exc:
            worker_errors.append(exc)

    stdout_reader = threading.Thread(
        target=stdout_work,
        name=f"looplane-stdout-{process.pid}",
        daemon=True,
    )
    stderr_reader = threading.Thread(
        target=stderr_work,
        name=f"looplane-stderr-{process.pid}",
        daemon=True,
    )
    workers = [stdout_reader, stderr_reader]
    if stdin is not None:
        workers.append(
            threading.Thread(
                target=stdin_work,
                name=f"looplane-stdin-{process.pid}",
                daemon=True,
            )
        )
    started: list[threading.Thread] = []
    timed_out = False
    output_incomplete = False
    returncode: int | None = None
    tree_stopped = False

    def stop_tree() -> None:
        nonlocal tree_stopped
        tree_stopped = True
        (_stop_process or _stop_process_tree)(process)

    try:
        for worker in workers:
            worker.start()
            started.append(worker)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                returncode = 130
                output_incomplete = any(reader.is_alive() for reader in workers[:2])
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                returncode = 124
                output_incomplete = any(reader.is_alive() for reader in workers[:2])
                break
            if worker_errors:
                raise worker_errors[0]
            if returncode is None:
                try:
                    returncode = process.wait(timeout=min(remaining, 0.05))
                except subprocess.TimeoutExpired:
                    continue
                # Never leave same-group descendants alive after leader exit.
                if os.name == "posix":
                    stop_tree()
            if all(not worker.is_alive() for worker in workers):
                break
            stop_io.wait(min(remaining, 0.01))
    finally:
        stop_io.set()
        try:
            if not tree_stopped:
                stop_tree()
        finally:
            cleanup_deadline = time.monotonic() + _IO_SHUTDOWN_SECONDS
            for worker in started:
                worker.join(timeout=max(0.0, cleanup_deadline - time.monotonic()))
            if stdout_reader not in started:
                process.stdout.close()
                if callback_reserved:
                    callback_slots.release()
            if stderr_reader not in started:
                process.stderr.close()
            if stdin is not None and workers[-1] not in started:
                assert process.stdin is not None
                process.stdin.close()
            # Started workers own their descriptors. Closing from this thread
            # can deadlock a buffered read or race descriptor reuse.

    if worker_errors:
        raise worker_errors[0]
    assert returncode is not None
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
        output_incomplete=output_incomplete,
        stdout_callback_incomplete=stdout_line_callback is not None and stdout_reader.is_alive(),
    )
