"""Narrow callback and execution contracts shared by native scheduling leaves."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from looplane.approvals import ApprovalDecision, ApprovalPolicy, ApprovalReason, ToolEffect
from looplane.contracts import (
    RunResult,
    TaskContract,
    ToolCall,
    ToolObservation,
    VerificationCommand,
    VerificationOutcome,
)
from looplane.events import EventSink
from looplane.hooks import HookDecision, HookEventName
from looplane.models import ModelProvider


class EventEmitter(Protocol):
    async def __call__(self, event_type: str, **data: Any) -> None: ...


class HookCall(Protocol):
    async def __call__(
        self, event: HookEventName, payload: dict[str, Any]
    ) -> HookDecision | None: ...


class ApprovalCall(Protocol):
    async def __call__(
        self,
        *,
        action_id: str,
        effect: ToolEffect,
        reason: ApprovalReason,
        preview: str,
        tool_call: ToolCall,
    ) -> tuple[ApprovalDecision, str | None]: ...


class RemainingTime(Protocol):
    def __call__(self, deadline: float) -> float: ...


class MarkActionStarted(Protocol):
    async def __call__(self, request_id: str) -> None: ...


class DispatchSubagents(Protocol):
    async def __call__(self, call: ToolCall, *, deadline: float) -> ToolObservation: ...


class ToolExecutionPort(Protocol):
    @property
    def verification_commands(self) -> Mapping[str, VerificationCommand]: ...

    @property
    def verification_outcomes(self) -> Mapping[str, VerificationOutcome]: ...

    def execute(self, call: ToolCall, *, timeout_seconds: float) -> ToolObservation: ...


@dataclass(frozen=True)
class PreparedToolCall:
    call: ToolCall
    decision: ApprovalDecision
    effect: ToolEffect
    request_id: str | None


class PrepareToolCall(Protocol):
    async def __call__(self, call: ToolCall) -> PreparedToolCall: ...


class ExecutePreparedCall(Protocol):
    async def __call__(self, prepared: PreparedToolCall, *, deadline: float) -> ToolObservation: ...


class SubagentRunner(Protocol):
    async def run(self) -> RunResult: ...


class SubagentRunnerFactory(Protocol):
    def __call__(
        self,
        task: TaskContract,
        model: ModelProvider,
        run_root: Path,
        *,
        run_id: str,
        sandbox_checks: bool,
        allow_unsafe_local_exec: bool,
        approval_policy: ApprovalPolicy | None,
        event_sink: EventSink | None,
        enable_subagent_dispatch: bool,
    ) -> SubagentRunner: ...
