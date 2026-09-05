"""Typed turn state and restoration of persisted native context markers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from looplane.context_watch import ProjectContextWatchSnapshot
from looplane.contracts import (
    ConversationItem,
    InjectedContext,
    ModelUsageRecord,
    Usage,
    VerificationOutcome,
)
from looplane.prompts import (
    CONTEXT_PRESSURE_REMINDER_VERSION,
    CONTEXT_SUMMARY_FALLBACK_VERSION,
    WORKSPACE_CONTEXT_REMINDER_VERSION,
)
from looplane.session import SessionManifest


@dataclass
class TurnState:
    """Mutable engine state; scheduling and verification policy remain in the engine."""

    messages: list[ConversationItem] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model_usage: list[ModelUsageRecord] = field(default_factory=list)
    step: int = 0
    last_fingerprint: str | None = None
    repeat_count: int = 0
    made_changes: bool = False
    last_verification: tuple[VerificationOutcome, ...] = ()
    verified_workspace_fingerprint: str | None = None

    def restore(self, manifest: SessionManifest) -> None:
        """Restore the original resume fields, preserving the fresh repetition budget."""

        self.messages = list(manifest.messages)
        self.usage = manifest.usage
        self.model_usage = list(manifest.model_usage)
        self.step = manifest.step
        self.last_fingerprint = manifest.last_action_fingerprint
        self.last_verification = manifest.verification
        self.verified_workspace_fingerprint = manifest.verified_workspace_fingerprint


@dataclass
class ContextState:
    """Deduplication markers owned by context assembly, separate from conversation history."""

    context_pressure_reminder_sent: bool = False
    history_summary_fallback_applied: bool = False
    workspace_context_reminder_sent: bool = False
    last_ide_diagnostics_fingerprint: str | None = None
    last_ide_open_files_fingerprint: str | None = None
    last_instruction_fingerprint: str | None = None
    last_project_context_watch: ProjectContextWatchSnapshot | None = None

    def restore(self, messages: list[ConversationItem]) -> None:
        def contains(source: str, version: str) -> bool:
            return any(
                isinstance(item, InjectedContext)
                and item.source == source
                and item.content is not None
                and version in item.content
                for item in messages
            )

        self.context_pressure_reminder_sent = contains(
            "context_pressure", CONTEXT_PRESSURE_REMINDER_VERSION
        )
        self.history_summary_fallback_applied = contains(
            "history_summary_fallback", CONTEXT_SUMMARY_FALLBACK_VERSION
        )
        self.workspace_context_reminder_sent = contains(
            "workspace_context_reminder", WORKSPACE_CONTEXT_REMINDER_VERSION
        )


@dataclass
class ActiveRunClock:
    """Charged task time, excluding approval waits."""

    active_wall_time_base: float = 0.0
    run_started_monotonic: float | None = None
    active_started_at: datetime | None = None
