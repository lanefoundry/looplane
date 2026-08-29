"""Turn orchestration for one long-lived external conversation session."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Protocol

from rivumi.approvals import (
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalReason,
    ApprovalRequest,
)
from rivumi.contracts import RunResult, RunStatus, ToolCall
from rivumi.conversation_runtime import (
    ApprovalRequestedEvent,
    CompactionCompletedEvent,
    ConversationProtocolError,
    ConversationRuntimeEvent,
    ConversationRuntimeSession,
    RuntimeTurnStatus,
    TextDeltaEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
    TurnCompletedEvent,
)
from rivumi.runtime_semantics import RuntimeCapabilities
from rivumi.startup_trace import _STARTUP


class ConversationEventSink(Protocol):
    async def emit(self, event: ConversationRuntimeEvent) -> None: ...


RuntimeApprovalCallback = Callable[[ApprovalRequestedEvent], Awaitable[ApprovalDecision]]


async def decide_runtime_approval(
    policy: ApprovalPolicy, event: ApprovalRequestedEvent
) -> ApprovalDecision:
    """Adapt a strict runtime approval to the existing provider-neutral policy."""

    approval = event.approval
    preview_parts = [approval.preview] if approval.preview else []
    preview_parts.extend(
        change.unified_diff or change.summary for change in approval.proposed_changes
    )
    request = ApprovalRequest(
        run_id=event.turn_id,
        action_id=approval.action_id,
        effect=approval.effect,
        reason=ApprovalReason.MODEL_TOOL,
        preview="\n\n".join(preview_parts)[:16_000],
        tool_call=ToolCall(
            tool_call_id=approval.action_id,
            name=f"external_{approval.kind.value}",
            arguments={
                "available_decisions": [
                    decision.value for decision in approval.available_decisions
                ],
                "grant_scope": approval.grant_scope,
            },
        ),
    )
    return await policy.decide(request)


class ConversationController:
    """Own one native session while exposing bounded, sequential turn handles."""

    persistent = True

    def __init__(
        self,
        session: ConversationRuntimeSession,
        *,
        interrupt_grace_seconds: float = 5.0,
        compaction_timeout_seconds: float = 120.0,
    ) -> None:
        if interrupt_grace_seconds <= 0 or compaction_timeout_seconds <= 0:
            raise ValueError("conversation timeouts must be positive")
        self.session = session
        self.interrupt_grace_seconds = interrupt_grace_seconds
        self.compaction_timeout_seconds = compaction_timeout_seconds
        self._started = False
        self._closed = False
        self._turn_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()

    def turn(
        self,
        text: str,
        *,
        event_sink: ConversationEventSink,
        approval_callback: RuntimeApprovalCallback,
    ) -> ConversationTurnHandle:
        if self._closed:
            raise RuntimeError("conversation controller is closed")
        return ConversationTurnHandle(
            self,
            text=text,
            event_sink=event_sink,
            approval_callback=approval_callback,
        )

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return self.session.capabilities

    @property
    def is_closed(self) -> bool:
        return self._closed

    async def compact_context(
        self,
        guidance: str | None = None,
        *,
        event_sink: ConversationEventSink | None = None,
    ) -> str:
        if self._closed:
            raise RuntimeError("conversation controller is closed")
        if not self.capabilities.native_compaction:
            raise RuntimeError("native context compaction is unavailable for this runtime")
        async with self._turn_lock:
            await self._ensure_started()
            turn_id = await self.session.compact_context(guidance)
            iterator = self.session.events().__aiter__()
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(
                            anext(iterator), timeout=self.compaction_timeout_seconds
                        )
                    except StopAsyncIteration as exc:
                        raise ConversationProtocolError(
                            "runtime event stream ended before compaction completed"
                        ) from exc
                    if event.turn_id != turn_id:
                        raise ConversationProtocolError(
                            "received an event for another context during compaction"
                        )
                    if event_sink is not None:
                        await event_sink.emit(event)
                    if isinstance(event, CompactionCompletedEvent):
                        return turn_id
            finally:
                close_iterator = getattr(iterator, "aclose", None)
                if close_iterator is not None:
                    with suppress(BaseException):
                        await close_iterator()

    async def aclose(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            await self.session.aclose()

    async def _ensure_started(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("conversation controller is closed")
            if not self._started:
                with _STARTUP.span("controller.start"):
                    await self.session.start()
                self._started = True


class ConversationTurnHandle:
    """TUI-compatible handle for one turn in a shared conversation session."""

    def __init__(
        self,
        controller: ConversationController,
        *,
        text: str,
        event_sink: ConversationEventSink,
        approval_callback: RuntimeApprovalCallback,
    ) -> None:
        if not text.strip() or "\x00" in text:
            raise ValueError("turn text must be non-blank and NUL-free")
        self.controller = controller
        self.text = text
        self.event_sink = event_sink
        self.approval_callback = approval_callback
        self._cancel_requested = asyncio.Event()
        self._turn_id: str | None = None

    def request_cancel(self) -> None:
        self._cancel_requested.set()

    async def run(self) -> RunResult:
        async with self.controller._turn_lock:
            try:
                await self.controller._ensure_started()
                self._turn_id = await self.controller.session.send_turn(self.text)
                return await self._consume_turn()
            except BaseException:
                with suppress(BaseException):
                    if self._turn_id is not None:
                        await asyncio.wait_for(
                            self.controller.session.interrupt(self._turn_id),
                            timeout=self.controller.interrupt_grace_seconds,
                        )
                with suppress(BaseException):
                    await self.controller.aclose()
                raise

    async def _consume_turn(self) -> RunResult:
        assert self._turn_id is not None
        turn_id = self._turn_id
        iterator = self.controller.session.events().__aiter__()
        text_parts: list[str] = []
        action_paths: dict[str, tuple[str, ...]] = {}
        changed_paths: set[str] = set()
        interrupted = False
        cancel_deadline: float | None = None
        try:
            while True:
                try:
                    if interrupted:
                        assert cancel_deadline is not None
                        remaining = max(0.0, cancel_deadline - asyncio.get_running_loop().time())
                        try:
                            event = await asyncio.wait_for(anext(iterator), timeout=remaining)
                        except TimeoutError:
                            await self.controller.aclose()
                            return self._cancelled_result(turn_id)
                    else:
                        event_task = asyncio.create_task(anext(iterator))
                        cancel_task = asyncio.create_task(self._cancel_requested.wait())
                        done, _ = await asyncio.wait(
                            {event_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
                        )
                        if cancel_task in done:
                            completed_event = event_task.result() if event_task in done else None
                            if isinstance(completed_event, TurnCompletedEvent):
                                event = completed_event
                            else:
                                interrupted = True
                                cancel_deadline = (
                                    asyncio.get_running_loop().time()
                                    + self.controller.interrupt_grace_seconds
                                )
                                try:
                                    await asyncio.wait_for(
                                        self.controller.session.interrupt(turn_id),
                                        timeout=self.controller.interrupt_grace_seconds,
                                    )
                                except TimeoutError:
                                    event_task.cancel()
                                    with suppress(asyncio.CancelledError, StopAsyncIteration):
                                        await event_task
                                    await self.controller.aclose()
                                    return self._cancelled_result(turn_id)
                                except BaseException:
                                    event_task.cancel()
                                    with suppress(asyncio.CancelledError, StopAsyncIteration):
                                        await event_task
                                    raise
                            if event_task not in done and interrupted:
                                assert cancel_deadline is not None
                                remaining = max(
                                    0.0,
                                    cancel_deadline - asyncio.get_running_loop().time(),
                                )
                                try:
                                    event = await asyncio.wait_for(
                                        asyncio.shield(event_task), timeout=remaining
                                    )
                                except TimeoutError:
                                    event_task.cancel()
                                    with suppress(asyncio.CancelledError, StopAsyncIteration):
                                        await event_task
                                    await self.controller.aclose()
                                    return self._cancelled_result(turn_id)
                            elif completed_event is not None:
                                event = completed_event
                        else:
                            event = event_task.result()
                        if not cancel_task.done():
                            cancel_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await cancel_task
                except StopAsyncIteration as exc:
                    raise ConversationProtocolError(
                        "conversation event stream ended before the turn completed"
                    ) from exc
                if event.turn_id != turn_id:
                    raise ConversationProtocolError("received an event for another active turn")
                await self.event_sink.emit(event)
                if isinstance(event, TextDeltaEvent):
                    text_parts.append(event.text)
                elif isinstance(event, ToolStartedEvent) and (event.paths or event.path):
                    action_paths[event.action_id] = event.paths or (
                        (event.path,) if event.path else ()
                    )
                elif isinstance(event, ToolCompletedEvent):
                    if event.diff and (paths := action_paths.get(event.action_id)):
                        changed_paths.update(paths)
                elif isinstance(event, ApprovalRequestedEvent):
                    decision = await self.approval_callback(event)
                    if decision not in event.approval.available_decisions:
                        raise ConversationProtocolError(
                            "approval callback returned an unavailable decision"
                        )
                    await self.controller.session.respond_approval(
                        event.approval.request_id, decision
                    )
                elif isinstance(event, TurnCompletedEvent):
                    return self._result(event, "".join(text_parts), changed_paths)
        finally:
            close_iterator = getattr(iterator, "aclose", None)
            if close_iterator is not None:
                with suppress(BaseException):
                    await close_iterator()

    @staticmethod
    def _cancelled_result(turn_id: str) -> RunResult:
        return RunResult(
            run_id=turn_id,
            task_id=turn_id,
            status=RunStatus.CANCELLED,
            summary="",
            terminal_reason="user_cancelled",
        )

    @staticmethod
    def _result(event: TurnCompletedEvent, summary: str, changed_paths: set[str]) -> RunResult:
        if event.status == RuntimeTurnStatus.COMPLETED:
            status = RunStatus.COMPLETED
            reason = "conversation_turn_completed"
        elif event.status == RuntimeTurnStatus.INTERRUPTED:
            status = RunStatus.CANCELLED
            reason = "user_cancelled"
        else:
            status = RunStatus.FAILED
            reason = "conversation_turn_failed"
        return RunResult(
            run_id=event.turn_id,
            task_id=event.turn_id,
            status=status,
            summary=summary,
            changed_files=tuple(sorted(changed_paths)),
            terminal_reason=reason,
            error=event.error,
        )
