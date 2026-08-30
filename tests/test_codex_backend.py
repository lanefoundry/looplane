from __future__ import annotations

import asyncio
import os
import stat
import time
from pathlib import Path

import pytest

from looplane.backends import ExternalAgentEvent, ExternalAgentTask, ExternalRunStatus
from looplane.codex_backend import CodexCliBackend


def _fake_codex(tmp_path: Path, body: str) -> Path:
    executable = tmp_path / "codex"
    executable.write_text(f"#!/usr/bin/env python3\n{body}", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


@pytest.mark.asyncio
async def test_codex_backend_emits_normalized_events_before_child_exit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    finished = tmp_path / "finished"
    executable = _fake_codex(
        tmp_path,
        f"""
import json, pathlib, time
print(json.dumps({{"type": "thread.started", "thread_id": "private"}}), flush=True)
print(json.dumps({{"type": "turn.started"}}), flush=True)
print(json.dumps({{"type": "item.completed", "item": {{
    "id": "private-message", "type": "agent_message", "text": "hello"
}}}}), flush=True)
time.sleep(0.4)
pathlib.Path({str(finished)!r}).write_text("done")
print(json.dumps({{"type": "turn.completed"}}), flush=True)
""",
    )
    received: list[ExternalAgentEvent] = []
    message_received = asyncio.Event()

    class Sink:
        async def emit(self, event: ExternalAgentEvent) -> None:
            received.append(event)
            if event.event_type == "message":
                message_received.set()

    run_task = asyncio.create_task(
        CodexCliBackend(executable=executable).run(
            ExternalAgentTask(task_id="streaming", instruction="say hello"),
            working_directory=workspace,
            event_sink=Sink(),
        )
    )

    await asyncio.wait_for(message_received.wait(), timeout=1)
    assert not run_task.done()
    assert not finished.exists()
    result = await run_task

    assert result.status is ExternalRunStatus.COMPLETED
    assert [event.event_type for event in received] == [
        "system",
        "system",
        "message",
        "result",
    ]
    assert "private" not in "".join(event.model_dump_json() for event in received)


@pytest.mark.asyncio
async def test_codex_backend_uses_bounded_ephemeral_exec_and_normalizes_success(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = _fake_codex(
        tmp_path,
        """
import json, os, sys
expected = [
    "exec", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
    "--sandbox", "workspace-write", "--color", "never", "--skip-git-repo-check",
    "-C", os.getcwd(), "-",
]
assert sys.argv[1:] == expected, sys.argv
assert sys.stdin.read() == "inspect the disposable workspace\\n"
assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
print(json.dumps({"type": "thread.started", "thread_id": "private-thread"}))
print(json.dumps({"type": "turn.started"}))
print(json.dumps({"type": "item.started", "item": {
    "id": "private-item", "type": "command_execution", "command": "echo private"
}}))
print(json.dumps({"type": "item.completed", "item": {
    "id": "private-message", "type": "agent_message", "text": "inspection complete"
}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}))
""",
    )
    backend = CodexCliBackend(
        executable=executable,
    )

    result = await backend.run(
        ExternalAgentTask(
            task_id="task-success",
            instruction="inspect the disposable workspace",
        ),
        working_directory=workspace,
    )

    assert result.status is ExternalRunStatus.COMPLETED
    assert result.terminal_reason == "completed"
    assert result.summary == "inspection complete"
    assert [event.event_type for event in result.events] == [
        "system",
        "system",
        "activity",
        "message",
        "result",
    ]
    serialized = result.model_dump_json()
    assert "private-thread" not in serialized
    assert "private-item" not in serialized
    assert "echo private" not in serialized
    assert backend.local_only is True
    assert backend.experimental is True


@pytest.mark.asyncio
async def test_codex_backend_forwards_optional_model(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = _fake_codex(
        tmp_path,
        """
import json, sys
assert sys.argv[-3:] == ["--model", "gpt-5.6-terra", "-"]
assert sys.argv[sys.argv.index("--sandbox") + 1] == "read-only"
print(json.dumps({"type": "thread.started"}))
print(json.dumps({"type": "turn.started"}))
print(json.dumps({"type": "item.completed", "item": {
    "type": "agent_message", "text": "done"
}}))
print(json.dumps({"type": "turn.completed", "usage": {}}))
""",
    )

    result = await CodexCliBackend(
        executable=executable, model="gpt-5.6-terra", sandbox_mode="read-only"
    ).run(
        ExternalAgentTask(task_id="task-model", instruction="inspect"),
        working_directory=workspace,
    )

    assert result.status is ExternalRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_codex_backend_normalizes_error_without_stderr_or_provider_message(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = _fake_codex(
        tmp_path,
        """
import json, sys
print(json.dumps({"type": "thread.started", "thread_id": "private-thread"}))
print(json.dumps({"type": "turn.started"}))
print(json.dumps({"type": "error", "message": "bearer private-provider-message"}))
print("private stderr diagnostic", file=sys.stderr)
raise SystemExit(7)
""",
    )

    result = await CodexCliBackend(
        executable=executable,
        working_directory=workspace,
    ).run(ExternalAgentTask(task_id="task-error", instruction="fail safely"))

    assert result.status is ExternalRunStatus.FAILED
    assert result.exit_code == 7
    assert result.terminal_reason == "external_agent_error"
    serialized = result.model_dump_json()
    assert "private-provider-message" not in serialized
    assert "private stderr diagnostic" not in serialized
    assert "private-thread" not in serialized


@pytest.mark.asyncio
async def test_codex_backend_rejects_duplicate_terminal_events(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = _fake_codex(
        tmp_path,
        """
import json
print(json.dumps({"type": "thread.started", "thread_id": "private"}))
print(json.dumps({"type": "turn.started"}))
print(json.dumps({"type": "item.completed", "item": {
    "id": "message", "type": "agent_message", "text": "looks done"
}}))
print(json.dumps({"type": "turn.completed"}))
print(json.dumps({"type": "turn.completed"}))
""",
    )

    result = await CodexCliBackend(
        executable=executable,
        working_directory=workspace,
    ).run(ExternalAgentTask(task_id="task-duplicate", instruction="reject duplicates"))

    assert result.status is ExternalRunStatus.FAILED
    assert result.terminal_reason == "invalid_terminal_count"


@pytest.mark.asyncio
async def test_codex_backend_rejects_protocol_drift(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = _fake_codex(
        tmp_path,
        """
import json
print(json.dumps({"type": "thread.started", "thread_id": "private"}))
print(json.dumps({"type": "turn.started"}))
print(json.dumps({"type": "future.unrecognized", "secret": "private"}))
print(json.dumps({"type": "item.completed", "item": {
    "id": "message", "type": "agent_message", "text": "looks done"
}}))
print(json.dumps({"type": "turn.completed"}))
""",
    )

    result = await CodexCliBackend(
        executable=executable,
        working_directory=workspace,
    ).run(ExternalAgentTask(task_id="task-drift", instruction="fail closed"))

    assert result.status is ExternalRunStatus.FAILED
    assert result.terminal_reason == "malformed_event_stream"
    assert "private" not in result.model_dump_json()
    assert result.events[2].event_type == "protocol_drift"
    assert result.events[2].data == {
        "source": "codex-cli",
        "top_level_type": "future.unrecognized",
    }


@pytest.mark.asyncio
async def test_codex_backend_timeout_kills_process_group(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "grandchild-finished"
    child = (
        f"import pathlib,time; time.sleep(0.6); pathlib.Path({str(marker)!r}).write_text('alive')"
    )
    executable = _fake_codex(
        tmp_path,
        f"""
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", {child!r}])
time.sleep(10)
""",
    )

    result = await CodexCliBackend(
        executable=executable,
        working_directory=workspace,
        timeout_seconds=0.1,
    ).run(ExternalAgentTask(task_id="task-timeout", instruction="time out"))

    assert result.status is ExternalRunStatus.TIMED_OUT
    assert result.exit_code == 124
    assert result.terminal_reason == "timeout"
    await asyncio.sleep(0.8)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_codex_backend_cancellation_kills_process_group_promptly(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    started = tmp_path / "started"
    grandchild = tmp_path / "grandchild-finished"
    child = (
        "import pathlib,time; time.sleep(0.8); "
        f"pathlib.Path({str(grandchild)!r}).write_text('alive')"
    )
    executable = _fake_codex(
        tmp_path,
        f"""
import pathlib, subprocess, sys, time
pathlib.Path({str(started)!r}).write_text("ready")
subprocess.Popen([sys.executable, "-c", {child!r}])
time.sleep(30)
""",
    )
    task = asyncio.create_task(
        CodexCliBackend(
            executable=executable,
            working_directory=workspace,
            timeout_seconds=30,
        ).run(ExternalAgentTask(task_id="task-cancel", instruction="cancel safely"))
    )
    for _ in range(300):
        if started.exists():
            break
        await asyncio.sleep(0.01)
    assert started.exists()

    cancelled_at = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert time.monotonic() - cancelled_at < 2
    await asyncio.sleep(1)
    assert not grandchild.exists()


@pytest.mark.asyncio
async def test_codex_backend_does_not_forward_or_return_host_secrets(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_home = tmp_path / "official-home"
    fake_codex_home = fake_home / ".codex"
    executable = _fake_codex(
        tmp_path,
        """
import json, os
markers = ("API_KEY", "AUTH", "CREDENTIAL", "PASSWORD", "SECRET", "TOKEN")
leaked = sorted(key for key in os.environ if any(marker in key.upper() for marker in markers))
assert os.environ["HOME"].endswith("official-home")
assert os.environ["CODEX_HOME"].endswith(".codex")
print(json.dumps({"type": "thread.started", "thread_id": "private"}))
print(json.dumps({"type": "turn.started"}))
print(json.dumps({"type": "item.completed", "item": {
    "id": "message", "type": "agent_message", "text": ",".join(leaked) or "clean"
}}))
print(json.dumps({"type": "turn.completed"}))
""",
    )
    secret = "must-not-reach-child"
    backend = CodexCliBackend(
        executable=executable,
        working_directory=workspace,
        host_env={
            "PATH": os.environ["PATH"],
            "HOME": str(fake_home),
            "CODEX_HOME": str(fake_codex_home),
            "OPENAI_API_KEY": secret,
            "CODEX_API_KEY": secret,
            "MY_AUTH_TOKEN": secret,
            "MY_SECRET": secret,
        },
    )

    result = await backend.run(
        ExternalAgentTask(task_id="task-env", instruction="check environment")
    )

    serialized = result.model_dump_json()
    assert result.status is ExternalRunStatus.COMPLETED
    assert result.summary == "clean"
    assert secret not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "CODEX_API_KEY" not in serialized


@pytest.mark.asyncio
async def test_codex_backend_bounds_input_and_output(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = _fake_codex(
        tmp_path,
        """
print("x" * 10000)
""",
    )

    with pytest.raises(ValueError, match="max_input_bytes"):
        await CodexCliBackend(
            executable=executable,
            working_directory=workspace,
            max_input_bytes=32,
        ).run(ExternalAgentTask(task_id="task-input", instruction="x" * 100))

    result = await CodexCliBackend(
        executable=executable,
        working_directory=workspace,
        max_output_bytes=256,
    ).run(ExternalAgentTask(task_id="task-output", instruction="bound output"))

    assert result.status is ExternalRunStatus.FAILED
    assert result.terminal_reason == "output_limit_exceeded"
    assert len(result.model_dump_json()) < 2_000


def test_codex_backend_rejects_unsafe_sandbox_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sandbox_mode"):
        CodexCliBackend(
            executable="codex",
            working_directory=tmp_path,
            sandbox_mode="danger-full-access",
        )
