from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from looplane.ask_runner import ExternalAskRunner
from looplane.backends import (
    ExternalAgentResult,
    ExternalAgentTask,
    ExternalRunStatus,
)
from looplane.contracts import RunStatus


class AskBackend:
    backend_name = "ask-fixture"
    local_only = True
    experimental = True

    def __init__(self) -> None:
        self.instruction = ""
        self.working_directory: Path | None = None

    async def run(
        self,
        task: ExternalAgentTask,
        *,
        working_directory: Path | None = None,
        event_sink=None,
    ) -> ExternalAgentResult:
        self.instruction = task.instruction
        self.working_directory = working_directory
        assert working_directory is not None
        assert list(working_directory.iterdir()) == []
        return ExternalAgentResult(
            backend_name=self.backend_name,
            task_id=task.task_id,
            status=ExternalRunStatus.COMPLETED,
            summary="Hello from Ask mode.",
            terminal_reason="completed",
            exit_code=0,
        )


@pytest.mark.asyncio
async def test_external_ask_uses_empty_temporary_directory_without_artifacts() -> None:
    backend = AskBackend()
    runner = ExternalAskRunner(instruction="hi", backend=backend)

    result = await runner.run()

    assert result.status is RunStatus.COMPLETED
    assert result.summary == "Hello from Ask mode."
    assert result.artifacts == {}
    assert backend.instruction == "hi"
    assert backend.working_directory is not None
    assert not backend.working_directory.exists()


@pytest.mark.asyncio
async def test_external_ask_cancellation_waits_for_backend_cleanup() -> None:
    started = asyncio.Event()
    cleaned = asyncio.Event()

    class BlockingBackend(AskBackend):
        async def run(self, task, *, working_directory=None, event_sink=None):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

    runner = ExternalAskRunner(instruction="wait", backend=BlockingBackend())
    task = asyncio.create_task(runner.run())
    await started.wait()
    runner.request_cancel()

    result = await task

    assert result.status is RunStatus.CANCELLED
    assert result.terminal_reason == "user_cancelled"
    assert cleaned.is_set()
