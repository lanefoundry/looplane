"""Provider-neutral semantic contracts for interactive coding runtimes.

These values describe runtime meaning, not how a provider reports it or how a
terminal renders it.  Keeping that boundary explicit lets adapters and UIs
evolve independently without weakening permission or lifecycle semantics.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, field_validator, model_validator

from rivumi.approvals import ToolEffect
from rivumi.contracts import ContractModel


class ContextTelemetryAccuracy(StrEnum):
    """Whether token counts were reported or conservatively estimated."""

    EXACT = "exact"
    ESTIMATED = "estimated"


class ContextTelemetry(ContractModel):
    """A coherent snapshot of one runtime context's token occupancy."""

    accuracy: ContextTelemetryAccuracy
    input_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(ge=0)
    context_window: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def counts_are_coherent(self) -> ContextTelemetry:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens plus output_tokens")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached_input_tokens cannot exceed input_tokens")
        if self.reasoning_output_tokens > self.output_tokens:
            raise ValueError("reasoning_output_tokens cannot exceed output_tokens")
        if self.context_window is not None and self.total_tokens > self.context_window:
            raise ValueError("total_tokens cannot exceed context_window")
        return self


class RuntimeCapabilities(ContractModel):
    """Explicit semantic features supported by one provider adapter."""

    token_usage: bool = False
    native_compaction: bool = False
    proposed_file_preview: bool = False
    structured_approvals: bool = False
    queued_submissions: bool = False
    steer_active_turn: bool = False
    background_task_management: bool = False


def should_auto_compact_context(
    telemetry: ContextTelemetry | None,
    capabilities: RuntimeCapabilities,
    *,
    trigger_ratio: float = 0.85,
) -> bool:
    """Return whether a native conversation should compact after a completed turn."""

    if trigger_ratio <= 0 or trigger_ratio > 1:
        raise ValueError("trigger_ratio must be within (0, 1]")
    if telemetry is None or not capabilities.native_compaction:
        return False
    if telemetry.context_window is None:
        return False
    return telemetry.total_tokens / telemetry.context_window >= trigger_ratio


def should_remind_context_pressure(
    *,
    total_tokens: int,
    max_total_tokens: int | None,
    trigger_ratio: float = 0.85,
) -> bool:
    """Return whether a native-loop task should receive a one-shot pressure reminder."""

    if trigger_ratio <= 0 or trigger_ratio > 1:
        raise ValueError("trigger_ratio must be within (0, 1]")
    if total_tokens < 0:
        raise ValueError("total_tokens cannot be negative")
    if max_total_tokens is None:
        return False
    if max_total_tokens < 1:
        raise ValueError("max_total_tokens must be positive")
    return total_tokens / max_total_tokens >= trigger_ratio


def should_apply_history_summary_fallback(
    *,
    total_tokens: int,
    max_total_tokens: int | None,
    message_count: int,
    already_applied: bool,
    trigger_ratio: float = 0.85,
    protected_head_items: int = 2,
    retained_tail_items: int = 4,
    min_summarized_items: int = 2,
) -> bool:
    """Return whether the native loop should replace older history with a summary."""

    if already_applied:
        return False
    if protected_head_items < 0:
        raise ValueError("protected_head_items cannot be negative")
    if retained_tail_items < 0:
        raise ValueError("retained_tail_items cannot be negative")
    if min_summarized_items < 1:
        raise ValueError("min_summarized_items must be positive")
    if message_count < 0:
        raise ValueError("message_count cannot be negative")
    if not should_remind_context_pressure(
        total_tokens=total_tokens,
        max_total_tokens=max_total_tokens,
        trigger_ratio=trigger_ratio,
    ):
        return False
    return message_count - protected_head_items - retained_tail_items >= min_summarized_items


def history_summary_fallback_span(
    *,
    message_count: int,
    protected_head_items: int = 2,
    retained_tail_items: int = 4,
    min_summarized_items: int = 2,
) -> tuple[int, int] | None:
    """Return the half-open message span eligible for deterministic summarization."""

    if protected_head_items < 0:
        raise ValueError("protected_head_items cannot be negative")
    if retained_tail_items < 0:
        raise ValueError("retained_tail_items cannot be negative")
    if min_summarized_items < 1:
        raise ValueError("min_summarized_items must be positive")
    if message_count < 0:
        raise ValueError("message_count cannot be negative")

    start = min(protected_head_items, message_count)
    end = max(start, message_count - retained_tail_items)
    if end - start < min_summarized_items:
        return None
    return start, end


class ContextSummary(ContractModel):
    """A bounded model-produced summary replacing one or more complete turns."""

    summary_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=64_000)
    source_turn_ids: tuple[str, ...] = Field(min_length=1, max_length=10_000)
    guidance: str | None = Field(default=None, min_length=1, max_length=4_000)

    @field_validator("summary_id", "text", "guidance")
    @classmethod
    def text_is_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("summary text fields cannot be blank")
        return value

    @field_validator("source_turn_ids")
    @classmethod
    def source_turns_are_bounded_and_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("source_turn_ids cannot contain duplicates")
        if any(not turn_id.strip() or len(turn_id) > 128 for turn_id in value):
            raise ValueError("source turn ids must be non-blank and at most 128 characters")
        return tuple(turn_id.strip() for turn_id in value)


class ContextCheckpoint(ContractModel):
    """Durable semantic result of compaction plus the recent turns it retained."""

    checkpoint_id: str = Field(min_length=1, max_length=128)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    summary: ContextSummary
    retained_turn_ids: tuple[str, ...] = Field(default=(), max_length=10_000)
    telemetry_before: ContextTelemetry
    telemetry_after: ContextTelemetry

    @field_validator("checkpoint_id")
    @classmethod
    def checkpoint_id_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("checkpoint_id cannot be blank")
        return value

    @field_validator("created_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @field_validator("retained_turn_ids")
    @classmethod
    def retained_turns_are_bounded_and_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(turn_id.strip() for turn_id in value)
        if len(normalized) != len(set(normalized)):
            raise ValueError("retained_turn_ids cannot contain duplicates")
        if any(not turn_id or len(turn_id) > 128 for turn_id in normalized):
            raise ValueError("retained turn ids must be non-blank and at most 128 characters")
        return normalized

    @model_validator(mode="after")
    def checkpoint_is_coherent(self) -> ContextCheckpoint:
        if set(self.summary.source_turn_ids) & set(self.retained_turn_ids):
            raise ValueError("summarized and retained turns must be disjoint")
        if self.telemetry_after.total_tokens > self.telemetry_before.total_tokens:
            raise ValueError("compaction cannot increase total context occupancy")
        return self


class PermissionMode(StrEnum):
    """Runtime-owned permission policy; provider prompt text cannot override it."""

    ASK = "ask"
    ACCEPT_EDITS = "accept-edits"
    READ_ONLY = "read-only"


class PermissionDecision(StrEnum):
    """Closed result used by an approval adapter or a headless runtime."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ProcessLocalGrant(ContractModel):
    """One non-persistent permission grant correlated to an exact action scope."""

    effect: ToolEffect
    scope: str = Field(min_length=1, max_length=4_096)

    @field_validator("scope")
    @classmethod
    def scope_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("grant scope cannot be blank")
        return value

    @model_validator(mode="after")
    def grant_is_for_a_side_effect(self) -> ProcessLocalGrant:
        if self.effect == ToolEffect.READ:
            raise ValueError("read access is intrinsic and must not be stored as a grant")
        return self


def decide_permission(
    mode: PermissionMode,
    effect: ToolEffect,
    *,
    scope: str,
    grants: Collection[ProcessLocalGrant] = (),
) -> PermissionDecision:
    """Return a deterministic decision without mutating process-local grants.

    Read-only mode is a hard ceiling, so stale grants can never re-enable a
    side effect after the user switches modes.  In other modes, grants match
    both the effect and exact scope.
    """

    mode = PermissionMode(mode)
    effect = ToolEffect(effect)
    scope = scope.strip()
    if not scope:
        raise ValueError("permission scope cannot be blank")
    if len(scope) > 4_096:
        raise ValueError("permission scope cannot exceed 4096 characters")

    if effect == ToolEffect.READ:
        return PermissionDecision.ALLOW
    if mode == PermissionMode.READ_ONLY:
        return PermissionDecision.DENY
    if any(grant.effect == effect and grant.scope == scope for grant in grants):
        return PermissionDecision.ALLOW
    if mode == PermissionMode.ACCEPT_EDITS and effect == ToolEffect.MODIFY:
        return PermissionDecision.ALLOW
    return PermissionDecision.ASK


class ProposedChangeKind(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    MOVE = "move"


class ProposedChange(ContractModel):
    """Bounded pre-approval metadata computed from trusted workspace state."""

    change_id: str = Field(min_length=1, max_length=128)
    action_id: str = Field(min_length=1, max_length=128)
    kind: ProposedChangeKind
    paths: tuple[str, ...] = Field(min_length=1, max_length=1_000)
    summary: str = Field(default="", max_length=16_000)
    unified_diff: str | None = Field(default=None, max_length=64_000)
    original_diff_bytes: int | None = Field(default=None, ge=0)
    truncated: bool = False

    @field_validator("change_id", "action_id")
    @classmethod
    def identifiers_are_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("change identifiers cannot be blank")
        return value

    @field_validator("paths")
    @classmethod
    def paths_are_bounded_and_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(path.strip() for path in value)
        if len(normalized) != len(set(normalized)):
            raise ValueError("proposed change paths cannot contain duplicates")
        if any(not path or len(path) > 4_096 or "\x00" in path for path in normalized):
            raise ValueError("proposed change paths must be non-blank, NUL-free, and bounded")
        return normalized

    @model_validator(mode="after")
    def preview_metadata_is_coherent(self) -> ProposedChange:
        if self.kind == ProposedChangeKind.MOVE and len(self.paths) != 2:
            raise ValueError("move previews require exactly a source and destination path")
        if self.unified_diff is None:
            if self.original_diff_bytes is not None or self.truncated:
                raise ValueError("missing diff cannot have byte or truncation metadata")
            return self

        shown_bytes = len(self.unified_diff.encode("utf-8"))
        if self.original_diff_bytes is None:
            raise ValueError("diff previews require original_diff_bytes")
        if self.original_diff_bytes < shown_bytes:
            raise ValueError("original_diff_bytes cannot be smaller than the shown diff")
        if self.truncated != (self.original_diff_bytes > shown_bytes):
            raise ValueError("truncated must exactly describe whether diff bytes were omitted")
        return self


class TaskLane(StrEnum):
    FOREGROUND = "foreground"
    QUEUED = "queued"
    BACKGROUND = "background"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class _TaskRecord(ContractModel):
    task_id: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=16_000)

    @field_validator("task_id", "turn_id", "summary")
    @classmethod
    def task_text_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("task record text cannot be blank")
        return value


class QueuedTaskState(_TaskRecord):
    """A follow-up waiting behind the one active runtime turn."""

    lane: Literal[TaskLane.QUEUED] = TaskLane.QUEUED
    status: Literal[TaskStatus.QUEUED] = TaskStatus.QUEUED
    queue_position: int = Field(ge=1)
    enqueued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("enqueued_at")
    @classmethod
    def enqueue_timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("enqueued_at must be timezone-aware")
        return value


class _ActiveTaskState(_TaskRecord):
    status: Literal[
        TaskStatus.RUNNING,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.INTERRUPTED,
    ]
    started_at: datetime
    completed_at: datetime | None = None
    result_summary: str | None = Field(default=None, min_length=1, max_length=16_000)
    error: str | None = Field(default=None, min_length=1, max_length=16_000)

    @field_validator("started_at", "completed_at")
    @classmethod
    def task_timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("task timestamps must be timezone-aware")
        return value

    @field_validator("result_summary", "error")
    @classmethod
    def optional_task_text_is_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("task result fields cannot be blank")
        return value

    @model_validator(mode="after")
    def lifecycle_is_coherent(self) -> _ActiveTaskState:
        if self.status == TaskStatus.RUNNING:
            if (
                self.completed_at is not None
                or self.result_summary is not None
                or self.error is not None
            ):
                raise ValueError("running tasks cannot have terminal metadata")
            return self

        if self.completed_at is None:
            raise ValueError("terminal tasks require completed_at")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        if self.status == TaskStatus.FAILED:
            if self.error is None:
                raise ValueError("failed tasks require an error")
        elif self.error is not None:
            raise ValueError("only failed tasks may contain an error")
        return self


class ForegroundTaskState(_ActiveTaskState):
    """The single task currently owning the interactive runtime."""

    lane: Literal[TaskLane.FOREGROUND] = TaskLane.FOREGROUND


class BackgroundTaskState(_ActiveTaskState):
    """A task explicitly detached onto a concurrent execution facility."""

    lane: Literal[TaskLane.BACKGROUND] = TaskLane.BACKGROUND


TaskState = Annotated[
    ForegroundTaskState | QueuedTaskState | BackgroundTaskState,
    Field(discriminator="lane"),
]

TASK_STATE_ADAPTER = TypeAdapter(TaskState)
