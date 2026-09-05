"""Compatibility facade for the canonical terminal application and features."""

from __future__ import annotations

import asyncio as asyncio
import json as json
import shlex as shlex
from collections import deque as deque
from collections.abc import Awaitable as Awaitable
from collections.abc import Callable as Callable
from collections.abc import Iterable as Iterable
from collections.abc import Mapping as Mapping
from contextlib import suppress as suppress
from pathlib import Path as Path
from time import monotonic as monotonic
from typing import TYPE_CHECKING as TYPE_CHECKING
from typing import Any
from uuid import uuid4 as uuid4

from rich.text import Text as Text
from textual import on as on
from textual import work as work
from textual.app import App as App
from textual.app import ComposeResult as ComposeResult
from textual.binding import Binding as Binding
from textual.containers import Horizontal as Horizontal
from textual.containers import Vertical as Vertical
from textual.events import Resize as Resize
from textual.theme import Theme as Theme
from textual.widgets import Button as Button
from textual.widgets import Collapsible as Collapsible
from textual.widgets import OptionList as OptionList
from textual.widgets import RichLog as RichLog
from textual.widgets import Select as Select
from textual.widgets import Static as Static
from textual.widgets import TextArea as TextArea
from textual.widgets.option_list import Option as Option

from looplane.approvals import ApprovalDecision as ApprovalDecision
from looplane.approvals import ApprovalRequest as ApprovalRequest
from looplane.backends import ExternalAgentEvent as ExternalAgentEvent
from looplane.cli_config import CliConfig as CliConfig
from looplane.cli_config import save_cli_config as save_cli_config
from looplane.console import LiveEventProjection as LiveEventProjection
from looplane.contracts import RunResult as RunResult
from looplane.contracts import RunStatus as RunStatus
from looplane.contracts import Usage as Usage
from looplane.conversation import ConversationEventKind as ConversationEventKind
from looplane.conversation import ConversationStore as ConversationStore
from looplane.conversation import ConversationWriterLease as ConversationWriterLease
from looplane.conversation_runtime import ActionPreviewUpdatedEvent as ActionPreviewUpdatedEvent
from looplane.conversation_runtime import ApprovalRequestedEvent as ApprovalRequestedEvent
from looplane.conversation_runtime import ApprovalResolvedEvent as ApprovalResolvedEvent
from looplane.conversation_runtime import CompactionCompletedEvent as CompactionCompletedEvent
from looplane.conversation_runtime import CompactionStartedEvent as CompactionStartedEvent
from looplane.conversation_runtime import ContextUsageUpdatedEvent as ContextUsageUpdatedEvent
from looplane.conversation_runtime import ConversationRuntimeEvent as ConversationRuntimeEvent
from looplane.conversation_runtime import NoticeEvent as NoticeEvent
from looplane.conversation_runtime import RuntimeModelUpdatedEvent as RuntimeModelUpdatedEvent
from looplane.conversation_runtime import RuntimeTurnStatus as RuntimeTurnStatus
from looplane.conversation_runtime import TextDeltaEvent as TextDeltaEvent
from looplane.conversation_runtime import ToolOutputDeltaEvent as ToolOutputDeltaEvent
from looplane.conversation_runtime import TurnStartedEvent as TurnStartedEvent
from looplane.events import RunEvent as RunEvent
from looplane.memory import remember as remember
from looplane.prompts import (
    WORKSPACE_CONTEXT_REMINDER_VERSION as WORKSPACE_CONTEXT_REMINDER_VERSION,
)
from looplane.prompts import build_workspace_context_reminder as build_workspace_context_reminder
from looplane.provider_catalog import estimate_cost as estimate_cost
from looplane.runtime_semantics import ContextTelemetry as ContextTelemetry
from looplane.runtime_semantics import PermissionMode, ProcessLocalGrant
from looplane.runtime_semantics import RuntimeCapabilities as RuntimeCapabilities
from looplane.runtime_semantics import input_cache_hit_rate as input_cache_hit_rate
from looplane.runtime_semantics import should_auto_compact_context as should_auto_compact_context
from looplane.slash_commands import DEFAULT_SLASH_COMMAND_REGISTRY as DEFAULT_SLASH_COMMAND_REGISTRY
from looplane.slash_commands import InvalidSlashCommand as InvalidSlashCommand
from looplane.slash_commands import SlashCommand as SlashCommand
from looplane.slash_commands import UnknownSlashCommand as UnknownSlashCommand
from looplane.terminal.app import _AUTOMATIC_MODEL as _AUTOMATIC_MODEL
from looplane.terminal.app import _IDLE_CONFIRM_WINDOW_S as _IDLE_CONFIRM_WINDOW_S
from looplane.terminal.app import _INTERRUPT_ESCALATION_S as _INTERRUPT_ESCALATION_S
from looplane.terminal.app import LOOPLANE_THEME as LOOPLANE_THEME
from looplane.terminal.app import RecordingConversationEventSink as RecordingConversationEventSink
from looplane.terminal.app import TerminalDependencies
from looplane.terminal.app import _looplane_version as _looplane_version
from looplane.terminal.app import _rewindable_prompts_from_events as _rewindable_prompts_from_events
from looplane.terminal.app import looplaneApp as _CanonicalApp
from looplane.terminal.approvals import ApprovalModal as ApprovalModal
from looplane.terminal.approvals import ApprovalPreview as ApprovalPreview
from looplane.terminal.approvals import InlineApprovalBlock as InlineApprovalBlock
from looplane.terminal.approvals import InlineApprovalChoices as InlineApprovalChoices
from looplane.terminal.approvals import TextualApprovalPolicy as _TerminalApprovalPolicy
from looplane.terminal.clipboard import copy_with_native_command as copy_with_native_command
from looplane.terminal.clipboard import selected_text_for_copy as selected_text_for_copy
from looplane.terminal.composer import MessageComposer as MessageComposer
from looplane.terminal.conversation_binding import TextualEventSink as _TerminalEventSink
from looplane.terminal.events import (
    ConversationRuntimeEventMessage as ConversationRuntimeEventMessage,
)
from looplane.terminal.events import ExternalRunEventMessage as ExternalRunEventMessage
from looplane.terminal.events import RunEventMessage as RunEventMessage
from looplane.terminal.onboarding import OnboardingModal as OnboardingModal
from looplane.terminal.scroll import TranscriptScroll as TranscriptScroll
from looplane.terminal.selectors import InlineSelectorBlock as InlineSelectorBlock
from looplane.terminal.selectors import InlineSelectorChoices as InlineSelectorChoices
from looplane.terminal.status import _add_usage as _add_usage
from looplane.terminal.status import _usage_bar as _usage_bar
from looplane.terminal.status import format_token_count as format_token_count
from looplane.terminal.status_widgets import RuntimeLoadingIndicator as RuntimeLoadingIndicator
from looplane.terminal.status_widgets import RuntimeMetrics as _TerminalRuntimeMetrics
from looplane.terminal.status_widgets import RuntimeStatus as RuntimeStatus
from looplane.terminal.tool_widgets import ToolActionBlock as ToolActionBlock
from looplane.terminal.tool_widgets import ToolGroupBlock as ToolGroupBlock
from looplane.terminal.transcript import MessageBlock as MessageBlock
from looplane.terminal.transcript import TimelineEntry as TimelineEntry
from looplane.terminal.types import CommandMenuChoice as CommandMenuChoice
from looplane.terminal.types import InlineSelectorOption as InlineSelectorOption
from looplane.terminal.types import InteractionState as InteractionState
from looplane.terminal.types import LoadingPhase as LoadingPhase
from looplane.terminal.types import ProviderOption as ProviderOption
from looplane.terminal.types import RunnerFactory as RunnerFactory
from looplane.terminal.types import RuntimeModelOption as RuntimeModelOption
from looplane.terminal.types import RuntimeOption as RuntimeOption
from looplane.terminal.types import TuiConfigurationSelection as TuiConfigurationSelection
from looplane.terminal.types import TuiResource as TuiResource
from looplane.terminal.types import TuiRunner as TuiRunner
from looplane.terminal.types import TuiRunRequest as TuiRunRequest
from looplane.transcript import infer_tool_detail_kind as infer_tool_detail_kind
from looplane.transcript_export import TranscriptReducer as TranscriptReducer


class TextualApprovalPolicy(_TerminalApprovalPolicy):
    """Adapt the legacy App constructor to the canonical approval callbacks."""

    def __init__(self, app: Any, session_grants: set[ProcessLocalGrant]) -> None:
        self.app = app
        super().__init__(
            app.request_approval,
            session_grants,
            permission_mode=lambda: PermissionMode(
                getattr(app, "_permission_mode", PermissionMode.ASK)
            ),
        )


class RuntimeMetrics(_TerminalRuntimeMetrics):
    """Keep the original formatter monkeypatch seam at the compatibility boundary."""

    def __init__(self, *, id: str, token_formatter: Callable[[int], str] | None = None) -> None:
        super().__init__(
            id=id, token_formatter=token_formatter or (lambda count: format_token_count(count))
        )


class TextualEventSink(_TerminalEventSink):
    """Adapt the legacy App argument without canonical imports back to this facade."""

    def __init__(self, app: _CanonicalApp, generation: int) -> None:
        super().__init__(app.conversation_binding, generation)


class looplaneApp(_CanonicalApp):
    """Legacy construction with late-bound clipboard, formatter and clock hooks."""

    def __init__(
        self,
        *,
        repository: Path,
        config: CliConfig,
        runner_factory: RunnerFactory,
        providers: Iterable[ProviderOption],
        runtimes: Iterable[RuntimeOption] = (("looplane-agent", "looplane"),),
        runtime_models: Mapping[str, tuple[RuntimeModelOption, ...]] | None = None,
        ollama_models: tuple[str, ...] = (),
        initial_prompt: str | None = None,
        locked_provider: str | None = None,
        conversation_store: ConversationStore | None = None,
        runner_warmup: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(
            repository=repository,
            config=config,
            runner_factory=runner_factory,
            providers=providers,
            runtimes=runtimes,
            runtime_models=runtime_models,
            ollama_models=ollama_models,
            initial_prompt=initial_prompt,
            locked_provider=locked_provider,
            conversation_store=conversation_store,
            runner_warmup=runner_warmup,
            dependencies=TerminalDependencies(
                copy_native=lambda text: copy_with_native_command(text),
                selected_text=lambda focused, screen: selected_text_for_copy(focused, screen),
                format_tokens=lambda count: format_token_count(count),
                clock=lambda: monotonic(),
                version=lambda: _looplane_version(),
                save_config=lambda config: save_cli_config(config),
                metrics_type=RuntimeMetrics,
            ),
        )
