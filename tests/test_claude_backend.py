from __future__ import annotations

import asyncio
import os
import stat
import time
from pathlib import Path

import pytest

from rivumi.backends import ExternalAgentEvent, ExternalAgentTask, ExternalRunStatus
from rivumi.claude_backend import ClaudeCodeBackend


def _fake_claude(tmp_path: Path, body: str) -> Path:
    executable = tmp_path / "claude"
    executable.write_text(f"#!/usr/bin/env python3\n{body}", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


@pytest.mark.asyncio
async def test_claude_backend_emits_message_before_exit_and_deduplicates_result(
    tmp_path: Path,
) -> None:
    finished = tmp_path / "finished"
    executable = _fake_claude(
        tmp_path,
        f"""
import json, pathlib, time
print(json.dumps({{"type": "system", "subtype": "init", "session_id": "private"}}), flush=True)
print(json.dumps({{"type": "assistant", "message": {{"content": [
    {{"type": "text", "text": "hello"}}
]}}}}), flush=True)
time.sleep(0.4)
pathlib.Path({str(finished)!r}).write_text("done")
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                  "result": "hello"}}), flush=True)
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
        ClaudeCodeBackend(executable=executable).run(
            ExternalAgentTask(task_id="streaming", instruction="say hello"),
            event_sink=Sink(),
        )
    )

    await asyncio.wait_for(message_received.wait(), timeout=1)
    assert not run_task.done()
    assert not finished.exists()
    result = await run_task

    assert result.status is ExternalRunStatus.COMPLETED
    assert result.summary == "hello"
    assert [event.event_type for event in received] == ["system", "message", "result"]
    assert received[-1].text is None
    assert result.events[-1].text is None
    assert "private" not in "".join(event.model_dump_json() for event in received)


@pytest.mark.asyncio
async def test_claude_backend_normalizes_successful_stream(tmp_path: Path) -> None:
    executable = _fake_claude(
        tmp_path,
        """
import json, os, sys
assert "--safe-mode" in sys.argv
assert "--disable-slash-commands" in sys.argv
assert "--tools=" in sys.argv
request = json.loads(sys.stdin.readline())
assert request["type"] == "user"
assert request["message"]["content"] == "inspect the fixture"
assert os.path.basename(os.getcwd()).startswith("rivumi-claude-")
print(json.dumps({"type": "system", "subtype": "init", "session_id": "private"}))
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "inspection complete"}
]}}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "done"}))
""",
    )
    backend = ClaudeCodeBackend(executable=executable)

    result = await backend.run(
        ExternalAgentTask(task_id="task-1", instruction="inspect the fixture")
    )

    assert result.status is ExternalRunStatus.COMPLETED
    assert result.summary == "done"
    assert [event.event_type for event in result.events] == ["system", "message", "result"]
    assert result.events[0].data == {"source": "claude-code", "subtype": "init"}
    assert "private" not in result.model_dump_json()
    assert backend.local_only is True
    assert backend.experimental is True


@pytest.mark.asyncio
async def test_claude_backend_forwards_optional_model_alias(tmp_path: Path) -> None:
    executable = _fake_claude(
        tmp_path,
        """
import json, sys
assert sys.argv[sys.argv.index("--model") + 1] == "sonnet"
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "done"}))
""",
    )

    result = await ClaudeCodeBackend(executable=executable, model="sonnet").run(
        ExternalAgentTask(task_id="task-model", instruction="inspect")
    )

    assert result.status is ExternalRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_claude_backend_coding_mode_uses_only_bounded_file_tools(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = _fake_claude(
        tmp_path,
        """
import json, os, sys
assert os.getcwd().endswith("workspace")
assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
tools = sys.argv[sys.argv.index("--tools") + 1]
assert tools == "Read,Glob,Grep,Edit"
assert "--tools=" not in sys.argv
assert sys.argv[sys.argv.index("--permission-mode") + 1] == "acceptEdits"
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "edited"}))
""",
    )

    result = await ClaudeCodeBackend(executable=executable).run(
        ExternalAgentTask(task_id="coding", instruction="edit one file"),
        working_directory=workspace,
    )

    assert result.status is ExternalRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_claude_backend_normalizes_cli_error_without_raw_stderr(tmp_path: Path) -> None:
    executable = _fake_claude(
        tmp_path,
        """
import json, sys
print(json.dumps({"type": "result", "subtype": "error_during_execution",
                  "is_error": True, "result": "request failed"}))
print("private diagnostic", file=sys.stderr)
raise SystemExit(7)
""",
    )

    result = await ClaudeCodeBackend(executable=executable).run(
        ExternalAgentTask(task_id="task-2", instruction="fail safely")
    )

    assert result.status is ExternalRunStatus.FAILED
    assert result.exit_code == 7
    assert result.terminal_reason == "external_agent_error"
    assert "private diagnostic" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_claude_backend_timeout_kills_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild-finished"
    child = (
        f"import pathlib,time; time.sleep(0.6); pathlib.Path({str(marker)!r}).write_text('alive')"
    )
    executable = _fake_claude(
        tmp_path,
        f"""
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", {child!r}])
time.sleep(10)
""",
    )

    result = await ClaudeCodeBackend(executable=executable, timeout_seconds=0.1).run(
        ExternalAgentTask(task_id="task-3", instruction="time out")
    )

    assert result.status is ExternalRunStatus.TIMED_OUT
    assert result.exit_code == 124
    await __import__("asyncio").sleep(0.8)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_claude_backend_does_not_forward_or_return_host_secrets(tmp_path: Path) -> None:
    secret = "should-never-reach-the-child"
    executable = _fake_claude(
        tmp_path,
        """
import json, os
leaked = [key for key in os.environ if "TOKEN" in key or "SECRET" in key or "API_KEY" in key]
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": ",".join(leaked) or "clean"}))
""",
    )
    backend = ClaudeCodeBackend(
        executable=executable,
        host_env={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "ANTHROPIC_API_KEY": secret,
            "CLAUDE_CODE_OAUTH_TOKEN": secret,
            "MY_SECRET": secret,
        },
    )

    result = await backend.run(ExternalAgentTask(task_id="task-4", instruction="check env"))

    serialized = result.model_dump_json()
    assert result.status is ExternalRunStatus.COMPLETED
    assert result.summary == "clean"
    assert secret not in serialized
    assert "ANTHROPIC_API_KEY" not in serialized
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in serialized


@pytest.mark.asyncio
async def test_claude_backend_rejects_oversized_input_before_spawn(tmp_path: Path) -> None:
    executable = _fake_claude(tmp_path, "raise SystemExit(99)\n")
    backend = ClaudeCodeBackend(executable=executable, max_input_bytes=32)

    with pytest.raises(ValueError, match="max_input_bytes"):
        await backend.run(ExternalAgentTask(task_id="task-5", instruction="x" * 100))


@pytest.mark.asyncio
async def test_claude_backend_rejects_result_without_explicit_error_flag(
    tmp_path: Path,
) -> None:
    executable = _fake_claude(
        tmp_path,
        """
import json
print(json.dumps({"type": "result", "subtype": "success", "result": "ambiguous"}))
""",
    )

    result = await ClaudeCodeBackend(executable=executable).run(
        ExternalAgentTask(task_id="task-6", instruction="fail closed")
    )

    assert result.status is ExternalRunStatus.FAILED
    assert result.terminal_reason == "invalid_result_event"


@pytest.mark.asyncio
async def test_claude_backend_requires_positive_success_subtype(tmp_path: Path) -> None:
    executable = _fake_claude(
        tmp_path,
        """
import json
print(json.dumps({"type": "result", "subtype": "error_during_execution",
                  "is_error": False, "result": "failed"}))
""",
    )

    result = await ClaudeCodeBackend(executable=executable).run(
        ExternalAgentTask(task_id="task-8", instruction="fail closed on protocol drift")
    )

    assert result.status is ExternalRunStatus.FAILED
    assert result.terminal_reason == "invalid_result_event"


@pytest.mark.asyncio
async def test_claude_backend_rejects_duplicate_terminal_results(tmp_path: Path) -> None:
    executable = _fake_claude(
        tmp_path,
        """
import json
print(json.dumps({"type": "result", "subtype": "error_during_execution",
                  "is_error": True, "result": "failed"}))
print(json.dumps({"type": "result", "subtype": "success",
                  "is_error": False, "result": "looks recovered"}))
""",
    )

    result = await ClaudeCodeBackend(executable=executable).run(
        ExternalAgentTask(task_id="task-9", instruction="reject duplicate terminal state")
    )

    assert result.status is ExternalRunStatus.FAILED
    assert result.terminal_reason == "invalid_result_count"


@pytest.mark.asyncio
async def test_claude_backend_cancellation_kills_process_group_promptly(tmp_path: Path) -> None:
    started = tmp_path / "started"
    grandchild = tmp_path / "grandchild-finished"
    child = (
        "import pathlib,time; time.sleep(0.8); "
        f"pathlib.Path({str(grandchild)!r}).write_text('alive')"
    )
    executable = _fake_claude(
        tmp_path,
        f"""
import pathlib, subprocess, sys, time
pathlib.Path({str(started)!r}).write_text("ready")
subprocess.Popen([sys.executable, "-c", {child!r}])
time.sleep(30)
""",
    )
    backend = ClaudeCodeBackend(executable=executable, timeout_seconds=30)
    task = asyncio.create_task(
        backend.run(ExternalAgentTask(task_id="task-7", instruction="cancel safely"))
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
