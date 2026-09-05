"""Local process contracts shared by the canonical runner and legacy facade."""

from __future__ import annotations

import ast
import contextlib
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from looplane import runtime
from looplane.execution.capture import _BoundedCapture, bounded_text
from looplane.execution.environment import sanitized_subprocess_env
from looplane.execution.local_process import run_local_process
from looplane.execution.types import CommandResult


@pytest.fixture(
    params=[run_local_process, runtime.run_bounded_command], ids=["canonical", "legacy"]
)
def run(request, tmp_path):
    def execute(script, **kwargs):
        options = dict(cwd=tmp_path, timeout_seconds=3, max_output_chars=4096)
        options.update(kwargs)
        return request.param((sys.executable, "-c", script), **options)

    return execute


def test_large_stdin_and_both_output_pipes_are_fully_drained(run):
    result = run(
        "import sys; data=sys.stdin.buffer.read(); "
        "sys.stdout.buffer.write(data); sys.stderr.buffer.write(data)",
        stdin="abc" * 400_000,
        max_output_chars=127,
    )
    assert result.ok
    assert result.stdout_bytes == result.stderr_bytes == 1_200_000
    assert result.stdout_truncated and result.stderr_truncated
    assert len(result.stdout.encode()) == len(result.stderr.encode()) == 127
    assert result.stdout.startswith("abc") and result.stdout.endswith("abc")


def test_default_stdin_is_eof_and_early_closed_stdin_does_not_block(run):
    assert run("import sys; print(repr(sys.stdin.read()))").stdout == "''\n"
    assert run("pass", stdin="x" * 2_000_000).ok


def test_blocked_stdin_writer_is_released_by_timeout(run):
    started = time.monotonic()
    result = run("import time; time.sleep(30)", stdin="x" * 2_000_000, timeout_seconds=0.1)
    assert result.returncode == 124 and result.timed_out
    assert time.monotonic() - started < 2


def test_slow_bounded_callback_drains_output_larger_than_pipe_capacity(run):
    delivered = []

    def consume(text, truncated):
        delivered.append((text, truncated))
        time.sleep(0.001)

    result = run(
        "print(('x' * 512 + '\\n') * 300, end='')",
        stdout_line_callback=consume,
        max_stdout_line_bytes=16,
    )
    assert result.ok and result.stdout_bytes == 513 * 300
    assert delivered == [("x" * 16, True)] * 300


def test_arguments_are_literal_and_cwd_is_explicit(tmp_path):
    argument = "$(touch must-not-exist); *"
    result = run_local_process(
        (sys.executable, "-c", "import os,sys; print(sys.argv[1]); print(os.getcwd())", argument),
        cwd=tmp_path,
        timeout_seconds=2,
        max_output_chars=4096,
    )
    assert result.ok and result.stdout.splitlines() == [argument, str(tmp_path.resolve())]
    assert not (tmp_path / "must-not-exist").exists()


def test_invalid_utf8_crlf_empty_and_unterminated_lines(run):
    lines = []
    result = run(
        "import os; os.write(1, b'\\r\\n\\n\\xff\\nlast')",
        stdout_line_callback=lambda text, truncated: lines.append((text, truncated)),
    )
    assert result.ok
    assert lines == [("", False), ("", False), ("\ufffd", False), ("last", False)]


def test_utf8_codepoint_split_across_os_writes_is_reassembled_before_line_decode(run):
    lines = []
    result = run(
        "import os,time; os.write(1, b'\\xe4'); time.sleep(.05); os.write(1, b'\\xb8\\xad\\n')",
        stdout_line_callback=lambda text, truncated: lines.append((text, truncated)),
    )
    assert result.ok and result.stdout == "\u4e2d\n"
    assert lines == [("\u4e2d", False)]


def test_callback_exception_does_not_stop_pipe_draining(run):
    calls = []

    def broken(text, truncated):
        calls.append(text)
        raise RuntimeError("consumer failed")

    result = run("print('line\\n' * 10000, end='')", stdout_line_callback=broken)
    assert result.ok and result.stdout_bytes == 50_000
    assert len(calls) == 10_000


def test_callback_can_cancel_without_waiting_for_child_deadline(run):
    event = threading.Event()
    started = time.monotonic()
    result = run(
        "import time; print('ready', flush=True); time.sleep(30)",
        cancel_event=event,
        stdout_line_callback=lambda *_: event.set(),
    )
    assert result.returncode == 130 and not result.timed_out
    assert result.stdout == "ready\n"
    assert time.monotonic() - started < 2


def test_expired_deadline_reports_timeout_and_reaps_child(run):
    result = run("import time; time.sleep(30)", timeout_seconds=0.1)
    assert result.returncode == 124 and result.timed_out and not result.ok


def test_preexisting_cancellation_wins_over_expired_deadline(run):
    event = threading.Event()
    event.set()
    result = run("import time; time.sleep(30)", cancel_event=event, timeout_seconds=1e-9)
    assert result.returncode == 130 and not result.timed_out


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
@pytest.mark.parametrize("finish", ["exit", "timeout", "cancel"])
def test_descendant_ignoring_sigterm_is_killed_even_after_leader_exit(run, tmp_path, finish):
    marker = tmp_path / "descendant-survived"
    event = threading.Event()
    pids = []

    def ready(text, truncated):
        pids.append(int(text))
        if finish == "cancel":
            event.set()

    # The inherited pipe synchronizes SIGTERM disposition before the leader exits.
    script = (
        "import os,signal,time; from pathlib import Path\n"
        "read,write=os.pipe()\n"
        "pid=os.fork()\n"
        "if pid == 0:\n"
        " os.close(read); signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        " os.write(write,b'r'); os.close(write); time.sleep(.8)\n"
        f" Path({str(marker)!r}).write_text('alive'); time.sleep(30)\n"
        "else:\n"
        " os.close(write); os.read(read,1); os.close(read); print(pid,flush=True)\n"
        + (" time.sleep(30)\n" if finish != "exit" else "")
    )
    try:
        result = run(
            script,
            cancel_event=event,
            stdout_line_callback=ready,
            timeout_seconds=0.4 if finish == "timeout" else 3,
        )
        assert pids
        assert result.returncode == {"exit": 0, "timeout": 124, "cancel": 130}[finish]
        time.sleep(0.9)
        assert not marker.exists()
    finally:
        for pid in pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_environment_default_is_sanitized_and_explicit_mapping_is_exact(run, monkeypatch):
    monkeypatch.setenv("PROCESS_TEST_SECRET", "must-not-forward")
    assert run("import os; print(os.getenv('PROCESS_TEST_SECRET'))").stdout == "None\n"
    assert (
        run(
            "import os; print(os.getenv('PROCESS_TEST_SECRET'))",
            env={"PROCESS_TEST_SECRET": "explicit"},
        ).stdout
        == "explicit\n"
    )


def test_task_environment_paths_and_git_noninteractive_contract(tmp_path):
    env = sanitized_subprocess_env(task_home=tmp_path / "home")
    assert Path(env["TMPDIR"]).is_dir()
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "HOME" not in env
    assert env["CODING_AGENT_TASK_HOME"] == str(tmp_path / "home")


@pytest.mark.parametrize("bound", [1, 2, 3, 31, 100])
def test_capture_retention_is_byte_bounded_and_split_utf8_limit_is_characterized(bound):
    capture = _BoundedCapture(bound)
    value = "\u4e2d" * 100
    for byte in value.encode():
        capture.add(bytes([byte]))
    assert capture.total_bytes == 300 and capture.truncated
    assert len(capture._head) + len(capture._tail) <= bound
    assert len(bounded_text(value, bound).encode()) <= bound
    assert len(capture.text().encode("utf-8")) <= bound


def test_split_cutpoint_replacement_stays_inside_utf8_byte_bound():
    capture = _BoundedCapture(80)
    capture.add(("\u4e2d" * 100).encode())
    assert len(capture.text().encode()) <= 80
    assert capture.truncated


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout_seconds": 0},
        {"max_output_chars": 0},
        {"max_stdout_line_bytes": 0},
    ],
)
def test_invalid_bounds_rejected_before_spawn(run, kwargs):
    with pytest.raises(ValueError):
        run("raise AssertionError('must not run')", **kwargs)


def test_facade_environment_and_stop_hooks_still_apply(tmp_path, monkeypatch):
    stops = []
    real_stop = runtime._stop_process_tree

    def stop(process):
        stops.append(process.pid)
        real_stop(process)

    monkeypatch.setattr(runtime, "_stop_process_tree", stop)
    monkeypatch.setattr(runtime, "sanitized_subprocess_env", lambda: {"TEST_VALUE": "kept"})
    result = runtime.run_bounded_command(
        (
            sys.executable,
            "-c",
            "import os,time; print(os.getenv('TEST_VALUE'),flush=True); time.sleep(30)",
        ),
        cwd=tmp_path,
        timeout_seconds=0.2,
        max_output_chars=100,
    )
    assert result.stdout == "kept\n" and result.timed_out and stops
    assert runtime.CommandResult is CommandResult


def test_process_and_sandbox_dependencies_do_not_point_to_facades_or_product_layers():
    root = Path(__file__).resolve().parents[2] / "src" / "looplane"
    forbidden = {
        "runtime",
        "landlock_run",
        "agent",
        "loop",
        "tools",
        "tooling",
        "commands",
        "cli",
        "terminal",
        "tui",
        "runtimes",
        "backends",
        "models",
        "codex_app_server",
        "codex_conversation",
        "conversation_workspace",
    }
    paths = [
        *(root / "execution").glob("*.py"),
        *(root / "sandbox").glob("*.py"),
        root / "workspace" / "local_git.py",
    ]
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"Use explicit canonical imports: {path}"
                imports = [node.module or ""]
                if node.module == "looplane":
                    imports.extend(f"looplane.{alias.name}" for alias in node.names)
            else:
                continue
            for imported in imports:
                parts = imported.split(".")
                assert not (len(parts) > 1 and parts[0] == "looplane" and parts[1] in forbidden), (
                    path,
                    imported,
                )
