"""Stable programmatic facade for embedding looplane.

This module is intentionally thin: it exposes typed contracts plus small
orchestration helpers without importing the CLI/TUI or provider SDK adapters.
"""

from __future__ import annotations

from pathlib import Path

from looplane.cache_strategy import (
    CacheAwarePromptOrdering,
    CacheAwarePromptOrderingMode,
    ProviderCacheMapping,
    ProviderCacheTrace,
    apply_provider_cache_defaults,
    cache_aware_prompt_ordering,
    provider_cache_mapping,
    provider_cache_trace,
)
from looplane.context_providers import (
    ContextProviderCommand,
    ContextProviderConfig,
    ContextProviderRunner,
    load_project_context_provider_config,
    load_project_context_provider_runner,
)
from looplane.context_watch import (
    ProjectContextWatchBackend,
    ProjectContextWatchBackendCapability,
    ProjectContextWatchChange,
    ProjectContextWatchSnapshot,
    project_context_watch_capabilities,
    project_context_watch_snapshot,
    render_project_context_reload,
    watch_project_context_changes,
)
from looplane.contracts import (
    InjectedContext,
    RunResult,
    TaskContract,
    ToolDefinition,
    Usage,
    VerificationCommand,
)
from looplane.conversation_controller import BackendTurnLimiter, TurnLimiter
from looplane.conversation_runtime import (
    ConversationRuntimeEvent,
    ConversationRuntimeSession,
    RuntimeAttachment,
    RuntimeInjectedContext,
    RuntimeSkillsChangedEvent,
)
from looplane.conversation_websocket import ConversationWebSocketApp
from looplane.events import EventSink
from looplane.events import RunEvent as RunEvent
from looplane.hooks import HookCommandConfig, HookConfig, HookDecision, HookEventName, HookRunner
from looplane.ide import (
    EditorDeepLinkStyle,
    IdeDiagnostic,
    IdeDiagnosticSeverity,
    IdeDiagnosticsSnapshot,
    IdeOpenFile,
    IdeOpenFilesSnapshot,
    IdePosition,
    IdeRange,
    build_editor_deep_link,
    load_project_ide_diagnostics,
    load_project_open_files,
    render_ide_diagnostics_context,
    render_ide_open_files_context,
)
from looplane.instructions import (
    InstructionDocument,
    InstructionResolution,
    InstructionSourceDiagnostic,
    load_instruction_documents,
    render_instruction_context,
    render_instruction_diagnostics,
    resolve_instruction_documents,
)
from looplane.loop import AgentRunner
from looplane.lsp import LspServerCommand, LspSupervisorError, ManagedLspServer
from looplane.models import ModelProvider
from looplane.plugins import (
    PluginDiscoveryMetadata,
    PluginSkillRef,
    ProjectPlugin,
    load_project_plugins,
)
from looplane.prompts import (
    A10_SUBAGENT_PLANNER_POLICY_VERSION,
    INTERACTION_CONTEXT_VERSION,
    PromptSection,
    render_interaction_prompt_context,
    render_prompt_sections,
    render_runtime_prompt_context,
    render_subagent_planner_policy,
    render_tool_prompt_context,
    render_workspace_prompt_context,
)
from looplane.provider_catalog import ModelRole, estimate_cost, role_candidates
from looplane.session_replay import (
    ReplayForkSeed,
    ReplayState,
    create_forked_run_from_event,
    reduce_jsonl,
)
from looplane.skills import (
    ProjectSkill,
    load_project_skills,
    render_skill_context,
    select_project_skills,
)
from looplane.subagents import (
    ScheduledSubagent,
    SubagentRole,
    SubagentScheduleTraceAnalysis,
    analyze_subagent_schedule_events,
    analyze_subagent_schedule_jsonl,
    derive_subagent_task,
    normalize_subagent_schedule,
    run_subagent_task,
)

SDK_STABILITY = "0.x: contracts are typed and versioned, but may change before 1.0."


async def run_task(
    task: TaskContract,
    model: ModelProvider,
    run_root: str | Path,
    *,
    event_sink: EventSink | None = None,
    sandbox_checks: bool = True,
) -> RunResult:
    """Run one bounded looplane task and return its persisted result."""

    return await AgentRunner(
        task,
        model,
        run_root,
        sandbox_checks=sandbox_checks,
        event_sink=event_sink,
    ).run()


def replay_run_events(events_jsonl: str | Path) -> ReplayState:
    """Reduce a run event log into deterministic replay state."""

    return reduce_jsonl(events_jsonl)


def fork_run_at_event(
    *,
    source_run_dir: str | Path,
    run_root: str | Path,
    sequence: int,
    new_run_id: str | None = None,
) -> ReplayForkSeed:
    """Create a side-effect-free fork workspace from a recorded event sequence."""

    return create_forked_run_from_event(
        source_run_dir=Path(source_run_dir),
        run_root=Path(run_root),
        sequence=sequence,
        new_run_id=new_run_id,
    )


__all__ = [
    "ConversationRuntimeEvent",
    "ConversationRuntimeSession",
    "ConversationWebSocketApp",
    "A10_SUBAGENT_PLANNER_POLICY_VERSION",
    "BackendTurnLimiter",
    "TurnLimiter",
    "CacheAwarePromptOrdering",
    "CacheAwarePromptOrderingMode",
    "ContextProviderCommand",
    "ContextProviderConfig",
    "ContextProviderRunner",
    "EditorDeepLinkStyle",
    "EventSink",
    "HookCommandConfig",
    "HookConfig",
    "HookDecision",
    "HookEventName",
    "HookRunner",
    "INTERACTION_CONTEXT_VERSION",
    "InjectedContext",
    "InstructionDocument",
    "InstructionResolution",
    "InstructionSourceDiagnostic",
    "IdeDiagnostic",
    "IdeDiagnosticSeverity",
    "IdeDiagnosticsSnapshot",
    "IdeOpenFile",
    "IdeOpenFilesSnapshot",
    "IdePosition",
    "IdeRange",
    "LspServerCommand",
    "LspSupervisorError",
    "ManagedLspServer",
    "ModelProvider",
    "ModelRole",
    "PluginSkillRef",
    "PluginDiscoveryMetadata",
    "PromptSection",
    "ProviderCacheTrace",
    "ProviderCacheMapping",
    "ProjectContextWatchBackend",
    "ProjectContextWatchBackendCapability",
    "ProjectContextWatchChange",
    "ProjectContextWatchSnapshot",
    "ProjectPlugin",
    "ProjectSkill",
    "ReplayForkSeed",
    "ReplayState",
    "RuntimeAttachment",
    "RuntimeInjectedContext",
    "RuntimeSkillsChangedEvent",
    "RunResult",
    "SDK_STABILITY",
    "ScheduledSubagent",
    "SubagentRole",
    "SubagentScheduleTraceAnalysis",
    "TaskContract",
    "ToolDefinition",
    "Usage",
    "VerificationCommand",
    "apply_provider_cache_defaults",
    "analyze_subagent_schedule_events",
    "analyze_subagent_schedule_jsonl",
    "build_editor_deep_link",
    "cache_aware_prompt_ordering",
    "derive_subagent_task",
    "estimate_cost",
    "fork_run_at_event",
    "load_project_ide_diagnostics",
    "load_project_context_provider_config",
    "load_project_context_provider_runner",
    "load_project_open_files",
    "load_project_plugins",
    "load_project_skills",
    "load_instruction_documents",
    "normalize_subagent_schedule",
    "provider_cache_trace",
    "provider_cache_mapping",
    "project_context_watch_capabilities",
    "project_context_watch_snapshot",
    "replay_run_events",
    "render_ide_diagnostics_context",
    "render_ide_open_files_context",
    "render_interaction_prompt_context",
    "render_instruction_diagnostics",
    "render_instruction_context",
    "render_project_context_reload",
    "render_runtime_prompt_context",
    "render_skill_context",
    "render_prompt_sections",
    "render_subagent_planner_policy",
    "render_tool_prompt_context",
    "render_workspace_prompt_context",
    "resolve_instruction_documents",
    "role_candidates",
    "run_subagent_task",
    "run_task",
    "select_project_skills",
    "watch_project_context_changes",
]
