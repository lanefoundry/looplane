"""Local experimental delegation to the user-installed official Claude Code CLI.

This backend launches the official external runtime. PCA never opens, stores, refreshes,
or forwards Claude credentials; the child retains ``HOME`` so the official CLI can resolve
its own authentication. It must not be used as a subscription proxy.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import threading
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from coding_agent.backends import (
    ExternalAgentEvent,
    ExternalAgentResult,
    ExternalAgentTask,
    ExternalEventSink,
    ExternalRunStatus,
)
from coding_agent.runtime import bounded_text, run_bounded_command

_SAFE_ENV_KEYS = {
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "SHELL",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "TZ",
    "USER",
}
_SECRET_ENV_MARKERS = ("API", "AUTH", "CREDENTIAL", "PASSWORD", "SECRET", "TOKEN")


class ClaudeCodeBackend:
    """Delegate a task to local Claude Code through its documented stream-JSON CLI.

    The executable owns its authentication, loop, permissions, and session.  The
    backend passes no credential values and uses an ephemeral working directory. The official
    child may access the auth state it owns through the retained user ``HOME``.
    """

    backend_name = "claude-code"
    local_only = True
    experimental = True

    def __init__(
        self,
        *,
        executable: str | Path = "claude",
        timeout_seconds: float = 300.0,
        max_input_bytes: int = 128_000,
        max_output_bytes: int = 1_000_000,
        max_event_bytes: int = 64_000,
        max_events: int = 512,
        host_env: Mapping[str, str] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if min(max_input_bytes, max_output_bytes, max_event_bytes, max_events) <= 0:
            raise ValueError("backend limits must be positive")
        self.executable = str(executable)
        self.timeout_seconds = timeout_seconds
        self.max_input_bytes = max_input_bytes
        self.max_output_bytes = max_output_bytes
        self.max_event_bytes = max_event_bytes
        self.max_events = max_events
        self._host_env = host_env

    def _resolve_executable(self) -> str:
        candidate = self.executable
        if os.path.dirname(candidate):
            path = Path(candidate).expanduser().resolve(strict=False)
            if not path.is_file() or not os.access(path, os.X_OK):
                raise FileNotFoundError("Claude Code executable is unavailable")
            return str(path)
        resolved = shutil.which(candidate, path=self._source_env().get("PATH"))
        if resolved is None:
            raise FileNotFoundError("Claude Code executable is unavailable")
        return resolved

    def _source_env(self) -> Mapping[str, str]:
        return os.environ if self._host_env is None else self._host_env

    def _controlled_env(self, temporary_home: Path) -> dict[str, str]:
        source = self._source_env()
        env = {key: value for key, value in source.items() if key in _SAFE_ENV_KEYS}
        env["PATH"] = env.get("PATH", os.defpath)
        # Keep the user's HOME so the official executable can own and locate its auth.
        # Temporary/cache state is redirected away from that home.
        if "HOME" not in env:
            env["HOME"] = str(temporary_home)
        env.update(
            {
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "GIT_ASKPASS": "/usr/bin/false",
                "GIT_TERMINAL_PROMPT": "0",
                "TMPDIR": str(temporary_home / "tmp"),
                "XDG_CACHE_HOME": str(temporary_home / "cache"),
            }
        )
        Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
        assert not any(
            marker in key.upper()
            for key in env
            for marker in _SECRET_ENV_MARKERS
            if key != "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"
        )
        return env

    def _input(self, task: ExternalAgentTask) -> str:
        line = json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": task.instruction},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload = f"{line}\n"
        if len(payload.encode("utf-8")) > self.max_input_bytes:
            raise ValueError("external-agent input exceeds max_input_bytes")
        return payload

    def _argv(self, executable: str) -> tuple[str, ...]:
        return (
            executable,
            "--print",
            "--safe-mode",
            "--disable-slash-commands",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--tools=",
            "--permission-mode",
            "plan",
        )

    def _normalize(self, stdout: str) -> tuple[tuple[ExternalAgentEvent, ...], bool]:
        events: list[ExternalAgentEvent] = []
        malformed = False
        for raw_line in stdout.splitlines():
            if not raw_line.strip():
                continue
            if len(events) >= self.max_events:
                malformed = True
                break
            if len(raw_line.encode("utf-8")) > self.max_event_bytes:
                malformed = True
                continue
            try:
                value = json.loads(raw_line)
            except json.JSONDecodeError:
                malformed = True
                continue
            if not isinstance(value, dict) or not isinstance(value.get("type"), str):
                malformed = True
                continue
            event = self._normalize_event(len(events), value)
            if event is not None:
                events.append(event)
        return tuple(events), malformed

    def _normalize_event(
        self, sequence: int, value: dict[str, Any]
    ) -> ExternalAgentEvent | None:
        event_type = value["type"]
        if event_type == "assistant":
            message = value.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            text_parts = [
                item["text"]
                for item in content or ()
                if isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            ]
            text = bounded_text("".join(text_parts), self.max_event_bytes) or None
            return ExternalAgentEvent(
                sequence=sequence,
                event_type="message",
                text=text,
                data={"source": "claude-code"},
            )
        if event_type == "result":
            text = value.get("result")
            data: dict[str, Any] = {"source": "claude-code"}
            if isinstance(value.get("is_error"), bool):
                data["is_error"] = value["is_error"]
            if isinstance(value.get("subtype"), str):
                data["subtype"] = value["subtype"]
            return ExternalAgentEvent(
                sequence=sequence,
                event_type="result",
                text=bounded_text(text, self.max_event_bytes) if isinstance(text, str) else None,
                data=data,
            )
        if event_type in {"system", "rate_limit_event"}:
            data = {"source": "claude-code"}
            if isinstance(value.get("subtype"), str):
                data["subtype"] = value["subtype"]
            return ExternalAgentEvent(
                sequence=sequence,
                event_type=event_type,
                data=data,
            )
        # Ignore unknown payloads instead of retaining arbitrary provider data.
        return None

    @staticmethod
    def _summary(events: tuple[ExternalAgentEvent, ...]) -> str:
        result_text = next(
            (
                event.text
                for event in reversed(events)
                if event.event_type == "result" and event.text
            ),
            None,
        )
        if result_text is not None:
            return result_text
        return next(
            (event.text for event in reversed(events) if event.text),
            "",
        )

    async def run(
        self,
        task: ExternalAgentTask,
        *,
        event_sink: ExternalEventSink | None = None,
    ) -> ExternalAgentResult:
        payload = self._input(task)
        try:
            executable = self._resolve_executable()
        except OSError:
            return ExternalAgentResult(
                backend_name=self.backend_name,
                task_id=task.task_id,
                status=ExternalRunStatus.FAILED,
                terminal_reason="executable_unavailable",
            )

        with tempfile.TemporaryDirectory(prefix="pca-claude-") as raw_directory:
            directory = Path(raw_directory)
            cancel_event = threading.Event()
            command_task = asyncio.create_task(
                asyncio.to_thread(
                    run_bounded_command,
                    self._argv(executable),
                    cwd=directory,
                    timeout_seconds=self.timeout_seconds,
                    max_output_chars=self.max_output_bytes,
                    env=self._controlled_env(directory),
                    stdin=payload,
                    cancel_event=cancel_event,
                )
            )
            try:
                result = await asyncio.shield(command_task)
            except asyncio.CancelledError:
                cancel_event.set()
                with suppress(asyncio.CancelledError):
                    await asyncio.shield(command_task)
                raise

        events, malformed = self._normalize(result.stdout)
        if event_sink is not None:
            for event in events:
                await event_sink.emit(event)

        result_events = tuple(event for event in events if event.event_type == "result")
        if result.timed_out:
            status = ExternalRunStatus.TIMED_OUT
            reason = "timeout"
        elif result.stdout_truncated or result.stderr_truncated:
            status = ExternalRunStatus.FAILED
            reason = "output_limit_exceeded"
        elif result.returncode != 0:
            status = ExternalRunStatus.FAILED
            reason = "external_agent_error"
        elif malformed:
            status = ExternalRunStatus.FAILED
            reason = "malformed_event_stream"
        elif len(result_events) != 1:
            status = ExternalRunStatus.FAILED
            reason = "invalid_result_count"
        elif result_events[-1].data.get("is_error") is True:
            status = ExternalRunStatus.FAILED
            reason = "external_agent_error"
        elif (
            result_events[-1].data.get("is_error") is not False
            or result_events[-1].data.get("subtype") != "success"
        ):
            status = ExternalRunStatus.FAILED
            reason = "invalid_result_event"
        else:
            status = ExternalRunStatus.COMPLETED
            reason = "completed"

        return ExternalAgentResult(
            backend_name=self.backend_name,
            task_id=task.task_id,
            status=status,
            summary=self._summary(events),
            events=events,
            terminal_reason=reason,
            exit_code=result.returncode,
        )
