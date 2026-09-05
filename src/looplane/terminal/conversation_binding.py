"""UI attachment, captured event delivery, resource cleanup, and writer handoff.

Runtime iteration and lifecycle remain in ConversationController and ConversationStore.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from looplane.approvals import ApprovalDecision
from looplane.conversation import ConversationEventKind, ConversationStore, ConversationWriterLease
from looplane.conversation_runtime import (
    CompactionCompletedEvent,
    ConversationRuntimeEvent,
    TextDeltaEvent,
)
from looplane.events import RunEvent
from looplane.external_agents import ExternalAgentEvent
from looplane.runtime_semantics import ContextCheckpoint
from looplane.terminal.events import (
    ConversationRuntimeEventMessage,
    ExternalRunEventMessage,
    RunEventMessage,
)
from looplane.terminal.types import TuiResource

EventMessage = RunEventMessage | ExternalRunEventMessage | ConversationRuntimeEventMessage


@dataclass(frozen=True)
class ViewToken:
    epoch: int
    generation: int


@dataclass(frozen=True)
class WriteTarget:
    token: ViewToken
    lease: ConversationWriterLease
    turn_id: str | None


class ConversationBinding:
    """Own UI attachment identities, never an additional agent session."""

    def __init__(
        self, store: ConversationStore | None, post_message: Callable[[EventMessage], bool]
    ) -> None:
        self.store = store
        self.post_message = post_message
        self.generation = 0
        self.epoch = 0
        self.closed = False
        self.conversation_id: str | None = None
        self.lease: ConversationWriterLease | None = None
        self.turn_id: str | None = None
        self.has_chunk = False
        self.received_messages: set[int] = set()
        self.resources: list[TuiResource] = []
        self._write_lock = asyncio.Lock()
        self._retired_leases: list[ConversationWriterLease] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._revisions: dict[str, int] = {}
        self._approvals: set[asyncio.Future[ApprovalDecision]] = set()

    def capture(self) -> ViewToken:
        return ViewToken(self.epoch, self.generation)

    def current(self, token: ViewToken) -> bool:
        return not self.closed and token == self.capture()

    def accepts(self, message: EventMessage) -> bool:
        return (
            not self.closed
            and message.generation == self.generation
            and (message.attachment_epoch is None or message.attachment_epoch == self.epoch)
        )

    def revision(self, channel: str) -> int:
        value = self._revisions.get(channel, 0) + 1
        self._revisions[channel] = value
        return value

    def revision_current(self, channel: str, revision: int, token: ViewToken) -> bool:
        return self.current(token) and self._revisions.get(channel) == revision

    def write_target(self) -> WriteTarget | None:
        if self.lease is None:
            return None
        return WriteTarget(self.capture(), self.lease, self.turn_id)

    def _target_current(self, target: WriteTarget) -> bool:
        return (
            self.current(target.token)
            and self.lease is target.lease
            and target.lease.active
            and target.turn_id == self.turn_id
        )

    async def append(
        self,
        target: WriteTarget | None,
        kind: ConversationEventKind,
        *,
        text: str | None = None,
        reason: str | None = None,
        error: str | None = None,
    ) -> bool:
        if target is None or self.store is None:
            return False
        async with self._write_lock:
            if not self._target_current(target):
                return False
            try:
                await self.store.append(
                    target.lease,
                    kind,
                    turn_id=target.turn_id,
                    text=text,
                    reason=reason,
                    error=error,
                )
                return True
            finally:
                # Store.append drains its shielded operation before returning/raising.
                self._release_retired()

    async def checkpoint(
        self,
        target: WriteTarget | None,
        operation: Callable[[ConversationWriterLease], Awaitable[object]],
    ) -> bool:
        if target is None:
            return False
        async with self._write_lock:
            if not self._target_current(target):
                return False
            try:
                pending = asyncio.ensure_future(operation(target.lease))
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    await pending
                    raise
                return True
            finally:
                self._release_retired()

    def retire_turn(self, target: WriteTarget | None) -> None:
        if target is not None and self._target_current(target):
            self.turn_id = None
            self.has_chunk = False

    async def record(
        self,
        event: ExternalAgentEvent | ConversationRuntimeEvent,
        token: ViewToken,
        target: WriteTarget | None,
        *,
        external: bool,
    ) -> None:
        if not self.current(token):
            return
        text = (
            event.text
            if isinstance(event, TextDeltaEvent)
            or (
                external and isinstance(event, ExternalAgentEvent) and event.event_type == "message"
            )
            else None
        )
        if not text:
            return
        if target is not None and target.turn_id is not None:
            recorded = await self.append(target, ConversationEventKind.ASSISTANT_CHUNK, text=text)
            if recorded and self._target_current(target):
                self.has_chunk = True
        if self.current(token):
            self.received_messages.add(token.generation)

    def invalidate(self) -> None:
        self.epoch += 1
        for decision in self._approvals:
            if not decision.done():
                decision.set_result(ApprovalDecision.CANCEL)
        self._approvals.clear()

    def watch_approval(self, decision: asyncio.Future[ApprovalDecision]) -> None:
        self._approvals.add(decision)

    def forget_approval(self, decision: asyncio.Future[ApprovalDecision]) -> None:
        self._approvals.discard(decision)

    def release_conversation(self) -> None:
        self.invalidate()
        lease = self.lease
        self.conversation_id = None
        self.lease = None
        self.turn_id = None
        self.has_chunk = False
        if lease is not None:
            if self._write_lock.locked():
                self._retired_leases.append(lease)
            else:
                lease.release()

    def _release_retired(self) -> None:
        for lease in self._retired_leases:
            lease.release()
        self._retired_leases.clear()

    def remember_resource(self, resource: TuiResource) -> None:
        self.resources[:] = [
            other
            for other in self.resources
            if other is resource or not getattr(other, "is_closed", False)
        ]
        if resource not in self.resources:
            self.resources.append(resource)

    async def close_resources(self) -> None:
        # Operate on captured identities; new attachments are never cleared by an old close.
        captured = tuple(self.resources)
        errors: list[str] = []
        for resource in reversed(captured):
            try:
                await resource.aclose()
            except Exception as exc:
                errors.append(str(exc))
            else:
                self.resources[:] = [other for other in self.resources if other is not resource]
        if errors:
            raise RuntimeError("; ".join(errors))

    def spawn(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def close(self) -> None:
        self.closed = True
        self.release_conversation()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._write_lock:
            self._release_retired()
        await self.close_resources()


class TextualEventSink:
    """One captured UI subscription to the controller's existing event delivery."""

    def __init__(self, binding: ConversationBinding, generation: int) -> None:
        self.binding = binding
        self.token = ViewToken(binding.epoch, generation)
        self.target = binding.write_target()
        self.generation = generation

    async def emit(self, event: RunEvent | ExternalAgentEvent | ConversationRuntimeEvent) -> None:
        if not self.binding.current(self.token):
            return
        if isinstance(event, RunEvent):
            message = RunEventMessage(event, self.generation, attachment_epoch=self.token.epoch)
        elif isinstance(event, ExternalAgentEvent):
            await self.binding.record(event, self.token, self.target, external=True)
            message = ExternalRunEventMessage(
                event, self.generation, attachment_epoch=self.token.epoch
            )
        else:
            await self.binding.record(event, self.token, self.target, external=False)
            message = ConversationRuntimeEventMessage(
                event, self.generation, attachment_epoch=self.token.epoch
            )
        if self.binding.current(self.token):
            self.binding.post_message(message)


class RecordingConversationEventSink:
    """Collect an already canonical checkpoint while forwarding the same subscription."""

    def __init__(self, wrapped: TextualEventSink) -> None:
        self.wrapped = wrapped
        self.compaction_checkpoint: ContextCheckpoint | None = None

    async def emit(self, event: ConversationRuntimeEvent) -> None:
        if not self.wrapped.binding.current(self.wrapped.token):
            return
        if isinstance(event, CompactionCompletedEvent):
            self.compaction_checkpoint = event.checkpoint
        await self.wrapped.emit(event)
