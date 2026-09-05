"""Responsibility contracts for native state, context, and bounded-run persistence."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from looplane.agent import context, run_lifecycle
from looplane.agent.checkpoints import RunPersistence, check_resume_identity, session_phase
from looplane.agent.run_lifecycle import BoundedRunLifecycle
from looplane.agent.state import ActiveRunClock, ContextState, TurnState
from looplane.contracts import (
    ConversationItem,
    InjectedContext,
    Limits,
    Message,
    RunResult,
    RunStatus,
    TaskContract,
    ToolCall,
    ToolObservation,
    Usage,
    VerificationCommand,
)
from looplane.events import RunEvent
from looplane.prompts import (
    CONTEXT_PRESSURE_REMINDER_VERSION,
    CONTEXT_SUMMARY_FALLBACK_VERSION,
    WORKSPACE_CONTEXT_REMINDER_VERSION,
)
from looplane.session import SessionManifest, SessionPhase, SessionValidationError


class RecordingSink:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.events: list[RunEvent] = []
        self.fail = False

    async def emit(self, event: RunEvent) -> None:
        # The manifest must already describe this event when delivery begins.
        persisted = json.loads((self.run_dir / "session.json").read_text())
        assert persisted["last_event_sequence"] == event.sequence
        if self.fail:
            raise OSError("event append failed")
        self.events.append(event)


@pytest.fixture
def task(tmp_path: Path) -> TaskContract:
    repository = tmp_path / "source"
    repository.mkdir()
    return TaskContract(
        task_id="state-context",
        repository=repository,
        instruction="Inspect the task context.",
        allowed_paths=("src/**",),
        verification=(VerificationCommand(name="check", argv=("git", "diff", "--check")),),
        base_sha="a" * 40,
        limits=Limits(max_steps=10, max_total_tokens=100),
    )


@pytest.fixture
async def persisted(
    tmp_path: Path, task: TaskContract
) -> AsyncIterator[tuple[RunPersistence, RecordingSink]]:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    sink = RecordingSink(run_dir)
    persistence = RunPersistence("run", run_dir, sink)
    await persistence.initialize(
        task,
        durable=False,
        provider_name="fixture",
        model_id="fixture",
        protocol="fixture",
        base_sha="a" * 40,
    )
    try:
        yield persistence, sink
    finally:
        assert persistence.lease is not None
        persistence.lease.release()


async def test_manifest_precedes_events_and_failed_delivery_keeps_sequence(
    persisted: tuple[RunPersistence, RecordingSink], task: TaskContract
) -> None:
    persistence, sink = persisted
    state = TurnState(messages=[Message(role="user", content="persist me")], step=3)
    clock = ActiveRunClock(active_wall_time_base=2.5)
    await persistence.emit(task.task_id, state, clock, "model.requested", step=3)
    assert persistence.sequence == 1
    assert persistence.manifest is not None
    assert persistence.manifest.messages == tuple(state.messages)
    assert persistence.manifest.active_wall_time_seconds == 2.5

    sink.fail = True
    with pytest.raises(OSError, match="event append failed"):
        await persistence.emit(task.task_id, state, clock, "model.completed")
    assert persistence.sequence == 1
    assert persistence.manifest.last_event_sequence == 1
    assert [event.sequence for event in sink.events] == [0]

    sink.fail = False
    await persistence.emit(task.task_id, state, clock, "model.completed")
    assert [event.sequence for event in sink.events] == [0, 1]
    assert persistence.sequence == 2


async def test_manifest_failure_prevents_event_delivery(
    persisted: tuple[RunPersistence, RecordingSink],
    task: TaskContract,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persistence, sink = persisted

    async def fail_save() -> None:
        raise OSError("manifest failed")

    monkeypatch.setattr(persistence, "save", fail_save)
    with pytest.raises(OSError, match="manifest failed"):
        await persistence.emit(task.task_id, TurnState(), ActiveRunClock(), "model.requested")
    assert sink.events == []
    assert persistence.sequence == 0


@pytest.mark.parametrize("status", list(RunStatus))
async def test_checkpoint_records_turn_state_and_terminal_phase(
    persisted: tuple[RunPersistence, RecordingSink], task: TaskContract, status: RunStatus
) -> None:
    persistence, _sink = persisted
    state = TurnState(
        messages=[
            Message(
                role="assistant",
                tool_calls=(ToolCall(name="read_file", arguments={"path": "src/main.py"}),),
            )
        ],
        step=2,
        usage=Usage(input_tokens=20, output_tokens=3),
        repeat_count=1,
        last_fingerprint="action",
        verified_workspace_fingerprint="verified",
    )
    await persistence.checkpoint(task.task_id, state, status, last_tool="read_file")
    checkpoint = json.loads((persistence.run_dir / "checkpoint.json").read_text())
    manifest = json.loads((persistence.run_dir / "session.json").read_text())
    assert checkpoint["tool_call_count"] == 1
    assert checkpoint["step"] == manifest["step"] == 2
    assert checkpoint["messages"] == manifest["messages"]
    assert checkpoint["usage"] == {**manifest["usage"], "total_tokens": 23}
    assert checkpoint["active_writer_token"] == persistence.writer_token
    assert checkpoint["metadata"] == {"last_tool": "read_file"}
    assert manifest["repeat_count"] == 1
    assert manifest["verified_workspace_fingerprint"] == "verified"
    assert manifest["phase"] == session_phase(status)
    assert manifest["terminal"] is (
        status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
    )


def test_restore_retains_messages_usage_and_marker_versions() -> None:
    messages = (
        Message(role="system", content="system"),
        InjectedContext(source="context_pressure", content=CONTEXT_PRESSURE_REMINDER_VERSION),
        InjectedContext(
            source="history_summary_fallback", content=CONTEXT_SUMMARY_FALLBACK_VERSION
        ),
        InjectedContext(
            source="workspace_context_reminder", content=WORKSPACE_CONTEXT_REMINDER_VERSION
        ),
    )
    manifest = SessionManifest.new(
        run_id="resume",
        task_id="task",
        provider_name="fixture",
        model_id="fixture",
        protocol="fixture",
        base_sha="a" * 40,
    ).model_copy(
        update={
            "messages": messages,
            "step": 4,
            "usage": Usage(input_tokens=85),
            "last_action_fingerprint": "action",
            "repeat_count": 2,
            "verified_workspace_fingerprint": "verified",
        }
    )
    state = TurnState()
    state.restore(manifest)
    markers = ContextState()
    markers.restore(state.messages)
    assert tuple(state.messages) == messages
    assert state.step == 4
    assert state.usage.total_tokens == 85
    assert state.last_fingerprint == "action"
    assert state.verified_workspace_fingerprint == "verified"
    assert state.repeat_count == 0  # Existing resume deliberately starts a fresh repeat budget.
    assert markers.context_pressure_reminder_sent
    assert markers.history_summary_fallback_applied
    assert markers.workspace_context_reminder_sent
    state.messages.clear()
    assert manifest.messages == messages
    markers.restore([InjectedContext(source="context_pressure", content="old marker")])
    assert not markers.context_pressure_reminder_sent


def test_context_returns_additions_without_mutating_history(task: TaskContract) -> None:
    messages: list[ConversationItem] = [Message(role="user", content="keep me")]
    state = ContextState(history_summary_fallback_applied=True)
    update = context.context_pressure_reminder(task, state, Usage(input_tokens=85))
    assert len(update.additions) == 1
    assert update.events[0].event_type == "context_pressure.reminder_injected"
    assert context.context_pressure_reminder(task, state, Usage(input_tokens=90)).additions == ()
    workspace = context.workspace_context_reminder(task, state, messages, (), 2, ("src/main.py",))
    assert len(workspace.additions) == 1
    assert "src/main.py" in (workspace.additions[0].content or "")
    assert workspace.events[0].data["changed_files"] == ("src/main.py",)
    assert messages == [Message(role="user", content="keep me")]
    assert not context.needs_workspace_reminder(state)


def test_compaction_keeps_tool_observations_with_their_call(task: TaskContract) -> None:
    call = ToolCall(name="read_file", arguments={"path": "src/main.py"})
    messages: list[ConversationItem] = [
        Message(role="system", content="system"),
        Message(role="user", content="task"),
        Message(role="assistant", content="older"),
        Message(role="assistant", tool_calls=(call,)),
        ToolObservation(tool_call_id=call.tool_call_id, name=call.name, ok=True, content="file"),
        Message(role="user", content="tail one"),
        Message(role="assistant", content="tail two"),
        Message(role="user", content="tail three"),
    ]
    original = list(messages)
    state = ContextState()
    plan = context.plan_history_compaction(task, state, messages, Usage(input_tokens=85))
    assert plan is not None
    assert (plan.start, plan.end) == (2, 5)
    summary = context.history_summary(task, messages, plan)
    assert summary.source == "history_summary_fallback"
    assert messages == original
    assert not state.history_summary_fallback_applied
    assert plan.hook_payload()["compaction"]["source_message_count"] == 3


async def test_lifecycle_cancellation_excludes_paused_time_and_releases_lease(
    persisted: tuple[RunPersistence, RecordingSink], monkeypatch: pytest.MonkeyPatch
) -> None:
    persistence, _sink = persisted
    clock = [100.0]
    monkeypatch.setattr(run_lifecycle.time, "monotonic", lambda: clock[0])
    lifecycle = BoundedRunLifecycle(persistence)

    async def engine() -> RunResult:
        clock[0] += 2
        await lifecycle.pause_active_wall_time()
        clock[0] += 30
        assert lifecycle.current_active_wall_time() == 2
        await lifecycle.resume_active_wall_time()
        clock[0] += 1
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await lifecycle.run(engine)
    assert persistence.lease is not None and not persistence.lease.active
    manifest = json.loads((persistence.run_dir / "session.json").read_text())
    assert manifest["active_wall_time_seconds"] == 3
    assert manifest["active_started_at"] is None


async def test_lifecycle_restores_consumed_time_and_returns_engine_result(
    persisted: tuple[RunPersistence, RecordingSink], monkeypatch: pytest.MonkeyPatch
) -> None:
    persistence, _sink = persisted
    assert persistence.manifest is not None
    persistence.manifest = persistence.manifest.model_copy(
        update={
            "active_wall_time_seconds": 7.0,
            "active_started_at": datetime.now(UTC) - timedelta(seconds=5),
            "phase": SessionPhase.RUNNING,
        }
    )
    monkeypatch.setattr(run_lifecycle.time, "monotonic", lambda: 100.0)
    lifecycle = BoundedRunLifecycle(persistence)
    result = RunResult(
        run_id="run",
        task_id="state-context",
        status=RunStatus.COMPLETED,
        terminal_reason="fixture",
    )

    async def engine() -> RunResult:
        assert lifecycle.current_active_wall_time() >= 12
        return result

    assert await lifecycle.run(engine) is result
    assert persistence.lease is not None and not persistence.lease.active
    assert lifecycle.clock.active_started_at is None


@pytest.mark.parametrize("field", ["provider_name", "model_id", "protocol"])
async def test_resume_identity_rejects_each_mismatched_dimension(
    persisted: tuple[RunPersistence, RecordingSink], field: str
) -> None:
    persistence, _sink = persisted
    assert persistence.manifest is not None
    identity = {"provider_name": "fixture", "model_id": "fixture", "protocol": "fixture"}
    check_resume_identity(persistence.manifest, **identity)
    identity[field] = "different"
    with pytest.raises(SessionValidationError, match="must match"):
        check_resume_identity(persistence.manifest, **identity)
