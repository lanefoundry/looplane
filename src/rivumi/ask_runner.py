"""Bounded process-local Ask mode for an external agent runtime."""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import suppress
from pathlib import Path

from rivumi.backends import (
    ExternalAgentBackend,
    ExternalAgentTask,
    ExternalEventSink,
    ExternalRunStatus,
)
from rivumi.contracts import RunResult, RunStatus


class ExternalAskRunner:
    """Run one read-only question without repository context or vendor session state."""

    def __init__(
        self,
        *,
        instruction: str,
        backend: ExternalAgentBackend,
        event_sink: ExternalEventSink | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        if not instruction.strip():
            raise ValueError("Ask instruction cannot be blank")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.instruction = instruction
        self.backend = backend
        self.event_sink = event_sink
        self.timeout_seconds = timeout_seconds
        self._cancel_requested = asyncio.Event()

    def request_cancel(self) -> None:
        self._cancel_requested.set()

    async def run(self) -> RunResult:
        with tempfile.TemporaryDirectory(prefix="rivumi-ask-") as raw_directory:
            working_directory = (
                None if self.backend.backend_name == "claude-code" else Path(raw_directory)
            )
            backend_task = asyncio.create_task(
                self.backend.run(
                    ExternalAgentTask(task_id="ask", instruction=self.instruction),
                    working_directory=working_directory,
                    event_sink=self.event_sink,
                )
            )
            cancel_task = asyncio.create_task(self._cancel_requested.wait())
            try:
                done, _ = await asyncio.wait(
                    {backend_task, cancel_task},
                    timeout=self.timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task in done:
                    backend_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await backend_task
                    return self._cancelled()
                if backend_task not in done:
                    backend_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await backend_task
                    return RunResult(
                        run_id="ask",
                        task_id="ask",
                        status=RunStatus.FAILED,
                        summary="Ask mode timed out.",
                        terminal_reason="timed_out",
                    )
                result = await backend_task
            except asyncio.CancelledError:
                backend_task.cancel()
                with suppress(asyncio.CancelledError):
                    await backend_task
                raise
            finally:
                cancel_task.cancel()
                with suppress(asyncio.CancelledError):
                    await cancel_task

        status = (
            RunStatus.COMPLETED
            if result.status == ExternalRunStatus.COMPLETED
            else RunStatus.FAILED
        )
        return RunResult(
            run_id="ask",
            task_id="ask",
            status=status,
            summary=result.summary,
            terminal_reason=result.terminal_reason,
        )

    @staticmethod
    def _cancelled() -> RunResult:
        return RunResult(
            run_id="ask",
            task_id="ask",
            status=RunStatus.CANCELLED,
            summary="Ask mode cancelled.",
            terminal_reason="user_cancelled",
        )
