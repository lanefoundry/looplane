"""Bounded local delegation to the user-installed official Codex CLI.

This is an external coding-agent backend, not a model transport. The child owns
its authentication and agent loop; Rivumi never opens or copies ``~/.codex``.
Callers must provide an already prepared disposable workspace and remain
responsible for approval, verification, and final patch acceptance.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import threading
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from rivumi.backends import (
    ExternalAgentEvent,
    ExternalAgentResult,
    ExternalAgentTask,
    ExternalEventSink,
    ExternalRunStatus,
)
from rivumi.runtime import bounded_text, run_bounded_command

_SAFE_ENV_KEYS = {
    "CODEX_HOME",
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
_SECRET_ENV_MARKERS = ("API_KEY", "AUTH", "CREDENTIAL", "PASSWORD", "SECRET", "TOKEN")
_SANDBOX_MODES = frozenset({"read-only", "workspace-write"})
_ITEM_TYPES = frozenset(
    {
        "agent_message",
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "reasoning",
        "todo_list",
        "web_search",
    }
)
_SAFE_TYPE_NAME = re.compile(r"^[a-z0-9_.-]{1,64}$")


class CodexCliBackend:
    """Run one task through official ``codex exec`` in a disposable workspace."""

    backend_name = "codex-cli"
    local_only = True
    experimental = True
    native_instruction_suppression_args = ("--ignore-user-config", "--ignore-rules")
    native_instruction_suppression_note = (
        "Codex CLI wrapper launches with --ignore-user-config and --ignore-rules."
    )

    def __init__(
        self,
        *,
        working_directory: Path | None = None,
        executable: str | Path = "codex",
        model: str | None = None,
        sandbox_mode: str = "workspace-write",
        timeout_seconds: float = 300.0,
        max_input_bytes: int = 128_000,
        max_output_bytes: int = 1_000_000,
        max_event_bytes: int = 64_000,
        max_events: int = 512,
        host_env: Mapping[str, str] | None = None,
    ) -> None:
        if sandbox_mode not in _SANDBOX_MODES:
            raise ValueError("sandbox_mode must be read-only or workspace-write")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if min(max_input_bytes, max_output_bytes, max_event_bytes, max_events) <= 0:
            raise ValueError("backend limits must be positive")
        self.working_directory = (
            Path(working_directory).expanduser() if working_directory is not None else None
        )
        self.executable = str(executable)
        self.model = self._validate_model(model)
        self.sandbox_mode = sandbox_mode
        self.timeout_seconds = timeout_seconds
        self.max_input_bytes = max_input_bytes
        self.max_output_bytes = max_output_bytes
        self.max_event_bytes = max_event_bytes
        self.max_events = max_events
        self._host_env = host_env

    @staticmethod
    def _validate_model(model: str | None) -> str | None:
        if model is None:
            return None
        normalized = model.strip()
        if not normalized or len(normalized) > 256 or not normalized.isprintable():
            raise ValueError("model must be a printable model name")
        return normalized

    def _source_env(self) -> Mapping[str, str]:
        return os.environ if self._host_env is None else self._host_env

    def _resolve_executable(self) -> str:
        candidate = self.executable
        if os.path.dirname(candidate):
            resolved = Path(candidate).expanduser().resolve(strict=False)
            if not resolved.is_file() or not os.access(resolved, os.X_OK):
                raise FileNotFoundError("Codex CLI executable is unavailable")
            return str(resolved)
        resolved = shutil.which(candidate, path=self._source_env().get("PATH"))
        if resolved is None:
            raise FileNotFoundError("Codex CLI executable is unavailable")
        return resolved

    def _resolve_working_directory(self, override: Path | None) -> Path:
        candidate = Path(override).expanduser() if override is not None else self.working_directory
        if candidate is None:
            raise ValueError("Codex working directory is required")
        if candidate.is_symlink():
            raise ValueError("Codex working directory cannot be a symlink")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("Codex working directory must be an existing directory")
        return resolved

    def _controlled_env(self, working_directory: Path) -> dict[str, str]:
        source = self._source_env()
        env = {key: value for key, value in source.items() if key in _SAFE_ENV_KEYS}
        env["PATH"] = env.get("PATH", os.defpath)
        # HOME/CODEX_HOME are paths, not exported credentials. The official child
        # remains the only process that interprets the auth state it owns there.
        if "HOME" not in env:
            env["HOME"] = str(working_directory / ".rivumi-codex-home")
        env.update(
            {
                "GIT_ASKPASS": "/usr/bin/false",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "NO_COLOR": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        assert not any(marker in key.upper() for key in env for marker in _SECRET_ENV_MARKERS)
        return env

    def _input(self, task: ExternalAgentTask) -> str:
        payload = f"{task.instruction}\n"
        if len(payload.encode("utf-8")) > self.max_input_bytes:
            raise ValueError("external-agent input exceeds max_input_bytes")
        return payload

    def _argv(self, executable: str, working_directory: Path) -> tuple[str, ...]:
        argv = (
            executable,
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            self.sandbox_mode,
            "--color",
            "never",
            "--skip-git-repo-check",
            "-C",
            str(working_directory),
        )
        if self.model is not None:
            argv = (*argv, "--model", self.model)
        return (*argv, "-")

    def _normalize_item(
        self,
        *,
        sequence: int,
        event_type: str,
        item: dict[str, Any],
    ) -> ExternalAgentEvent | None:
        item_type = item.get("type")
        if not isinstance(item_type, str) or item_type not in _ITEM_TYPES:
            raise ValueError("unsupported Codex item type")
        phase = event_type.removeprefix("item.")
        if item_type == "agent_message":
            if event_type != "item.completed":
                return None
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("completed Codex agent message has no text")
            return ExternalAgentEvent(
                sequence=sequence,
                event_type="message",
                text=bounded_text(text, self.max_event_bytes),
                data={"source": "codex-cli"},
            )
        data: dict[str, Any] = {
            "source": "codex-cli",
            "item_type": item_type,
            "phase": phase,
        }
        status = item.get("status")
        if isinstance(status, str) and status in {
            "in_progress",
            "completed",
            "failed",
            "declined",
        }:
            data["status"] = status
        return ExternalAgentEvent(
            sequence=sequence,
            event_type="activity",
            data=data,
        )

    def _normalize(
        self, stdout: str
    ) -> tuple[
        tuple[ExternalAgentEvent, ...],
        bool,
        int,
        int,
        int,
        bool,
    ]:
        events: list[ExternalAgentEvent] = []
        malformed = False
        thread_starts = 0
        turn_starts = 0
        terminals = 0
        external_error = False
        raw_events = 0
        for raw_line in stdout.splitlines():
            if not raw_line.strip():
                continue
            if raw_events >= self.max_events:
                malformed = True
                break
            raw_events += 1
            if len(raw_line.encode("utf-8")) > self.max_event_bytes:
                malformed = True
                continue
            value: Any = None
            try:
                value = json.loads(raw_line)
                if not isinstance(value, dict) or not isinstance(value.get("type"), str):
                    raise ValueError
                event_type = value["type"]
                event: ExternalAgentEvent | None
                if event_type == "thread.started":
                    thread_starts += 1
                    event = ExternalAgentEvent(
                        sequence=len(events),
                        event_type="system",
                        data={"source": "codex-cli", "subtype": "thread_started"},
                    )
                elif event_type == "turn.started":
                    turn_starts += 1
                    event = ExternalAgentEvent(
                        sequence=len(events),
                        event_type="system",
                        data={"source": "codex-cli", "subtype": "turn_started"},
                    )
                elif event_type in {"item.started", "item.updated", "item.completed"}:
                    item = value.get("item")
                    if not isinstance(item, dict):
                        raise ValueError
                    event = self._normalize_item(
                        sequence=len(events),
                        event_type=event_type,
                        item=item,
                    )
                elif event_type == "turn.completed":
                    terminals += 1
                    event = ExternalAgentEvent(
                        sequence=len(events),
                        event_type="result",
                        data={"source": "codex-cli", "is_error": False},
                    )
                elif event_type in {"turn.failed", "error"}:
                    if event_type == "turn.failed":
                        terminals += 1
                    external_error = True
                    event = ExternalAgentEvent(
                        sequence=len(events),
                        event_type="error",
                        data={"source": "codex-cli"},
                    )
                else:
                    raise ValueError
            except (json.JSONDecodeError, ValueError):
                malformed = True
                top_level_type = value.get("type") if isinstance(value, dict) else None
                item = value.get("item") if isinstance(value, dict) else None
                item_type = item.get("type") if isinstance(item, dict) else None
                safe_top_level = (
                    top_level_type
                    if isinstance(top_level_type, str) and _SAFE_TYPE_NAME.fullmatch(top_level_type)
                    else "invalid"
                )
                safe_item = (
                    item_type
                    if isinstance(item_type, str) and _SAFE_TYPE_NAME.fullmatch(item_type)
                    else None
                )
                data = {"source": "codex-cli", "top_level_type": safe_top_level}
                if safe_item is not None:
                    data["item_type"] = safe_item
                events.append(
                    ExternalAgentEvent(
                        sequence=len(events),
                        event_type="protocol_drift",
                        data=data,
                    )
                )
                continue
            if event is not None:
                events.append(event)
        return (
            tuple(events),
            malformed,
            thread_starts,
            turn_starts,
            terminals,
            external_error,
        )

    @staticmethod
    def _summary(events: tuple[ExternalAgentEvent, ...]) -> str:
        return next(
            (
                event.text
                for event in reversed(events)
                if event.event_type == "message" and event.text
            ),
            "",
        )

    async def _consume_stdout_lines(
        self,
        queue: asyncio.Queue[tuple[str, bool] | None],
        event_sink: ExternalEventSink,
        cancel_event: threading.Event,
    ) -> None:
        raw_events = 0
        sequence = 0
        while True:
            item = await queue.get()
            if item is None:
                return
            raw_line, line_truncated = item
            if not raw_line.strip():
                continue
            raw_events += 1
            if line_truncated or raw_events > self.max_events:
                continue
            events, *_ = self._normalize(raw_line)
            for event in events:
                event = event.model_copy(update={"sequence": sequence})
                try:
                    await event_sink.emit(event)
                except BaseException:
                    cancel_event.set()
                    raise
                sequence += 1

    async def run(
        self,
        task: ExternalAgentTask,
        *,
        working_directory: Path | None = None,
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
        try:
            resolved_working_directory = self._resolve_working_directory(working_directory)
        except (OSError, ValueError):
            return ExternalAgentResult(
                backend_name=self.backend_name,
                task_id=task.task_id,
                status=ExternalRunStatus.FAILED,
                terminal_reason="working_directory_unavailable",
            )

        cancel_event = threading.Event()
        line_queue: asyncio.Queue[tuple[str, bool] | None] | None = None
        consumer_task: asyncio.Task[None] | None = None
        stdout_line_callback = None
        if event_sink is not None:
            line_queue = asyncio.Queue()
            loop = asyncio.get_running_loop()
            queued_lines = 0

            def stdout_line_callback(line: str, truncated: bool) -> None:
                nonlocal queued_lines
                if not line.strip():
                    return
                queued_lines += 1
                if queued_lines > self.max_events + 1:
                    return
                loop.call_soon_threadsafe(line_queue.put_nowait, (line, truncated))

            consumer_task = asyncio.create_task(
                self._consume_stdout_lines(line_queue, event_sink, cancel_event)
            )
        command_task = asyncio.create_task(
            asyncio.to_thread(
                run_bounded_command,
                self._argv(executable, resolved_working_directory),
                cwd=resolved_working_directory,
                timeout_seconds=self.timeout_seconds,
                max_output_chars=self.max_output_bytes,
                env=self._controlled_env(resolved_working_directory),
                stdin=payload,
                cancel_event=cancel_event,
                stdout_line_callback=stdout_line_callback,
                max_stdout_line_bytes=self.max_event_bytes,
            )
        )
        try:
            result = await asyncio.shield(command_task)
        except asyncio.CancelledError:
            cancel_event.set()
            with suppress(asyncio.CancelledError):
                await asyncio.shield(command_task)
            if consumer_task is not None:
                consumer_task.cancel()
                with suppress(asyncio.CancelledError):
                    await consumer_task
            raise
        except Exception:
            cancel_event.set()
            if line_queue is not None and consumer_task is not None:
                line_queue.put_nowait(None)
                with suppress(Exception):
                    await consumer_task
            raise
        if line_queue is not None and consumer_task is not None:
            line_queue.put_nowait(None)
            await consumer_task

        (
            events,
            malformed,
            thread_starts,
            turn_starts,
            terminals,
            external_error,
        ) = self._normalize(result.stdout)
        summary = self._summary(events)
        if result.timed_out:
            status = ExternalRunStatus.TIMED_OUT
            reason = "timeout"
        elif result.stdout_truncated or result.stderr_truncated:
            status = ExternalRunStatus.FAILED
            reason = "output_limit_exceeded"
        elif result.returncode != 0 or external_error:
            status = ExternalRunStatus.FAILED
            reason = "external_agent_error"
        elif terminals != 1:
            status = ExternalRunStatus.FAILED
            reason = "invalid_terminal_count"
        elif malformed:
            status = ExternalRunStatus.FAILED
            reason = "malformed_event_stream"
        elif thread_starts != 1 or turn_starts != 1:
            status = ExternalRunStatus.FAILED
            reason = "invalid_event_lifecycle"
        elif not summary:
            status = ExternalRunStatus.FAILED
            reason = "missing_final_response"
        else:
            status = ExternalRunStatus.COMPLETED
            reason = "completed"

        return ExternalAgentResult(
            backend_name=self.backend_name,
            task_id=task.task_id,
            status=status,
            summary=summary,
            events=events,
            terminal_reason=reason,
            exit_code=result.returncode,
        )
