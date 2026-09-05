from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from looplane.agent import run_lifecycle as lifecycle_module
from looplane.approvals import ApprovalDecision, ApprovalReason
from looplane.contracts import (
    Limits,
    ModelTurn,
    RunStatus,
    TaskContract,
    ToolCall,
    VerificationCommand,
)
from looplane.events import RunEvent
from looplane.loop import AgentRunner
from looplane.models import ScriptedModel

FIX_PATCH = """\
diff --git a/src/tiny_python_bug/calculator.py b/src/tiny_python_bug/calculator.py
--- a/src/tiny_python_bug/calculator.py
+++ b/src/tiny_python_bug/calculator.py
@@ -1,3 +1,3 @@
 def add(left: int, right: int) -> int:
     \"\"\"Return the sum of two integers.\"\"\"
-    return left - right
+    return left + right
"""


class AdvancingApprovalPolicy:
    def __init__(self, clock: list[float], *, wait_seconds: float) -> None:
        self.clock = clock
        self.wait_seconds = wait_seconds
        self.requests = []

    async def decide(self, request):
        self.requests.append(request)
        self.clock[0] += self.wait_seconds
        return ApprovalDecision.ALLOW_ONCE


class RecordingEventSink:
    def __init__(
        self,
        clock: list[float],
        *,
        final_approval_active_seconds: float = 0.0,
    ) -> None:
        self.clock = clock
        self.final_approval_active_seconds = final_approval_active_seconds
        self.events: list[RunEvent] = []

    async def emit(self, event: RunEvent) -> None:
        self.events.append(event)
        if (
            event.event_type == "approval.resolved"
            and event.data.get("reason") == ApprovalReason.FINAL_VERIFICATION.value
        ):
            self.clock[0] += self.final_approval_active_seconds


class BlockingApprovalPolicy:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def decide(self, _request):
        self.started.set()
        await asyncio.Future()


def task_for(repository: Path) -> TaskContract:
    return TaskContract(
        repository=repository,
        instruction="Fix the calculator.",
        allowed_paths=("src/**",),
        verification=(
            VerificationCommand(name="check-1", argv=("git", "diff", "--check")),
        ),
        limits=Limits(max_steps=2, wall_time_seconds=0.5),
    )


def model_for_edit() -> ScriptedModel:
    return ScriptedModel(
        [
            ModelTurn(tool_calls=(ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),)),
            ModelTurn(content="The calculator is fixed and ready for verification."),
        ]
    )


@pytest.mark.asyncio
async def test_approval_wait_does_not_consume_active_wall_time(
    tiny_bug_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(lifecycle_module.time, "monotonic", lambda: clock[0])
    approvals = AdvancingApprovalPolicy(clock, wait_seconds=10.0)
    events = RecordingEventSink(clock)

    result = await AgentRunner(
        task_for(tiny_bug_repo),
        model_for_edit(),
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
        approval_policy=approvals,
        event_sink=events,
    ).run()

    assert clock[0] == 120.0
    assert len(approvals.requests) == 2
    assert result.status is RunStatus.COMPLETED
    assert result.verification[0].ok is True
    assert any(event.event_type == "verification.completed" for event in events.events)


@pytest.mark.asyncio
async def test_active_expiry_after_final_approval_is_phase_accurate(
    tiny_bug_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(lifecycle_module.time, "monotonic", lambda: clock[0])
    approvals = AdvancingApprovalPolicy(clock, wait_seconds=10.0)
    events = RecordingEventSink(clock, final_approval_active_seconds=1.0)

    result = await AgentRunner(
        task_for(tiny_bug_repo),
        model_for_edit(),
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
        approval_policy=approvals,
        event_sink=events,
    ).run()

    event_types = [event.event_type for event in events.events]
    assert result.status is RunStatus.FAILED
    assert result.terminal_reason == "wall_time_exceeded"
    assert "The calculator is fixed and ready for verification." in result.summary
    assert "Final verification exceeded the remaining wall-time budget." in result.summary
    assert "Model request exceeded" not in result.summary
    assert "verification.started" not in event_types


@pytest.mark.asyncio
async def test_cancelled_approval_persists_only_active_time(
    tiny_bug_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(lifecycle_module.time, "monotonic", lambda: clock[0])
    approvals = BlockingApprovalPolicy()
    runner = AgentRunner(
        task_for(tiny_bug_repo),
        model_for_edit(),
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
        approval_policy=approvals,
    )

    run_task = asyncio.create_task(runner.run())
    await asyncio.wait_for(approvals.started.wait(), timeout=2)
    clock[0] += 30.0
    waiting = json.loads((runner.run_dir / "session.json").read_text())
    assert waiting["phase"] == "waiting_approval"
    assert waiting["active_started_at"] is None
    assert waiting["active_wall_time_seconds"] < 0.5

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    cancelled = json.loads((runner.run_dir / "session.json").read_text())
    assert cancelled["active_started_at"] is None
    assert cancelled["active_wall_time_seconds"] < 0.5
