"""Contract tests for M13 external CLI backends (OpenCode, Pi, OMP).

Each backend is exercised against a fake executable that replays a recorded JSON-lines
stream, proving the argv shape and the normalizer mapping without depending on the real
vendor binary being installed.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from looplane.backends import ExternalAgentEvent, ExternalAgentTask
from looplane.external_cli_base import StreamJsonCliBackend
from looplane.omp_backend import OmpBackend
from looplane.opencode_backend import OpenCodeBackend
from looplane.pi_backend import PiBackend


class _CollectingSink:
    def __init__(self) -> None:
        self.events: list[ExternalAgentEvent] = []

    async def emit(self, event: ExternalAgentEvent) -> None:
        self.events.append(event)


def _fake_executable(tmp_path: Path, payload: str) -> str:
    script = tmp_path / "fake_cli"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "payload = os.environ.get('FAKE_STREAM', '')\n"
        "print(payload)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


def _normalized_via_run(backend: StreamJsonCliBackend, payload: str, tmp_path: Path) -> object:
    executable = _fake_executable(tmp_path, payload)
    sink = _CollectingSink()
    backend.executable = executable
    backend._host_env = {**os.environ, "FAKE_STREAM": payload}
    task = ExternalAgentTask(instruction="do the thing", task_id="task-1")
    import asyncio

    return asyncio.run(backend.run(task, event_sink=sink))


PI_STREAM = "\n".join(
    [
        '{"type":"session"}',
        '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"Hello "}}',
        '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"world"}}',
        '{"type":"message_update","assistantMessageEvent":{"type":"toolcall_start","toolName":"read"}}',
        '{"type":"message_update","assistantMessageEvent":{"type":"toolcall_end","toolName":"read"}}',
        '{"type":"message_end","message":{"content":[{"text":"Final answer"}]}}',
    ]
)

OPENCODE_STREAM = "\n".join(
    [
        '{"type":"session"}',
        '{"type":"text","content":"Thinking..."}',
        '{"type":"tool_use","name":"edit","input":{}}',
        '{"type":"result","content":"Done","is_error":false}',
    ]
)


def test_pi_argv_shape(tmp_path: Path) -> None:
    backend = PiBackend(executable="pi")
    assert backend._argv("pi", "prompt") == ("pi", "--mode", "json", "prompt")
    assert backend._argv("pi", "prompt")[-1] == "prompt"


def test_pi_normalizes_message_and_tool_events(tmp_path: Path) -> None:
    backend = PiBackend(executable="pi")
    payload = PI_STREAM
    events, malformed = backend._normalize(payload)
    messages = [e for e in events if e.event_type == "message"]
    tools = [e for e in events if e.event_type == "tool"]
    assert "".join(e.text or "" for e in messages) == "Hello worldFinal answer"
    assert tools and tools[0].data.get("tool") == "read"


def test_pi_end_to_end_fake_cli(tmp_path: Path) -> None:
    backend = PiBackend(executable="pi")
    result = _normalized_via_run(backend, PI_STREAM, tmp_path)
    assert result.status.value == "completed"
    assert result.summary == "Final answer"
    message_text = "".join(e.text or "" for e in result.events if e.event_type == "message")
    assert "Hello world" in message_text


def test_omp_reuses_pi_vocabulary(tmp_path: Path) -> None:
    backend = OmpBackend(executable="omp")
    assert backend._argv("omp", "prompt") == ("omp", "--mode", "json", "prompt")
    events, malformed = backend._normalize(PI_STREAM)
    assert any(e.event_type == "message" for e in events)


def test_opencode_normalizes_events(tmp_path: Path) -> None:
    backend = OpenCodeBackend(executable="opencode")
    assert backend._argv("opencode", "prompt") == (
        "opencode",
        "run",
        "--format",
        "json",
        "--dangerously-skip-permissions",
        "prompt",
    )
    events, malformed = backend._normalize(OPENCODE_STREAM)
    messages = [e for e in events if e.event_type == "message"]
    tools = [e for e in events if e.event_type == "tool"]
    results = [e for e in events if e.event_type == "result"]
    assert "".join(e.text or "" for e in messages) == "Thinking..."
    assert tools and tools[0].data.get("tool") == "edit"
    assert results and results[0].text == "Done"
    assert results[0].data.get("is_error") is not True


def test_opencode_error_marks_failure(tmp_path: Path) -> None:
    backend = OpenCodeBackend(executable="opencode")
    stream = '{"type":"error","message":"boom"}'
    events, malformed = backend._normalize(stream)
    assert events and events[0].data.get("is_error") is True


def test_opencode_error_nested_schema(tmp_path: Path) -> None:
    backend = OpenCodeBackend(executable="opencode")
    stream = '{"type":"error","error":{"name":"UnknownError","data":{"message":"Model not found"}}}'
    events, malformed = backend._normalize(stream)
    assert events and events[0].data.get("is_error") is True
    assert events[0].text == "Model not found"


OPENCODE_SUCCESS = "\n".join(
    [
        '{"type":"step_start","part":{"type":"step-start"}}',
        '{"type":"tool_use","part":{"type":"tool","tool":"bash","callID":"c1",'
        '"state":{"status":"completed","input":{"command":"ls -a"}}}}',
        '{"type":"step_finish","part":{"type":"step-finish","reason":"tool-calls"}}',
        '{"type":"text","part":{"type":"text","text":"README.md\\nsrc\\ntests"}}',
        '{"type":"step_finish","part":{"type":"step-finish","reason":"stop"}}',
    ]
)


def test_opencode_success_schema(tmp_path: Path) -> None:
    backend = OpenCodeBackend(executable="opencode")
    events, malformed = backend._normalize(OPENCODE_SUCCESS)
    messages = [e for e in events if e.event_type == "message"]
    tools = [e for e in events if e.event_type == "tool"]
    assert "".join(e.text or "" for e in messages) == "README.md\nsrc\ntests"
    assert tools and tools[0].data.get("tool") == "bash"
    assert events[0].event_type == "tool"


def test_pi_toolcall_end_captures_tool_name(tmp_path: Path) -> None:
    backend = PiBackend(executable="pi")
    stream = "\n".join(
        [
            '{"type":"message_update","assistantMessageEvent":{"type":"toolcall_start"}}',
            '{"type":"message_update","assistantMessageEvent":{"type":"toolcall_end",'
            '"toolCall":{"type":"toolCall","id":"1","name":"bash","arguments":{}}}}',
        ]
    )
    events, malformed = backend._normalize(stream)
    tools = [e for e in events if e.event_type == "tool"]
    assert tools and tools[-1].data.get("tool") == "bash"
