"""Enforced process limits and interrupted-I/O contracts, without network."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from looplane import runtime
from looplane.execution import local_process
from looplane.execution.capture import _BoundedCapture, _stdin_chunks
from looplane.execution.types import DEFAULT_MAX_STDIN_BYTES


@pytest.fixture(params=[local_process.run_local_process, runtime.run_bounded_command])
def run(request, tmp_path):
    def execute(script, **overrides):
        options = dict(cwd=tmp_path, timeout_seconds=3, max_output_chars=4096)
        options.update(overrides)
        return request.param((sys.executable, "-c", script), **options)

    return execute


@pytest.mark.parametrize("bound", [1, 2, 3, 4, 31, 80, 100])
@pytest.mark.parametrize("payload", [b"\xff" * 100, "\u4e2d\U0001f30d".encode() * 100])
def test_capture_rendered_bytes_never_exceed_cap(bound, payload):
    capture = _BoundedCapture(bound)
    for byte in payload:
        capture.add(bytes([byte]))
    assert capture.total_bytes == len(payload)
    assert len(capture.text().encode("utf-8")) <= bound
    assert capture.truncated


def test_malformed_bytes_within_raw_cap_still_mark_rendering_truncation():
    capture = _BoundedCapture(2)
    capture.add(b"\xff")
    assert capture.total_bytes == 1
    assert capture.truncated
    assert len(capture.text().encode()) <= 2


@pytest.mark.parametrize("bound", [1, 2, 3, 5, 80])
def test_both_streams_and_callback_lines_respect_encoded_byte_cap(run, bound):
    lines = []
    result = run(
        "import os; os.write(1,b'\\xff'*100+b'\\n'); os.write(2,b'\\xff'*100)",
        max_output_chars=bound,
        max_stdout_line_bytes=bound,
        stdout_line_callback=lambda text, truncated: lines.append((text, truncated)),
    )
    assert result.ok
    assert result.stdout_bytes == 101 and result.stderr_bytes == 100
    assert result.stdout_truncated and result.stderr_truncated
    assert len(result.stdout.encode()) <= bound and len(result.stderr.encode()) <= bound
    assert len(lines) == 1 and lines[0][1]
    assert len(lines[0][0].encode()) <= bound


@pytest.mark.parametrize("payload,cap", [("a" * 11, 10), ("\u4e2d" * 4, 11)])
def test_oversized_stdin_is_rejected_before_any_spawn(run, monkeypatch, payload, cap):
    monkeypatch.setattr(
        local_process.subprocess, "Popen", lambda *_a, **_k: pytest.fail("must not spawn")
    )
    with pytest.raises(ValueError, match=f"stdin exceeds {cap} UTF-8 bytes"):
        run("pass", stdin=payload, max_stdin_bytes=cap)


def test_default_stdin_limit_is_finite_and_exported(run, monkeypatch):
    assert runtime.DEFAULT_MAX_STDIN_BYTES == DEFAULT_MAX_STDIN_BYTES == 8 * 1024 * 1024
    monkeypatch.setattr(
        local_process.subprocess, "Popen", lambda *_a, **_k: pytest.fail("must not spawn")
    )
    with pytest.raises(ValueError, match="stdin exceeds"):
        run("pass", stdin="x" * (DEFAULT_MAX_STDIN_BYTES + 1))


def test_exact_stdin_byte_limit_is_delivered_without_full_input_encoding(run):
    class ChunkOnlyString(str):
        def encode(self, *_args, **_kwargs):
            raise AssertionError("whole-input encoding is forbidden")

    payload = ChunkOnlyString("\u4e2d" * 20_000)
    result = run(
        "import sys; data=sys.stdin.buffer.read(); print(len(data)); "
        "assert data == '\u4e2d'.encode()*20000",
        stdin=payload,
        max_stdin_bytes=60_000,
    )
    assert result.ok and result.stdout == "60000\n"
    assert max(map(len, _stdin_chunks(payload))) <= 64 * 1024


@pytest.mark.parametrize(
    "options",
    [
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": float("nan")},
        {"max_stdin_bytes": 0},
        {"max_stdin_bytes": -1},
        {"max_stdin_bytes": float("inf")},
    ],
)
def test_unbounded_or_invalid_request_is_rejected_before_spawn(run, monkeypatch, options):
    monkeypatch.setattr(
        local_process.subprocess, "Popen", lambda *_a, **_k: pytest.fail("must not spawn")
    )
    with pytest.raises(ValueError):
        run("pass", **options)


def test_pre_cancelled_request_never_launches_user_code(run, monkeypatch):
    event = threading.Event()
    event.set()
    monkeypatch.setattr(
        local_process.subprocess, "Popen", lambda *_a, **_k: pytest.fail("must not spawn")
    )
    result = run("pass", cancel_event=event)
    assert result.returncode == 130 and not result.timed_out


def test_preflight_time_is_part_of_deadline(tmp_path, monkeypatch):
    def launcher(*_args, **_kwargs):
        time.sleep(0.03)
        return (sys.executable, "-c", "pass")

    monkeypatch.setattr(
        local_process.subprocess, "Popen", lambda *_a, **_k: pytest.fail("must not spawn")
    )
    result = local_process.run_local_process(
        ("placeholder",),
        cwd=tmp_path,
        timeout_seconds=0.01,
        max_output_chars=100,
        _sandbox_launcher=launcher,
    )
    assert result.returncode == 124 and result.timed_out


@pytest.mark.parametrize("finish", ["deadline", "cancel", "leader_exit"])
def test_blocked_callback_cannot_hold_runner_or_dispatch_later_lines(run, finish):
    entered = threading.Event()
    release = threading.Event()
    cancel = threading.Event()
    callback_threads = []
    delivered = []

    def callback(text, _truncated):
        callback_threads.append(threading.current_thread())
        delivered.append(text)
        entered.set()
        release.wait(5)

    def cancel_on_entry():
        if entered.wait(2):
            cancel.set()

    canceller = threading.Thread(target=cancel_on_entry) if finish == "cancel" else None
    if canceller is not None:
        canceller.start()
    try:
        start = time.monotonic()
        result = run(
            "import time; print('first',flush=True); print('later',flush=True); "
            + ("time.sleep(30)" if finish != "leader_exit" else "pass"),
            timeout_seconds=0.4,
            cancel_event=cancel,
            stdout_line_callback=callback,
        )
        assert entered.is_set()
        assert time.monotonic() - start < 2
        assert result.returncode == (130 if finish == "cancel" else 124)
        assert result.timed_out == (finish != "cancel")
        assert result.stdout_callback_incomplete and result.output_incomplete
        assert not result.ok
    finally:
        release.set()
        if canceller is not None:
            canceller.join(2)
        for worker in callback_threads:
            worker.join(2)
    assert delivered == ["first"]
    assert all(not worker.is_alive() for worker in callback_threads)


def test_blocked_callbacks_have_bounded_capacity_and_recover_after_release(run, monkeypatch):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(local_process, "_CALLBACK_SLOTS", slots)
    release = threading.Event()
    callback_threads = []

    def callback(*_):
        callback_threads.append(threading.current_thread())
        release.wait(5)

    try:
        first = run("print('line',flush=True)", stdout_line_callback=callback, timeout_seconds=0.3)
        assert first.timed_out and first.stdout_callback_incomplete
        second = run("raise AssertionError('must not launch')", stdout_line_callback=callback)
        assert second.returncode == 125 and "capacity is exhausted" in second.stderr
    finally:
        release.set()
        for worker in callback_threads:
            worker.join(2)
    third = run("print('ready')", stdout_line_callback=lambda *_: None)
    assert third.ok and not third.stdout_callback_incomplete


def test_failed_spawn_releases_reserved_callback_capacity(run, monkeypatch):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(local_process, "_CALLBACK_SLOTS", slots)

    def fail(*_args, **_kwargs):
        raise OSError("fixture spawn failure")

    monkeypatch.setattr(local_process.subprocess, "Popen", fail)
    with pytest.raises(OSError, match="fixture spawn failure"):
        run("pass", stdout_line_callback=lambda *_: None)
    assert slots.acquire(blocking=False)
    slots.release()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group and nonblocking pipe evidence")
def test_escaped_session_holding_pipes_cannot_hold_runner(run, tmp_path):
    pidfile = tmp_path / "escaped.pid"
    script = (
        "import os,time\n"
        "r,w=os.pipe()\n"
        "pid=os.fork()\n"
        "if pid == 0:\n"
        " os.close(r); os.setsid()\n"
        f" open({str(pidfile)!r},'w').write(str(os.getpid()))\n"
        " os.write(w,b'r'); os.close(w); time.sleep(30)\n"
        "else:\n"
        " os.close(w); os.read(r,1); os.close(r); print(pid,flush=True)\n"
    )
    try:
        started = time.monotonic()
        result = run(script, timeout_seconds=0.3)
        assert time.monotonic() - started < 2
        assert result.timed_out and result.output_incomplete and not result.ok
        # No claim that process groups contain intentionally escaped sessions.
        os.kill(int(pidfile.read_text()), 0)
    finally:
        if pidfile.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pidfile.read_text()), signal.SIGKILL)


def test_final_kill_wait_is_bounded():
    waits = []
    signals = []

    class Unreapable:
        def wait(self, timeout):
            waits.append(timeout)
            raise subprocess.TimeoutExpired("fixture", timeout)

        def kill(self):
            signals.append("kill")

    with pytest.raises(subprocess.TimeoutExpired):
        local_process._stop_process_tree(
            Unreapable(),
            _signal_group=lambda _process, sig: signals.append(sig),
        )
    assert waits == [0.5, 1.0, 1.0]
    assert signals[-1] == "kill"
