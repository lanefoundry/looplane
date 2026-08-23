"""Strict provider-neutral contracts for a live external-agent conversation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import Field, TypeAdapter, field_validator, model_validator

from rivumi.approvals import ApprovalDecision, ToolEffect
from rivumi.contracts import ContractModel
from rivumi.runtime_semantics import (
    ContextCheckpoint,
    ContextTelemetry,
    ProposedChange,
    RuntimeCapabilities,
)


class ConversationProtocolError(RuntimeError):
    """Raised when a runtime violates the bounded conversation protocol."""


class RuntimeApprovalKind(StrEnum):
    COMMAND = "command"
    FILE_CHANGE = "file_change"
    PERMISSIONS = "permissions"


class RuntimeToolStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    DECLINED = "declined"
    INTERRUPTED = "interrupted"


class RuntimeToolKind(StrEnum):
    READ = "read"
    SEARCH = "search"
    COMMAND = "command"
    FILE_CHANGE = "file_change"
    MCP = "mcp"
    AGENT = "agent"
    WEB = "web"


class RuntimeTurnStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class RuntimeApprovalRequest(ContractModel):
    """One Rivumi-correlated approval request; it contains no vendor identifiers."""

    request_id: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(min_length=1, max_length=128)
    action_id: str = Field(min_length=1, max_length=128)
    kind: RuntimeApprovalKind
    effect: ToolEffect
    preview: str = Field(default="", max_length=16_000)
    proposed_changes: tuple[ProposedChange, ...] = Field(default=(), max_length=1_000)
    grant_scope: str | None = Field(default=None, min_length=1, max_length=4_096)
    available_decisions: tuple[ApprovalDecision, ...]

    @field_validator("available_decisions")
    @classmethod
    def decisions_are_unique(
        cls, value: tuple[ApprovalDecision, ...]
    ) -> tuple[ApprovalDecision, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("available approval decisions must be non-empty and unique")
        return value

    @field_validator("grant_scope")
    @classmethod
    def grant_scope_is_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("grant_scope cannot be blank")
        return value

    @model_validator(mode="after")
    def proposed_changes_belong_to_action(self) -> RuntimeApprovalRequest:
        if any(change.action_id != self.action_id for change in self.proposed_changes):
            raise ValueError("proposed changes must belong to the approval action")
        return self


class _RuntimeEvent(ContractModel):
    sequence: int = Field(ge=0)
    turn_id: str = Field(min_length=1, max_length=128)


class TurnStartedEvent(_RuntimeEvent):
    event_type: Literal["turn_started"] = "turn_started"


class TextDeltaEvent(_RuntimeEvent):
    event_type: Literal["text_delta"] = "text_delta"
    text: str = Field(min_length=1, max_length=64_000)


class NoticeEvent(_RuntimeEvent):
    event_type: Literal["notice"] = "notice"
    level: Literal["info", "warning"]
    text: str = Field(min_length=1, max_length=16_000)


class ContextUsageUpdatedEvent(_RuntimeEvent):
    event_type: Literal["context_usage_updated"] = "context_usage_updated"
    telemetry: ContextTelemetry


class RuntimeModelUpdatedEvent(_RuntimeEvent):
    event_type: Literal["runtime_model_updated"] = "runtime_model_updated"
    model: str = Field(min_length=1, max_length=256)


class CompactionStartedEvent(_RuntimeEvent):
    event_type: Literal["compaction_started"] = "compaction_started"
    guidance: str | None = Field(default=None, min_length=1, max_length=4_000)

    @field_validator("guidance")
    @classmethod
    def guidance_is_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("compaction guidance cannot be blank")
        return value


class CompactionCompletedEvent(_RuntimeEvent):
    event_type: Literal["compaction_completed"] = "compaction_completed"
    checkpoint: ContextCheckpoint | None = None


class ToolStartedEvent(_RuntimeEvent):
    event_type: Literal["tool_started"] = "tool_started"
    action_id: str = Field(min_length=1, max_length=128)
    kind: RuntimeToolKind
    tool_name: str = Field(min_length=1, max_length=256)
    effect: ToolEffect
    summary: str = Field(default="", max_length=16_000)
    path: str | None = Field(default=None, max_length=4_096)
    paths: tuple[str, ...] = Field(default=(), max_length=1_000)

    @field_validator("paths")
    @classmethod
    def paths_are_unique_and_bounded(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not path or len(path) > 4_096 for path in value):
            raise ValueError("tool paths must be non-empty, unique, and bounded")
        return value


class ToolOutputDeltaEvent(_RuntimeEvent):
    event_type: Literal["tool_output_delta"] = "tool_output_delta"
    action_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=64_000)


class ActionPreviewUpdatedEvent(_RuntimeEvent):
    event_type: Literal["action_preview_updated"] = "action_preview_updated"
    action_id: str = Field(min_length=1, max_length=128)
    proposed_changes: tuple[ProposedChange, ...] = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def proposed_changes_belong_to_action(self) -> ActionPreviewUpdatedEvent:
        if any(change.action_id != self.action_id for change in self.proposed_changes):
            raise ValueError("proposed changes must belong to the preview action")
        return self


class ToolCompletedEvent(_RuntimeEvent):
    event_type: Literal["tool_completed"] = "tool_completed"
    action_id: str = Field(min_length=1, max_length=128)
    status: RuntimeToolStatus
    summary: str = Field(default="", max_length=16_000)
    output: str | None = Field(default=None, max_length=64_000)
    diff: str | None = Field(default=None, max_length=64_000)


class ApprovalRequestedEvent(_RuntimeEvent):
    event_type: Literal["approval_requested"] = "approval_requested"
    approval: RuntimeApprovalRequest

    @model_validator(mode="after")
    def approval_belongs_to_turn(self) -> ApprovalRequestedEvent:
        if self.approval.turn_id != self.turn_id:
            raise ValueError("approval turn_id must match event turn_id")
        return self


class ApprovalResolvedEvent(_RuntimeEvent):
    event_type: Literal["approval_resolved"] = "approval_resolved"
    request_id: str = Field(min_length=1, max_length=128)
    decision: ApprovalDecision


class TurnCompletedEvent(_RuntimeEvent):
    event_type: Literal["turn_completed"] = "turn_completed"
    status: RuntimeTurnStatus
    error: str | None = Field(default=None, max_length=16_000)

    @model_validator(mode="after")
    def error_matches_status(self) -> TurnCompletedEvent:
        if self.status == RuntimeTurnStatus.COMPLETED and self.error is not None:
            raise ValueError("completed turns cannot contain an error")
        if self.status == RuntimeTurnStatus.FAILED and not self.error:
            raise ValueError("failed turns require an error")
        return self


ConversationRuntimeEvent = Annotated[
    TurnStartedEvent
    | NoticeEvent
    | TextDeltaEvent
    | ContextUsageUpdatedEvent
    | RuntimeModelUpdatedEvent
    | CompactionStartedEvent
    | CompactionCompletedEvent
    | ToolStartedEvent
    | ToolOutputDeltaEvent
    | ActionPreviewUpdatedEvent
    | ToolCompletedEvent
    | ApprovalRequestedEvent
    | ApprovalResolvedEvent
    | TurnCompletedEvent,
    Field(discriminator="event_type"),
]

CONVERSATION_RUNTIME_EVENT_ADAPTER = TypeAdapter(ConversationRuntimeEvent)


@runtime_checkable
class ConversationRuntimeSession(Protocol):
    """One live, multi-turn external runtime session."""

    async def start(self) -> None: ...

    @property
    def capabilities(self) -> RuntimeCapabilities: ...

    async def send_turn(self, text: str) -> str: ...

    async def compact_context(self, guidance: str | None = None) -> str: ...

    def events(self) -> AsyncIterator[ConversationRuntimeEvent]: ...

    async def respond_approval(self, request_id: str, decision: ApprovalDecision) -> None: ...

    async def interrupt(self, turn_id: str) -> None: ...

    async def aclose(self) -> None: ...
