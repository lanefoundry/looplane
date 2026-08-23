from __future__ import annotations

import asyncio

import pytest

from rivumi.approvals import ApprovalDecision, ToolEffect
from rivumi.contracts import RunStatus
from rivumi.conversation_controller import ConversationController
from rivumi.conversation_runtime import (
    ApprovalRequestedEvent,
    CompactionCompletedEvent,
    CompactionStartedEvent,
    ConversationProtocolError,
    RuntimeApprovalKind,
    RuntimeApprovalRequest,
    RuntimeToolKind,
    RuntimeToolStatus,
    RuntimeTurnStatus,
    TextDeltaEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
    TurnCompletedEvent,
)
from rivumi.runtime_semantics import RuntimeCapabilities


class RecordingSink:
    def __init__(self) -> None:
        self.events = []

    async def emit(self, event) -> None:
        self.events.append(event)


class FakeSession:
    def __init__(self) -> None:
        self.started = 0
        self.closed = 0
        self.turn_number = 0
        self.queue: asyncio.Queue = asyncio.Queue()
        self.responses = []
        self.interrupts = []
        self.suppress_interrupt_terminal = False
        self.block_interrupt = False
        self.fail_interrupt = False
        self.iterator_closed = 0

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(native_compaction=True)

    async def start(self) -> None:
        self.started += 1

    async def send_turn(self, _text: str) -> str:
        self.turn_number += 1
        return f"turn-{self.turn_number}"

    async def compact_context(self, guidance: str | None = None) -> str:
        turn_id = "compact-1"
        await self.queue.put(
            CompactionStartedEvent(sequence=90, turn_id=turn_id, guidance=guidance)
        )
        await self.queue.put(CompactionCompletedEvent(sequence=91, turn_id=turn_id))
        return turn_id

    async def events(self):
        try:
            while True:
                yield await self.queue.get()
        finally:
            self.iterator_closed += 1

    async def respond_approval(self, request_id, decision) -> None:
        self.responses.append((request_id, decision))

    async def interrupt(self, turn_id: str) -> None:
        self.interrupts.append(turn_id)
        if self.block_interrupt:
            await asyncio.Event().wait()
        if self.fail_interrupt:
            raise RuntimeError("interrupt failed")
        if self.suppress_interrupt_terminal:
            return
        await self.queue.put(
            TurnCompletedEvent(
                sequence=99,
                turn_id=turn_id,
                status=RuntimeTurnStatus.INTERRUPTED,
            )
        )

    async def aclose(self) -> None:
        self.closed += 1


async def test_two_turns_share_session_and_project_typed_actions() -> None:
    session = FakeSession()
    controller = ConversationController(session)
    sink = RecordingSink()

    first = controller.turn(
        "Fix it",
        event_sink=sink,
        approval_callback=lambda _event: asyncio.sleep(0, result=ApprovalDecision.ALLOW_ONCE),
    )
    task = asyncio.create_task(first.run())
    await asyncio.sleep(0)
    events = (
        TextDeltaEvent(sequence=0, turn_id="turn-1", text="Working"),
        ToolStartedEvent(
            sequence=1,
            turn_id="turn-1",
            action_id="action-1",
            kind=RuntimeToolKind.FILE_CHANGE,
            tool_name="Update",
            effect=ToolEffect.MODIFY,
            path="src/app.py",
        ),
        ApprovalRequestedEvent(
            sequence=2,
            turn_id="turn-1",
            approval=RuntimeApprovalRequest(
                request_id="approval-1",
                turn_id="turn-1",
                action_id="action-1",
                kind=RuntimeApprovalKind.FILE_CHANGE,
                effect=ToolEffect.MODIFY,
                preview="diff",
                available_decisions=(
                    ApprovalDecision.ALLOW_ONCE,
                    ApprovalDecision.DENY,
                ),
            ),
        ),
        ToolCompletedEvent(
            sequence=3,
            turn_id="turn-1",
            action_id="action-1",
            status=RuntimeToolStatus.COMPLETED,
            diff="diff",
        ),
        TurnCompletedEvent(
            sequence=4,
            turn_id="turn-1",
            status=RuntimeTurnStatus.COMPLETED,
        ),
    )
    for event in events:
        await session.queue.put(event)
    result = await task
    assert result.status == RunStatus.COMPLETED
    assert result.summary == "Working"
    assert result.changed_files == ("src/app.py",)
    assert session.responses == [("approval-1", ApprovalDecision.ALLOW_ONCE)]

    second = controller.turn(
        "Continue",
        event_sink=sink,
        approval_callback=lambda _event: asyncio.sleep(0, result=ApprovalDecision.DENY),
    )
    second_task = asyncio.create_task(second.run())
    await asyncio.sleep(0)
    await session.queue.put(
        TurnCompletedEvent(
            sequence=5,
            turn_id="turn-2",
            status=RuntimeTurnStatus.COMPLETED,
        )
    )
    assert (await second_task).status == RunStatus.COMPLETED
    assert session.started == 1


async def test_native_compaction_drains_its_lifecycle_before_the_next_turn() -> None:
    session = FakeSession()
    controller = ConversationController(session)

    assert await controller.compact_context("keep failures") == "compact-1"
    assert session.queue.empty()

    handle = controller.turn(
        "Continue",
        event_sink=RecordingSink(),
        approval_callback=lambda _event: asyncio.sleep(0, result=ApprovalDecision.DENY),
    )
    task = asyncio.create_task(handle.run())
    await asyncio.sleep(0)
    await session.queue.put(
        TurnCompletedEvent(
            sequence=92,
            turn_id="turn-1",
            status=RuntimeTurnStatus.COMPLETED,
        )
    )
    assert (await task).status == RunStatus.COMPLETED


async def test_failed_turn_preserves_error_summary_and_partial_changes() -> None:
    session = FakeSession()
    controller = ConversationController(session)
    sink = RecordingSink()
    handle = controller.turn(
        "Research it",
        event_sink=sink,
        approval_callback=lambda _event: asyncio.sleep(0, result=ApprovalDecision.ALLOW_ONCE),
    )
    task = asyncio.create_task(handle.run())
    await asyncio.sleep(0)
    events = (
        TextDeltaEvent(sequence=0, turn_id="turn-1", text="Useful partial answer."),
        ToolStartedEvent(
            sequence=1,
            turn_id="turn-1",
            action_id="action-1",
            kind=RuntimeToolKind.FILE_CHANGE,
            tool_name="file_change",
            effect=ToolEffect.MODIFY,
            path=".codex-task.md",
        ),
        ToolCompletedEvent(
            sequence=2,
            turn_id="turn-1",
            action_id="action-1",
            status=RuntimeToolStatus.COMPLETED,
            diff="+ task notes",
        ),
        TurnCompletedEvent(
            sequence=3,
            turn_id="turn-1",
            status=RuntimeTurnStatus.FAILED,
            error="Workspace audit failed: reported paths did not match",
        ),
    )
    for event in events:
        await session.queue.put(event)

    result = await task

    assert result.status == RunStatus.FAILED
    assert result.summary == "Useful partial answer."
    assert result.error == "Workspace audit failed: reported paths did not match"
    assert result.terminal_reason == "conversation_turn_failed"
    assert result.changed_files == (".codex-task.md",)


async def test_cancel_interrupts_and_waits_for_terminal() -> None:
    session = FakeSession()
    controller = ConversationController(session)
    handle = controller.turn(
        "Stop me",
        event_sink=RecordingSink(),
        approval_callback=lambda _event: asyncio.sleep(0, result=ApprovalDecision.DENY),
    )
    task = asyncio.create_task(handle.run())
    await asyncio.sleep(0)
    handle.request_cancel()
    result = await asyncio.wait_for(task, timeout=1)
    assert result.status == RunStatus.CANCELLED
    assert session.interrupts == ["turn-1"]


async def test_cancel_closes_session_when_runtime_never_emits_terminal() -> None:
    session = FakeSession()
    session.suppress_interrupt_terminal = True
    controller = ConversationController(session, interrupt_grace_seconds=0.01)
    handle = controller.turn(
        "Stop me",
        event_sink=RecordingSink(),
        approval_callback=lambda _event: asyncio.sleep(0, result=ApprovalDecision.DENY),
    )
    task = asyncio.create_task(handle.run())
    await asyncio.sleep(0)
    handle.request_cancel()

    result = await asyncio.wait_for(task, timeout=1)

    assert result.status == RunStatus.CANCELLED
    assert session.interrupts == ["turn-1"]
    assert session.closed == 1


async def test_cancel_bounds_a_blocking_interrupt_and_closes_iterator() -> None:
    session = FakeSession()
    session.block_interrupt = True
    controller = ConversationController(session, interrupt_grace_seconds=0.01)
    handle = controller.turn(
        "Stop me",
        event_sink=RecordingSink(),
        approval_callback=lambda _event: asyncio.sleep(0, result=ApprovalDecision.DENY),
    )
    task = asyncio.create_task(handle.run())
    await asyncio.sleep(0)
    handle.request_cancel()

    result = await asyncio.wait_for(task, timeout=1)

    assert result.status == RunStatus.CANCELLED
    assert session.closed == 1
    assert session.iterator_closed == 1


async def test_interrupt_failure_cancels_pending_event_read_and_closes_iterator() -> None:
    session = FakeSession()
    session.fail_interrupt = True
    controller = ConversationController(session, interrupt_grace_seconds=0.01)
    handle = controller.turn(
        "Stop me",
        event_sink=RecordingSink(),
        approval_callback=lambda _event: asyncio.sleep(0, result=ApprovalDecision.DENY),
    )
    task = asyncio.create_task(handle.run())
    await asyncio.sleep(0)
    handle.request_cancel()

    with pytest.raises(RuntimeError, match="interrupt failed"):
        await asyncio.wait_for(task, timeout=1)

    assert session.closed == 1
    assert session.iterator_closed == 1


async def test_cross_turn_event_fails_closed() -> None:
    session = FakeSession()
    controller = ConversationController(session)
    handle = controller.turn(
        "Question",
        event_sink=RecordingSink(),
        approval_callback=lambda _event: asyncio.sleep(0, result=ApprovalDecision.DENY),
    )
    task = asyncio.create_task(handle.run())
    await asyncio.sleep(0)
    await session.queue.put(TextDeltaEvent(sequence=0, turn_id="wrong-turn", text="leak"))
    with pytest.raises(ConversationProtocolError, match="another active turn"):
        await task


async def test_close_is_idempotent() -> None:
    session = FakeSession()
    controller = ConversationController(session)
    await controller.aclose()
    await controller.aclose()
    assert session.closed == 1
