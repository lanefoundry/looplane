from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from looplane.backends import (
    ExternalAgentBackend,
    ExternalAgentResult,
    ExternalAgentTask,
    ExternalRunStatus,
)
from looplane.contracts import Limits, RunStatus, TaskContract, VerificationCommand
from looplane.external_runner import ExternalCodingRunner
from looplane.omp_backend import OmpBackend
from looplane.opencode_backend import OpenCodeBackend
from looplane.pi_backend import PiBackend

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "m13"

BACKENDS = {"pi": PiBackend, "omp": OmpBackend, "opencode": OpenCodeBackend}


def _recorded_stream(runtime: str) -> str:
    return (FIXTURE_DIR / f"{runtime}.jsonl").read_text(encoding="utf-8")


def _task(repository: Path, *, allowed_paths=("src/**",)) -> TaskContract:
    return TaskContract(
        repository=repository,
        instruction="Fix the calculator addition bug.",
        allowed_paths=allowed_paths,
        verification=(
            VerificationCommand(name="tests", argv=("pytest", "-q"), timeout_seconds=30),
        ),
        limits=Limits(wall_time_seconds=60),
        task_id="external-fixture",
    )


class RecordedStreamBackend:
    """Replays a real captured stream through the real backend normalizer.

    The event stream is genuine (captured from the installed CLI); the workspace edit is
    synthetic so the runner's diff/verification pipeline has something to reconcile. This proves
    the runner consumes each real backend's normalized output end-to-end.
    """

    def __init__(
        self,
        *,
        base: ExternalAgentBackend,
        recorded_stream: str,
        fail: bool = False,
    ) -> None:
        self._base = base
        self._recorded = recorded_stream
        self._fail = fail
        self.backend_name = base.backend_name
        self.local_only = True
        self.experimental = True

    async def run(
        self,
        task: ExternalAgentTask,
        *,
        working_directory: Path | None = None,
        event_sink=None,
    ) -> ExternalAgentResult:
        assert working_directory is not None
        target = working_directory / "src/tiny_python_bug/calculator.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.write_text(
                target.read_text(encoding="utf-8").replace("left - right", "left + right"),
                encoding="utf-8",
            )
        else:
            target.write_text("outside policy\n", encoding="utf-8")
        events, _ = self._base._normalize(self._recorded)
        if self._fail:
            return ExternalAgentResult(
                backend_name=self.backend_name,
                task_id=task.task_id,
                status=ExternalRunStatus.FAILED,
                terminal_reason="external_agent_error",
                events=events,
            )
        return ExternalAgentResult(
            backend_name=self.backend_name,
            task_id=task.task_id,
            status=ExternalRunStatus.COMPLETED,
            summary=f"{self.backend_name} recorded replay",
            events=events,
            terminal_reason="completed",
            exit_code=0,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime,expected_tool",
    [("pi", "bash"), ("omp", "read"), ("opencode", "bash")],
)
async def test_recorded_stream_normalizes_per_runtime(runtime: str, expected_tool: str) -> None:
    base = BACKENDS[runtime](executable=runtime)
    events, malformed = base._normalize(_recorded_stream(runtime))

    assert not malformed
    tool_events = [e for e in events if e.event_type == "tool"]
    message_events = [e for e in events if e.event_type == "message" and e.text]
    assert tool_events, f"{runtime} should emit a tool event"
    assert any(e.data.get("tool") == expected_tool for e in tool_events)
    assert message_events, f"{runtime} should surface assistant message text"


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["pi", "omp", "opencode"])
async def test_recorded_stream_runner_verifies_patch(
    runtime: str, tmp_path: Path, tiny_bug_repo: Path
) -> None:
    backend: RecordedStreamBackend = RecordedStreamBackend(
        base=BACKENDS[runtime](executable=runtime),
        recorded_stream=_recorded_stream(runtime),
    )
    runner = ExternalCodingRunner(
        _task(tiny_bug_repo),
        backend,  # type: ignore[arg-type]
        tmp_path / "runs",
        allow_external_modify=True,
        allow_unsafe_local_exec=True,
    )

    result = await runner.run()

    assert result.status is RunStatus.COMPLETED
    assert result.terminal_reason == "verified"
    assert result.changed_files == ("src/tiny_python_bug/calculator.py",)


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["pi", "omp", "opencode"])
async def test_recorded_stream_runner_surfaces_external_agent_error(
    runtime: str, tmp_path: Path, tiny_bug_repo: Path
) -> None:
    error_stream = _recorded_stream("opencode.error") if runtime == "opencode" else ""
    backend = RecordedStreamBackend(
        base=BACKENDS[runtime](executable=runtime),
        recorded_stream=error_stream,
        fail=True,
    )
    runner = ExternalCodingRunner(
        _task(tiny_bug_repo),
        backend,  # type: ignore[arg-type]
        tmp_path / "runs",
        allow_external_modify=True,
        allow_unsafe_local_exec=True,
    )

    result = await runner.run()

    assert result.status is RunStatus.FAILED
    assert result.terminal_reason == "external_agent_error"
    assert result.error is not None
    assert runtime in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["pi", "omp", "opencode"])
async def test_recorded_stream_runner_cancellation(
    runtime: str, tmp_path: Path, tiny_bug_repo: Path
) -> None:
    backend = RecordedStreamBackend(
        base=BACKENDS[runtime](executable=runtime),
        recorded_stream=_recorded_stream(runtime),
    )
    runner = ExternalCodingRunner(
        _task(tiny_bug_repo),
        backend,  # type: ignore[arg-type]
        tmp_path / "runs",
        allow_external_modify=True,
        allow_unsafe_local_exec=True,
    )

    async def _cancel_soon() -> None:
        await asyncio.sleep(0.05)
        runner.request_cancel()

    asyncio.create_task(_cancel_soon())
    result = await runner.run()

    assert result.status is RunStatus.CANCELLED
    assert result.terminal_reason == "user_cancelled"
