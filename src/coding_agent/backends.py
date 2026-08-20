"""Contracts for delegating a whole task to an external agent runtime.

External agent backends own their model loop and are deliberately separate from
``ModelProvider``.  They are not a way to forward provider credentials.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import Field, field_validator

from coding_agent.contracts import ContractModel


class ExternalRunStatus(StrEnum):
    """Terminal status of one delegated external-agent run."""

    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class ExternalAgentTask(ContractModel):
    """A bounded task request passed to an external agent as standard input."""

    instruction: str = Field(min_length=1)
    task_id: str = Field(min_length=1)

    @field_validator("instruction", "task_id")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value cannot be blank")
        if "\x00" in value:
            raise ValueError("value cannot contain NUL bytes")
        return value


class ExternalAgentEvent(ContractModel):
    """Provider-neutral event normalized from an external agent's event stream."""

    sequence: int = Field(ge=0)
    event_type: str = Field(min_length=1)
    text: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ExternalAgentResult(ContractModel):
    """Bounded terminal result from an external agent backend."""

    backend_name: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    status: ExternalRunStatus
    summary: str = ""
    events: tuple[ExternalAgentEvent, ...] = ()
    terminal_reason: str = Field(min_length=1)
    exit_code: int | None = None


@runtime_checkable
class ExternalEventSink(Protocol):
    """Optional consumer for normalized external-agent events."""

    async def emit(self, event: ExternalAgentEvent) -> None: ...


@runtime_checkable
class ExternalAgentBackend(Protocol):
    """A full delegated agent runtime, not a model-completion adapter."""

    backend_name: str
    local_only: bool
    experimental: bool

    async def run(
        self,
        task: ExternalAgentTask,
        *,
        working_directory: Path | None = None,
        event_sink: ExternalEventSink | None = None,
    ) -> ExternalAgentResult: ...
