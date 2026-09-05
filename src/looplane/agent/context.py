"""Context assembly returning explicit additions; never mutates engine messages."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

from looplane.agent.state import ContextState
from looplane.context_providers import ContextProviderRunner
from looplane.context_watch import (
    project_context_watch_snapshot,
    render_project_context_reload,
)
from looplane.contracts import (
    ConversationItem,
    InjectedContext,
    Message,
    TaskContract,
    ToolDefinition,
    ToolObservation,
    Usage,
    VerificationOutcome,
)
from looplane.execution.capture import bounded_text
from looplane.execution.environment import sanitized_subprocess_env
from looplane.execution.local_process import run_local_process
from looplane.ide import (
    IdeBridgeError,
    load_project_ide_diagnostics,
    load_project_open_files,
    render_ide_diagnostics_context,
    render_ide_open_files_context,
)
from looplane.instructions import (
    instruction_documents_fingerprint,
    render_instruction_context,
    render_instruction_diagnostics,
    resolve_instruction_documents,
)
from looplane.memory import relevant_memory_entries, render_known_context
from looplane.prompts import (
    build_coding_agent_system_prompt,
    build_context_pressure_reminder,
    build_history_summary_fallback_message,
    build_workspace_context_reminder,
    render_interaction_prompt_context,
    render_runtime_prompt_context,
    render_subagent_planner_policy,
    render_task_request,
    render_tool_prompt_context,
    render_workspace_prompt_context,
)
from looplane.runtime_semantics import (
    history_summary_fallback_span,
    should_apply_history_summary_fallback,
    should_inject_workspace_context_reminder,
    should_remind_context_pressure,
)
from looplane.skills import load_project_skills, render_skill_context, select_project_skills

BlockingResult = TypeVar("BlockingResult")


class BlockingCall(Protocol):
    def __call__(
        self, function: Callable[..., BlockingResult], /, *args: Any, **kwargs: Any
    ) -> Awaitable[BlockingResult]: ...


@dataclass(frozen=True)
class ContextEvent:
    event_type: str
    data: dict[str, Any]


@dataclass(frozen=True)
class ContextUpdate:
    additions: tuple[ConversationItem, ...] = ()
    events: tuple[ContextEvent, ...] = ()


@dataclass(frozen=True)
class HistoryCompaction:
    start: int
    end: int
    message_count: int
    total_tokens: int
    max_total_tokens: int | None

    def hook_payload(self) -> dict[str, Any]:
        return {
            "compaction": {
                "kind": "history_summary_fallback",
                "source_start_index": self.start,
                "source_end_index": self.end,
                "source_message_count": self.end - self.start,
                "message_count": self.message_count,
                "total_tokens": self.total_tokens,
                "max_total_tokens": self.max_total_tokens,
            }
        }


def plan_history_compaction(
    task: TaskContract, state: ContextState, messages: Sequence[ConversationItem], usage: Usage
) -> HistoryCompaction | None:
    if not should_apply_history_summary_fallback(
        total_tokens=usage.total_tokens,
        max_total_tokens=task.limits.max_total_tokens,
        message_count=len(messages),
        already_applied=state.history_summary_fallback_applied,
    ):
        return None
    span = history_summary_fallback_span(message_count=len(messages))
    if span is None:
        return None
    start, end = span
    while end < len(messages) and isinstance(messages[end], ToolObservation):
        end += 1
    if end - start < 2:
        return None
    return HistoryCompaction(
        start, end, len(messages), usage.total_tokens, task.limits.max_total_tokens
    )


def history_summary(
    task: TaskContract, messages: Sequence[ConversationItem], plan: HistoryCompaction
) -> InjectedContext:
    summary = build_history_summary_fallback_message(
        messages[plan.start : plan.end],
        source_start_index=plan.start,
        source_end_index=plan.end,
        max_chars=max(512, min(task.limits.max_tool_output_bytes, 12_000)),
    )
    return InjectedContext(source="history_summary_fallback", content=summary.content or "")


def needs_workspace_reminder(state: ContextState) -> bool:
    return should_inject_workspace_context_reminder(
        compacted_context=state.history_summary_fallback_applied,
        already_injected=state.workspace_context_reminder_sent,
    )


def initial_git_status(
    workspace: Path | None, run_dir: Path, allow_direct_repo_edit: bool
) -> tuple[str, ...]:
    if workspace is None:
        return ("unavailable: workspace executor is not initialized",)
    task_home = run_dir / ".task-env" if allow_direct_repo_edit else workspace.parent / ".task-env"
    result = run_local_process(
        ("git", "status", "--short", "--branch"),
        cwd=workspace,
        timeout_seconds=10.0,
        max_output_chars=4_000,
        env=sanitized_subprocess_env(task_home=task_home),
    )
    if not result.ok:
        reason = result.stderr.strip() or result.stdout.strip() or "git status failed"
        return (f"unavailable: {reason}",)
    lines = tuple(line for line in result.stdout.splitlines() if line.strip())
    if result.stdout_truncated:
        return (*lines, "... truncated")
    return lines


def initial_messages(
    task: TaskContract,
    state: ContextState,
    base_sha: str,
    *,
    provider_tools: tuple[ToolDefinition, ...],
    workspace: Path | None,
    run_dir: Path,
    allow_direct_repo_edit: bool,
    enable_subagent_dispatch: bool,
    sandbox_backend: str,
    sandbox_checks: bool,
    sandbox_profile: str,
) -> list[ConversationItem]:
    known_context = render_known_context(relevant_memory_entries(project=task.repository))
    instruction_resolution = resolve_instruction_documents(
        project_root=task.repository,
        start_dir=Path.cwd(),
    )
    instruction_documents = instruction_resolution.documents
    state.last_instruction_fingerprint = instruction_documents_fingerprint(instruction_documents)
    instruction_context = render_instruction_context(instruction_documents)
    instruction_diagnostics = render_instruction_diagnostics(instruction_resolution.diagnostics)
    if instruction_diagnostics:
        known_context = (
            f"{known_context}\n\n{instruction_diagnostics}"
            if known_context
            else instruction_diagnostics
        )
    skills = select_project_skills(
        load_project_skills(task.repository),
        task.enabled_skills,
    )
    skill_context = render_skill_context(skills)
    provider_tools = provider_tools if workspace is not None else ()
    tool_context_parts = [render_tool_prompt_context(provider_tools)]
    if enable_subagent_dispatch:
        tool_context_parts.append(render_subagent_planner_policy())
    tool_context = "\n\n".join(part for part in tool_context_parts if part)
    git_status = initial_git_status(workspace, run_dir, allow_direct_repo_edit)
    direct_edit_warning = (
        "direct_edit_warning: You are editing this repository's real working tree "
        "directly, not an isolated clone. The git_status_short lines above may "
        "include changes that already existed before this run — review diffs "
        "carefully before assuming everything shown is your own edit."
        if allow_direct_repo_edit and git_status
        else None
    )
    workspace_context = render_workspace_prompt_context(
        base_sha=base_sha,
        allowed_paths=task.allowed_paths,
        verification=task.verification,
        git_status=git_status,
        direct_edit_warning=direct_edit_warning,
    )
    interaction_context = render_interaction_prompt_context()
    runtime_context = render_runtime_prompt_context(
        {
            "mode": "native_loop",
            "sandbox_backend": sandbox_backend,
            "sandbox_checks": sandbox_checks,
            "sandbox_profile": sandbox_profile,
            "max_steps": task.limits.max_steps,
            "max_tool_output_bytes": task.limits.max_tool_output_bytes,
        }
    )
    state.last_project_context_watch = project_context_watch_snapshot(
        task.repository,
        start_dir=Path.cwd(),
    )
    request = render_task_request(
        instruction=task.instruction,
        base_sha=base_sha,
        allowed_paths=task.allowed_paths,
        verification=task.verification,
    )
    return [
        Message(
            role="system",
            content=build_coding_agent_system_prompt(
                known_context=known_context,
                instruction_context=instruction_context,
                skill_context=skill_context,
                tool_context=tool_context,
                interaction_context=interaction_context,
                workspace_context=workspace_context,
                runtime_context=runtime_context,
            ),
        ),
        Message(role="user", content=request),
    ]


def recent_important_paths(
    messages: Sequence[ConversationItem], *, max_items: int = 12
) -> tuple[str, ...]:
    seen: set[str] = set()
    paths: list[str] = []

    def add(value: object) -> None:
        if not isinstance(value, str):
            return
        normalized = value.strip().replace("\\", "/")
        if not normalized or "\x00" in normalized or normalized in {".", "/"}:
            return
        if normalized.startswith(("/", "../")) or "/../" in normalized:
            return
        if normalized not in seen:
            seen.add(normalized)
            paths.append(normalized)

    for item in reversed(messages):
        if not isinstance(item, Message):
            continue
        for call in reversed(item.tool_calls):
            add(call.arguments.get("path"))
            raw_paths = call.arguments.get("paths")
            if isinstance(raw_paths, Sequence) and not isinstance(raw_paths, str):
                for raw_path in raw_paths:
                    add(raw_path)
        if len(paths) >= max_items:
            break
    return tuple(paths[:max_items])


def check_status_lines(verification: tuple[VerificationOutcome, ...]) -> tuple[str, ...]:
    if not verification:
        return ()
    return tuple(
        f"{outcome.name}: {'passed' if outcome.ok else 'failed'}"
        + (f" (exit {outcome.exit_code})" if outcome.exit_code is not None else "")
        for outcome in verification
    )


def constraint_lines(task: TaskContract, step: int) -> tuple[str, ...]:
    verification = "; ".join(
        f"{command.name}={list(command.argv)!r}" for command in task.verification
    )
    token_limit = (
        f"max_total_tokens={task.limits.max_total_tokens}"
        if task.limits.max_total_tokens is not None
        else "max_total_tokens=unbounded"
    )
    remaining_steps = max(0, task.limits.max_steps - step)
    return (
        "allowed_paths=" + ", ".join(task.allowed_paths),
        "verification=" + verification,
        f"remaining_steps_before_next_request={remaining_steps}",
        token_limit,
    )


def context_pressure_reminder(
    task: TaskContract, state: ContextState, usage: Usage
) -> ContextUpdate:
    additions: list[ConversationItem] = []
    events: list[ContextEvent] = []
    max_total_tokens = task.limits.max_total_tokens
    if state.context_pressure_reminder_sent or max_total_tokens is None:
        return ContextUpdate(tuple(additions), tuple(events))
    if not should_remind_context_pressure(
        total_tokens=usage.total_tokens, max_total_tokens=max_total_tokens
    ):
        return ContextUpdate(tuple(additions), tuple(events))
    additions.append(
        InjectedContext(
            source="context_pressure",
            content=build_context_pressure_reminder(
                total_tokens=usage.total_tokens, max_total_tokens=max_total_tokens
            ),
        )
    )
    state.context_pressure_reminder_sent = True
    events.append(
        ContextEvent(
            "context_pressure.reminder_injected",
            {"total_tokens": usage.total_tokens, "max_total_tokens": max_total_tokens},
        )
    )
    return ContextUpdate(tuple(additions), tuple(events))


def workspace_context_reminder(
    task: TaskContract,
    state: ContextState,
    messages: Sequence[ConversationItem],
    verification: tuple[VerificationOutcome, ...],
    step: int,
    changed_files: tuple[str, ...],
) -> ContextUpdate:
    additions: list[ConversationItem] = []
    events: list[ContextEvent] = []
    if not should_inject_workspace_context_reminder(
        compacted_context=state.history_summary_fallback_applied,
        already_injected=state.workspace_context_reminder_sent,
    ):
        return ContextUpdate(tuple(additions), tuple(events))
    recent_paths = tuple(dict.fromkeys((*changed_files, *recent_important_paths(messages))).keys())
    additions.append(
        InjectedContext(
            source="workspace_context_reminder",
            content=build_workspace_context_reminder(
                changed_files=changed_files,
                check_status=check_status_lines(verification),
                recent_paths=recent_paths,
                constraints=constraint_lines(task, step),
                max_chars=max(512, min(task.limits.max_tool_output_bytes, 4000)),
            ).content
            or "",
        )
    )
    state.workspace_context_reminder_sent = True
    events.append(
        ContextEvent(
            "context_pressure.workspace_reminder_injected",
            {"changed_files": changed_files, "recent_paths": recent_paths},
        )
    )
    return ContextUpdate(tuple(additions), tuple(events))


async def runtime_context_providers(
    task: TaskContract,
    provider_runner: ContextProviderRunner,
    blocking_call: BlockingCall,
    *,
    run_id: str,
    sequence: int,
    step: int,
) -> ContextUpdate:
    additions: list[ConversationItem] = []
    events: list[ContextEvent] = []
    if not provider_runner.enabled:
        return ContextUpdate(tuple(additions), tuple(events))
    try:
        items = await blocking_call(
            provider_runner.collect,
            {
                "run_id": run_id,
                "task_id": task.task_id,
                "sequence": sequence,
                "step": step,
                "repository": str(task.repository),
            },
        )
    except Exception as exc:
        events.append(
            ContextEvent(
                "context_provider.failed",
                {"error": bounded_text(f"{type(exc).__name__}: {exc}", 2000)},
            )
        )
        return ContextUpdate(tuple(additions), tuple(events))
    for item in items:
        additions.append(
            InjectedContext(
                source=bounded_text(f"context_provider:{item.source}", 128).rstrip(),
                content=item.content,
            )
        )
    if items:
        events.append(
            ContextEvent(
                "context_provider.injected",
                {"sources": [item.source for item in items], "items": len(items)},
            )
        )
    return ContextUpdate(tuple(additions), tuple(events))


def ide_diagnostics(task: TaskContract, state: ContextState) -> ContextUpdate:
    additions: list[ConversationItem] = []
    events: list[ContextEvent] = []
    try:
        snapshot = load_project_ide_diagnostics(task.repository)
    except IdeBridgeError as exc:
        events.append(ContextEvent("ide.diagnostics_ignored", {"error": str(exc)}))
        return ContextUpdate(tuple(additions), tuple(events))
    if snapshot is None:
        return ContextUpdate(tuple(additions), tuple(events))
    fingerprint = snapshot.fingerprint
    if fingerprint == state.last_ide_diagnostics_fingerprint:
        return ContextUpdate(tuple(additions), tuple(events))
    content = render_ide_diagnostics_context(snapshot, project_root=task.repository)
    if not content:
        state.last_ide_diagnostics_fingerprint = fingerprint
        return ContextUpdate(tuple(additions), tuple(events))
    additions.append(InjectedContext(source="ide_diagnostics", content=content))
    state.last_ide_diagnostics_fingerprint = fingerprint
    paths = tuple(dict.fromkeys(diagnostic.path for diagnostic in snapshot.diagnostics))
    events.append(
        ContextEvent(
            "ide.diagnostics_injected",
            {"diagnostic_count": len(snapshot.diagnostics), "paths": paths[:20]},
        )
    )
    return ContextUpdate(tuple(additions), tuple(events))


def ide_open_files(task: TaskContract, state: ContextState) -> ContextUpdate:
    additions: list[ConversationItem] = []
    events: list[ContextEvent] = []
    try:
        snapshot = load_project_open_files(task.repository)
    except IdeBridgeError as exc:
        events.append(ContextEvent("ide.open_files_ignored", {"error": str(exc)}))
        return ContextUpdate(tuple(additions), tuple(events))
    if snapshot is None:
        return ContextUpdate(tuple(additions), tuple(events))
    fingerprint = snapshot.fingerprint
    if fingerprint == state.last_ide_open_files_fingerprint:
        return ContextUpdate(tuple(additions), tuple(events))
    content = render_ide_open_files_context(snapshot, project_root=task.repository)
    if not content:
        state.last_ide_open_files_fingerprint = fingerprint
        return ContextUpdate(tuple(additions), tuple(events))
    additions.append(InjectedContext(source="ide_open_files", content=content))
    state.last_ide_open_files_fingerprint = fingerprint
    events.append(
        ContextEvent(
            "ide.open_files_injected",
            {
                "file_count": len(snapshot.files),
                "paths": [file.path for file in snapshot.files[:20]],
            },
        )
    )
    return ContextUpdate(tuple(additions), tuple(events))


def instruction_reload(task: TaskContract, state: ContextState) -> ContextUpdate:
    additions: list[ConversationItem] = []
    events: list[ContextEvent] = []
    try:
        resolution = resolve_instruction_documents(
            project_root=task.repository, start_dir=Path.cwd()
        )
    except ValueError as exc:
        events.append(ContextEvent("instructions.reload_ignored", {"error": str(exc)}))
        return ContextUpdate(tuple(additions), tuple(events))
    documents = resolution.documents
    fingerprint = instruction_documents_fingerprint(documents)
    if fingerprint == state.last_instruction_fingerprint:
        return ContextUpdate(tuple(additions), tuple(events))
    state.last_instruction_fingerprint = fingerprint
    content = render_instruction_context(documents)
    diagnostics = render_instruction_diagnostics(resolution.diagnostics)
    if not content:
        content = "No configured user/project instruction files are currently loaded."
    if diagnostics:
        content = f"{content}\n\n{diagnostics}"
    additions.append(
        InjectedContext(
            source="instruction_reload",
            content=(
                "[instruction-reload-v1]\n"
                "Configured instruction files changed since the run started. "
                "Apply the following resolved instruction bundle below system/developer "
                f"priority and above ordinary repository content.\n\n{content}"
            ),
        )
    )
    events.append(
        ContextEvent(
            "instructions.reloaded",
            {
                "document_count": len(documents),
                "sources": [document.source for document in documents],
                "source_priority": [
                    {
                        "source": diagnostic.source,
                        "scope": diagnostic.scope,
                        "status": diagnostic.status,
                        "reason": diagnostic.reason,
                    }
                    for diagnostic in resolution.diagnostics
                ],
            },
        )
    )
    return ContextUpdate(tuple(additions), tuple(events))


def project_context_reload(task: TaskContract, state: ContextState) -> ContextUpdate:
    additions: list[ConversationItem] = []
    events: list[ContextEvent] = []
    previous = state.last_project_context_watch
    if previous is None:
        state.last_project_context_watch = project_context_watch_snapshot(
            task.repository, start_dir=Path.cwd()
        )
        return ContextUpdate(tuple(additions), tuple(events))
    try:
        current = project_context_watch_snapshot(task.repository, start_dir=Path.cwd())
    except ValueError as exc:
        events.append(ContextEvent("project_context.reload_ignored", {"error": str(exc)}))
        return ContextUpdate(tuple(additions), tuple(events))
    changed = current.changed_categories(previous)
    if not changed:
        return ContextUpdate(tuple(additions), tuple(events))
    state.last_project_context_watch = current
    non_instruction_changes = tuple(category for category in changed if category != "instructions")
    if not non_instruction_changes:
        return ContextUpdate(tuple(additions), tuple(events))
    content = render_project_context_reload(previous, current, categories=non_instruction_changes)
    if {"skills", "plugins"} & set(non_instruction_changes):
        skills = select_project_skills(load_project_skills(task.repository), task.enabled_skills)
        skill_context = render_skill_context(skills)
        if skill_context:
            content = f"{content}\n\nReloaded project skill context:\n{skill_context}"
    if content:
        additions.append(InjectedContext(source="project_context_reload", content=content))
    events.append(
        ContextEvent(
            "project_context.reloaded",
            {
                "categories": list(non_instruction_changes),
                "sources": {
                    category: list(current.sources.get(category, ()))
                    for category in non_instruction_changes
                },
            },
        )
    )
    return ContextUpdate(tuple(additions), tuple(events))
