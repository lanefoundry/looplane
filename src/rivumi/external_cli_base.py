"""Shared harness for external coding CLIs driven through a structured CLI surface.

OpenCode, Pi, and OMP are sibling agent runtimes, not Rivumi's native harness. Each owns
its model loop, login, permissions, and session; Rivumi only delegates a bounded task to a
disposable clone and audits the result. The child retains the host environment (including its
own provider credentials) and is never used as a subscription or model-provider proxy.

Backends subclass :class:`StreamJsonCliBackend`, supplying the exact headless command line and
a tolerant normalizer that maps the tool's JSON event stream into Rivumi's provider-neutral
``ExternalAgentEvent`` shape. Event schemas differ per tool and are finalized against live
captures in the M13 stage report; the normalizers below are deliberately permissive so a
successful run still surfaces assistant text and tool activity.
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

from rivumi.backends import (
    ExternalAgentEvent,
    ExternalAgentResult,
    ExternalAgentTask,
    ExternalEventSink,
    ExternalRunStatus,
)
from rivumi.runtime import run_bounded_command

_SECRET_ENV_MARKERS = ("API", "AUTH", "CREDENTIAL", "PASSWORD", "SECRET", "TOKEN")


class StreamJsonCliBackend:
    """Delegate one task to an installed coding CLI that emits a JSON event stream.

    Subclasses set ``backend_name`` and implement :meth:`_argv`, :meth:`_normalize_event`,
    and :meth:`_summary`. The base handles executable discovery, an isolated temporary
    environment, bounded subprocess execution, and streaming event forwarding.
    """

    backend_name: str
    local_only: bool = True
    experimental: bool = True

    def __init__(
        self,
        *,
        executable: str | Path = "",
        model: str | None = None,
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
        self.executable = str(executable) or self.backend_name
        self.model = self._validate_model(model)
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

    def _resolve_executable(self) -> str:
        candidate = self.executable
        if os.path.dirname(candidate):
            path = Path(candidate).expanduser().resolve(strict=False)
            if not path.is_file() or not os.access(path, os.X_OK):
                raise FileNotFoundError(f"{self.backend_name} executable is unavailable")
            return str(path)
        resolved = shutil.which(candidate, path=self._source_env().get("PATH"))
        if resolved is None:
            raise FileNotFoundError(f"{self.backend_name} executable is unavailable")
        return resolved

    def _source_env(self) -> Mapping[str, str]:
        return os.environ if self._host_env is None else self._host_env

    def _controlled_env(self, temporary_home: Path) -> dict[str, str]:
        # The child owns its provider credentials and auth state, so the full source
        # environment is forwarded (do not strip provider API keys). Only redirect
        # transient/cache state and disable interactive git prompts.
        env = dict(self._source_env())
        env["GIT_ASKPASS"] = "/usr/bin/false"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["TMPDIR"] = str(temporary_home / "tmp")
        env["XDG_CACHE_HOME"] = str(temporary_home / "cache")
        Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
        return env

    def _input(self, task: ExternalAgentTask) -> str | None:
        """Stdin payload; most headless CLIs take the prompt as a positional argument."""

        return None

    def _argv(self, executable: str, instruction: str) -> tuple[str, ...]:
        raise NotImplementedError

    def _normalize_event(self, sequence: int, value: dict[str, Any]) -> ExternalAgentEvent | None:
        raise NotImplementedError

    def _summary(self, events: tuple[ExternalAgentEvent, ...]) -> str:
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
        return next((event.text for event in reversed(events) if event.text), "")

    def _normalize(self, stdout: str) -> tuple[tuple[ExternalAgentEvent, ...], bool]:
        events: list[ExternalAgentEvent] = []
        malformed = False
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
            events, _ = self._normalize(raw_line)
            for event in events:
                event = event.model_copy(update={"sequence": sequence})
                try:
                    await event_sink.emit(event)
                except BaseException:
                    cancel_event.set()
                    raise
                sequence += 1

    def _status_from_events(
        self, events: tuple[ExternalAgentEvent, ...], *, returncode: int
    ) -> tuple[ExternalRunStatus, str]:
        if returncode != 0:
            return ExternalRunStatus.FAILED, "external_agent_error"
        return ExternalRunStatus.COMPLETED, "completed"

    async def run(
        self,
        task: ExternalAgentTask,
        *,
        working_directory: Path | None = None,
        event_sink: ExternalEventSink | None = None,
    ) -> ExternalAgentResult:
        try:
            executable = self._resolve_executable()
        except OSError:
            return ExternalAgentResult(
                backend_name=self.backend_name,
                task_id=task.task_id,
                status=ExternalRunStatus.FAILED,
                terminal_reason="executable_unavailable",
            )

        with tempfile.TemporaryDirectory(prefix=f"rivumi-{self.backend_name}-") as raw_directory:
            directory = Path(raw_directory)
            child_directory = (
                working_directory.resolve(strict=True)
                if working_directory is not None
                else directory
            )
            if not child_directory.is_dir():
                raise ValueError("working_directory must be a directory")
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
                    self._argv(executable, task.instruction),
                    cwd=child_directory,
                    timeout_seconds=self.timeout_seconds,
                    max_output_chars=self.max_output_bytes,
                    env=self._controlled_env(directory),
                    stdin=self._input(task),
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

        events, malformed = self._normalize(result.stdout)
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
        else:
            status, reason = self._status_from_events(events, returncode=result.returncode)

        return ExternalAgentResult(
            backend_name=self.backend_name,
            task_id=task.task_id,
            status=status,
            summary=self._summary(events),
            events=events,
            terminal_reason=reason,
            exit_code=result.returncode,
        )
