from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from rivumi.approvals import ApprovalDecision, ToolEffect
from rivumi.context_providers import (
    ContextProviderCommand,
    ContextProviderConfig,
    ContextProviderRunner,
)
from rivumi.contracts import RunStatus
from rivumi.conversation_controller import BackendTurnLimiter, ConversationController
from rivumi.conversation_runtime import (
    ApprovalRequestedEvent,
    CompactionCompletedEvent,
    CompactionStartedEvent,
    ConversationProtocolError,
    RuntimeApprovalKind,
    RuntimeApprovalRequest,
    RuntimeAttachment,
    RuntimeInjectedContext,
    RuntimeToolKind,
    RuntimeToolStatus,
    RuntimeTurnStatus,
    TextDeltaEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
    TurnCompletedEvent,
)
from rivumi.hooks import HookCommandConfig, HookConfig, HookRunner
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
        self.turn_texts = []
        self.interrupts = []
        self.suppress_interrupt_terminal = False
        self.block_interrupt = False
        self.fail_interrupt = False
        self.iterator_closed = 0
        self.compaction_requests = 0

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(native_compaction=True)

    async def start(self) -> None:
        self.started += 1

    async def send_turn(self, text: str) -> str:
        self.turn_number += 1
        self.turn_texts.append(text)
        return f"turn-{self.turn_number}"

    async def compact_context(self, guidance: str | None = None) -> str:
        self.compaction_requests += 1
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


class NoNativeCompactionSession(FakeSession):
    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(native_compaction=False)


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


async def test_backend_limiter_queues_concurrent_controller_turns() -> None:
    limiter = BackendTurnLimiter(max_active_turns=1)
    first_session = FakeSession()
    second_session = FakeSession()
    first = ConversationController(first_session, backend_limiter=limiter)
    second = ConversationController(second_session, backend_limiter=limiter)

    first_task = asyncio.create_task(
        first.turn(
            "First",
            event_sink=RecordingSink(),
            approval_callback=lambda _event: asyncio.sleep(
                0, result=ApprovalDecision.DENY
            ),
        ).run()
    )
    await asyncio.sleep(0)
    assert first_session.turn_number == 1

    second_task = asyncio.create_task(
        second.turn(
            "Second",
            event_sink=RecordingSink(),
            approval_callback=lambda _event: asyncio.sleep(
                0, result=ApprovalDecision.DENY
            ),
        ).run()
    )
    await asyncio.sleep(0.01)
    assert second_session.turn_number == 0

    await first_session.queue.put(
        TurnCompletedEvent(sequence=1, turn_id="turn-1", status=RuntimeTurnStatus.COMPLETED)
    )
    assert (await first_task).status == RunStatus.COMPLETED
    await asyncio.sleep(0)
    assert second_session.turn_number == 1
    await second_session.queue.put(
        TurnCompletedEvent(sequence=1, turn_id="turn-1", status=RuntimeTurnStatus.COMPLETED)
    )
    assert (await second_task).status == RunStatus.COMPLETED


async def test_injected_items_are_projected_into_next_turn_text() -> None:
    session = FakeSession()
    controller = ConversationController(session)
    accepted = controller.inject_items(
        (
            RuntimeInjectedContext(
                source="ide",
                content="active file: src/app.py",
            ),
        )
    )

    assert accepted[0].source == "ide"
    task = asyncio.create_task(
        controller.turn(
            "Continue",
            event_sink=RecordingSink(),
            approval_callback=lambda _event: asyncio.sleep(0, result=ApprovalDecision.DENY),
        ).run()
    )
    await asyncio.sleep(0)
    await session.queue.put(
        TurnCompletedEvent(sequence=1, turn_id="turn-1", status=RuntimeTurnStatus.COMPLETED)
    )
    assert (await task).status == RunStatus.COMPLETED
    assert session.turn_texts[0].startswith("[app-server-injected-context-v1]")
    assert "[injected_context:ide]\nactive file: src/app.py" in session.turn_texts[0]
    assert session.turn_texts[0].endswith("[user_turn]\nContinue")


async def test_turn_attachments_are_projected_into_turn_text() -> None:
    session = FakeSession()
    controller = ConversationController(session)
    task = asyncio.create_task(
        controller.turn(
            "Use this context",
            event_sink=RecordingSink(),
            approval_callback=lambda _event: asyncio.sleep(0, result=ApprovalDecision.DENY),
            attachments=(
                RuntimeAttachment(
                    name="notes.md",
                    media_type="text/markdown",
                    content="# Notes\nUse the cache key.",
                ),
                RuntimeAttachment(
                    name="design.pdf",
                    media_type="application/pdf",
                    uri="file:///tmp/design.pdf",
                ),
            ),
        ).run()
    )
    await asyncio.sleep(0)
    await session.queue.put(
        TurnCompletedEvent(sequence=1, turn_id="turn-1", status=RuntimeTurnStatus.COMPLETED)
    )

    assert (await task).status == RunStatus.COMPLETED
    assert "[app-server-attachments-v1]" in session.turn_texts[0]
    assert "[attachment:notes.md; media_type=text/markdown]" in session.turn_texts[0]
    assert "# Notes\nUse the cache key." in session.turn_texts[0]
    assert "[attachment:design.pdf; media_type=application/pdf]" in session.turn_texts[0]
    assert "(file reference: file:///tmp/design.pdf)" in session.turn_texts[0]


async def test_runtime_context_provider_is_projected_into_external_turn(
    tmp_path: Path,
) -> None:
    provider = tmp_path / "provider.py"
    payload_path = tmp_path / "payload.json"
    provider.write_text(
        f"""
import json
import sys

payload = json.loads(sys.stdin.read())
with open({str(payload_path)!r}, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, sort_keys=True)
print(json.dumps({{"source": "lifecycle", "content": payload["payload"]["phase"]}}))
""".lstrip(),
        encoding="utf-8",
    )
    session = FakeSession()
    controller = ConversationController(
        session,
        context_provider_runner=ContextProviderRunner(
            ContextProviderConfig(
                providers=(
                    ContextProviderCommand(
                        name="lifecycle",
                        command=(sys.executable, str(provider)),
                    ),
                )
            ),
            cwd=tmp_path,
        ),
    )

    task = asyncio.create_task(
        controller.turn(
            "Continue",
            event_sink=RecordingSink(),
            approval_callback=lambda _event: asyncio.sleep(0, result=ApprovalDecision.DENY),
            attachments=(
                RuntimeAttachment(name="notes.txt", content="note"),
            ),
        ).run()
    )
    await asyncio.sleep(0)
    await session.queue.put(
        TurnCompletedEvent(sequence=1, turn_id="turn-1", status=RuntimeTurnStatus.COMPLETED)
    )

    assert (await task).status == RunStatus.COMPLETED
    assert "[injected_context:context_provider:lifecycle]\nbefore_turn" in session.turn_texts[0]
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["provider"] == "lifecycle"
    assert payload["payload"]["phase"] == "before_turn"
    assert payload["payload"]["turn_text"] == "Continue"
    assert payload["payload"]["attachments"][0]["name"] == "notes.txt"


async def test_local_compaction_fallback_emits_checkpoint_without_native_support() -> None:
    session = NoNativeCompactionSession()
    controller = ConversationController(session)
    sink = RecordingSink()
    turn = controller.turn(
        "Summarize",
        event_sink=sink,
        approval_callback=lambda _event: asyncio.sleep(0, result=ApprovalDecision.DENY),
    )
    task = asyncio.create_task(turn.run())
    await asyncio.sleep(0)
    await session.queue.put(TextDeltaEvent(sequence=1, turn_id="turn-1", text="First answer."))
    await session.queue.put(
        TurnCompletedEvent(sequence=2, turn_id="turn-1", status=RuntimeTurnStatus.COMPLETED)
    )
    assert (await task).status == RunStatus.COMPLETED

    compact_sink = RecordingSink()
    compact_id = await controller.compact_context("keep decisions", event_sink=compact_sink)

    assert compact_id.startswith("local-compact-")
    assert len(compact_sink.events) == 1
    event = compact_sink.events[0]
    assert isinstance(event, CompactionCompletedEvent)
    assert event.checkpoint is not None
    assert event.checkpoint.summary.source_turn_ids == ("turn-1",)
    assert "First answer." in event.checkpoint.summary.text


async def test_native_compaction_drains_its_lifecycle_before_the_next_turn() -> None:
    session = FakeSession()
    controller = ConversationController(session)
    sink = RecordingSink()

    assert await controller.compact_context("keep failures", event_sink=sink) == "compact-1"
    assert session.queue.empty()
    assert [type(event) for event in sink.events] == [
        CompactionStartedEvent,
        CompactionCompletedEvent,
    ]

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


async def test_external_runtime_compaction_runs_pre_and_post_hooks(
    tmp_path: Path,
) -> None:
    hook_log = tmp_path / "hooks.jsonl"
    hook = tmp_path / "hook.py"
    hook.write_text(
        f"""
import json
import sys

payload = json.loads(sys.stdin.read())
with open({str(hook_log)!r}, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, sort_keys=True) + "\\n")
print("{{}}")
""".lstrip(),
        encoding="utf-8",
    )
    runner = HookRunner(
        HookConfig(
            pre_compact=(HookCommandConfig(command=(sys.executable, str(hook))),),
            post_compact=(HookCommandConfig(command=(sys.executable, str(hook))),),
        ),
        cwd=tmp_path,
    )
    session = FakeSession()
    controller = ConversationController(session, hook_runner=runner)

    assert await controller.compact_context("keep failures") == "compact-1"

    payloads = [json.loads(line) for line in hook_log.read_text(encoding="utf-8").splitlines()]
    assert [payload["event"] for payload in payloads] == ["pre_compact", "post_compact"]
    assert payloads[0]["payload"]["compaction"] == {
        "guidance": "keep failures",
        "kind": "external_runtime_compaction",
        "turn_id": None,
    }
    assert payloads[1]["payload"]["compaction"] == {
        "guidance": "keep failures",
        "kind": "external_runtime_compaction",
        "turn_id": "compact-1",
    }


async def test_external_runtime_compaction_pre_hook_can_deny(tmp_path: Path) -> None:
    hook = tmp_path / "deny.py"
    hook.write_text(
        """
import json

print(json.dumps({"decision": "deny", "reason": "compact denied"}))
""".lstrip(),
        encoding="utf-8",
    )
    runner = HookRunner(
        HookConfig(pre_compact=(HookCommandConfig(command=(sys.executable, str(hook))),)),
        cwd=tmp_path,
    )
    session = FakeSession()
    controller = ConversationController(session, hook_runner=runner)

    with pytest.raises(ConversationProtocolError, match="compact denied"):
        await controller.compact_context("keep failures")
    assert session.compaction_requests == 0


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
