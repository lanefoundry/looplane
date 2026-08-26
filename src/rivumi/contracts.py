"""Provider-neutral contracts shared by the coding-agent harness."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator


class ContractModel(BaseModel):
    """Strict immutable base class for values crossing component boundaries."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class VerificationCommand(ContractModel):
    """An exact argv allowlist entry; it is never interpreted by a shell."""

    name: str = Field(min_length=1)
    argv: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: float = Field(default=300.0, gt=0)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("verification command name cannot be blank")
        return value

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not part or "\x00" in part for part in value):
            raise ValueError("verification argv entries must be non-empty and NUL-free")
        return value


class Limits(ContractModel):
    """Deterministic limits enforced by the harness rather than the prompt."""

    max_steps: int = Field(default=12, ge=1)
    wall_time_seconds: float = Field(default=900.0, gt=0)
    max_tool_output_bytes: int = Field(default=200_000, ge=1)
    max_patch_bytes: int = Field(default=100_000, ge=1)
    max_total_tokens: int | None = Field(default=None, ge=1)


class TaskContract(ContractModel):
    """Immutable request accepted by a coding-agent run."""

    repository: Path
    instruction: str = Field(min_length=1)
    allowed_paths: tuple[str, ...] = Field(min_length=1)
    verification: tuple[VerificationCommand, ...] = Field(min_length=1)
    limits: Limits = Field(default_factory=Limits)
    task_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1)
    base_sha: str | None = None

    @field_validator("instruction", "task_id")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value cannot be blank")
        return value

    @field_validator("base_sha")
    @classmethod
    def normalize_base_sha(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("base_sha cannot be blank")
        return value

    @field_validator("allowed_paths")
    @classmethod
    def validate_allowed_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for pattern in value:
            pattern = pattern.strip().replace("\\", "/")
            path = PurePosixPath(pattern)
            if not pattern or path.is_absolute() or ".." in path.parts or "\x00" in pattern:
                raise ValueError(f"allowed path must be a safe relative pattern: {pattern!r}")
            normalized.append(pattern)
        if len(set(normalized)) != len(normalized):
            raise ValueError("allowed_paths cannot contain duplicates")
        return tuple(normalized)


class ToolDefinition(ContractModel):
    """Canonical tool schema; adapters translate it to provider-specific payloads."""

    name: str = Field(min_length=1)
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class ToolCall(ContractModel):
    """A provider-neutral request to invoke one named tool."""

    tool_call_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


class Message(ContractModel):
    """A canonical system, user, or assistant conversation message."""

    role: Literal["system", "user", "assistant"]
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()

    @model_validator(mode="after")
    def validate_content(self) -> Message:
        if self.role != "assistant" and self.tool_calls:
            raise ValueError("only assistant messages can contain tool calls")
        if self.role != "assistant" and self.content is None:
            raise ValueError(f"{self.role} messages require content")
        if self.role == "assistant" and self.content is None and not self.tool_calls:
            raise ValueError("assistant message requires content or tool calls")
        return self


class ToolObservation(ContractModel):
    """Canonical result returned to the model after a tool call."""

    tool_call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    ok: bool
    content: str = ""
    error: str | None = None

    @model_validator(mode="after")
    def validate_error(self) -> ToolObservation:
        if self.ok and self.error is not None:
            raise ValueError("successful tool observations cannot contain an error")
        if not self.ok and not self.error:
            raise ValueError("failed tool observations require an error")
        return self


ConversationItem = Message | ToolObservation


class Usage(ContractModel):
    """Normalized usage with inclusive input/output counts.

    Cached input is a subset of ``input_tokens`` and reasoning is a subset of
    ``output_tokens``. A provider-reported total is retained when available.
    """

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    provider_total_tokens: int | None = Field(default=None, ge=0)

    @computed_field
    @property
    def total_tokens(self) -> int:
        if self.provider_total_tokens is not None:
            return self.provider_total_tokens
        return self.input_tokens + self.output_tokens


class ModelCapabilities(ContractModel):
    """Capabilities advertised by an adapter for its configured model/API."""

    tool_calling: bool
    streaming: bool
    structured_output: bool


class ModelProtocol(StrEnum):
    """Wire protocol selected independently from provider identity and endpoint."""

    SCRIPTED = "scripted"
    OPENAI_CHAT = "openai_chat"
    OPENAI_RESPONSES = "openai_responses"
    OPENAI_CODEX_RESPONSES = "openai_codex_responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"
    GEMINI_GENERATE_CONTENT = "gemini_generate_content"
    WORKERS_AI_RUN = "workers_ai_run"


class ModelTurn(ContractModel):
    """Canonical output from one non-streaming model invocation."""

    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str | None = None

    @model_validator(mode="after")
    def validate_output(self) -> ModelTurn:
        if self.content is None and not self.tool_calls:
            raise ValueError("model turn requires content or tool calls")
        return self

    def as_message(self) -> Message:
        return Message(role="assistant", content=self.content, tool_calls=self.tool_calls)


class RunStatus(StrEnum):
    CREATED = "created"
    PREPARING = "preparing"
    INSPECTING = "inspecting"
    PLANNING = "planning"
    IMPLEMENTING = "implementing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class VerificationOutcome(ContractModel):
    name: str
    argv: tuple[str, ...]
    ok: bool
    exit_code: int | None = None
    duration_seconds: float = Field(default=0.0, ge=0)
    output: str = ""


class Checkpoint(ContractModel):
    """Minimal resumable state persisted after state-changing steps."""

    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    status: RunStatus = RunStatus.CREATED
    step: int = Field(default=0, ge=0)
    messages: tuple[ConversationItem, ...] = ()
    tool_call_count: int = Field(default=0, ge=0)
    usage: Usage = Field(default_factory=Usage)
    active_writer_token: str | None = None
    last_action_fingerprint: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RunResult(ContractModel):
    """Terminal, reviewable result for one run."""

    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    status: Literal[RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED]
    summary: str = ""
    changed_files: tuple[str, ...] = ()
    verification: tuple[VerificationOutcome, ...] = ()
    usage: Usage = Field(default_factory=Usage)
    terminal_reason: str = Field(min_length=1)
    error: str | None = Field(default=None, max_length=16_000)
    artifacts: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def error_matches_status(self) -> RunResult:
        if self.error is not None and (not self.error.strip() or "\x00" in self.error):
            raise ValueError("run error must be non-blank and contain no NUL")
        if self.status != RunStatus.FAILED and self.error is not None:
            raise ValueError("only failed runs can contain an error")
        return self
