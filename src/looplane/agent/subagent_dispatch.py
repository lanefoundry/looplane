"""Programmatic subagent dispatch on isolated looplane run workspaces."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Any
from uuid import uuid4

from looplane.agent.agent_definitions import AgentDefinition, AgentRegistry, load_agents
from looplane.agent.ports import (
    EventEmitter,
    ExecutePreparedCall,
    PrepareToolCall,
    RemainingTime,
    SubagentRunnerFactory,
)
from looplane.agent.state import TurnState
from looplane.approvals import ApprovalDecision, ApprovalPolicy, HeadlessApprovalPolicy
from looplane.contracts import (
    ConversationItem,
    Limits,
    Message,
    RunResult,
    RunStatus,
    TaskContract,
    ToolCall,
    ToolDefinition,
    ToolObservation,
    VerificationCommand,
)
from looplane.events import EventSink
from looplane.execution.capture import bounded_text
from looplane.models import ModelProvider
from looplane.tooling.types import ToolExecutionError

FORK_CONTEXT_MARKER = "<fork-context>agent-fork-child</fork-context>"
MAX_SUBAGENT_DEPTH = 2


class SubagentRole(StrEnum):
    """Deprecated: use AgentDefinition via agent_definitions.load_agents() instead."""

    SCOUT = "scout"
    REVIEWER = "reviewer"
    GENERAL = "general"


_LEGACY_ROLE_INSTRUCTIONS: dict[str, str] = {
    "scout": (
        "Role: scout. Inspect the requested surface and report concrete files, facts, and risks. "
        "Do not propose broad rewrites."
    ),
    "reviewer": (
        "Role: reviewer. Review prior findings for correctness, missed risks, and verification "
        "gaps. Prefer concise findings over repetition."
    ),
    "general": (
        "You are a general-purpose agent. Execute the task directly using whatever tools are "
        "needed. Report your findings or results concisely when done."
    ),
}

SUBAGENT_ROLE_INSTRUCTIONS: dict[SubagentRole, str] = {
    SubagentRole(k): v for k, v in _LEGACY_ROLE_INSTRUCTIONS.items()
}

_DEFAULT_REGISTRY: AgentRegistry | None = None


def _get_registry() -> AgentRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = load_agents()
    return _DEFAULT_REGISTRY


def reset_registry() -> None:
    """Force re-load on next access (useful for tests)."""
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = None


def is_in_fork(messages: Sequence[ConversationItem]) -> bool:
    """Detect whether the current conversation is already inside a fork child."""
    for item in messages:
        if isinstance(item, Message) and item.content and FORK_CONTEXT_MARKER in item.content:
            return True
    return False


def build_forked_messages(
    parent_messages: Sequence[ConversationItem],
    directive: str,
) -> tuple[ConversationItem, ...]:
    """Build a child's message list from the parent's conversation for fork mode.

    Copies the parent's full conversation history and appends a user message
    with the fork directive and anti-recursion marker.
    """
    fork_instruction = (
        f"{FORK_CONTEXT_MARKER}\n\n"
        "You are a forked child agent. Execute the task below directly — "
        "do not re-delegate or spawn further forks.\n\n"
        f"Task: {directive}"
    )
    return (
        *parent_messages,
        Message(role="user", content=fork_instruction),
    )


def resolve_agent_tools(
    definition: AgentDefinition,
    parent_tools: tuple[ToolDefinition, ...],
) -> tuple[ToolDefinition, ...]:
    """Filter parent's tools by the agent definition's allowlist.

    - ``tools=None``: read-only defaults (tools marked ``read_only=True``)
    - ``tools=["*"]``: all parent tools
    - ``tools=["read_file", "grep"]``: only those named tools
    """
    if definition.tools is None:
        return tuple(t for t in parent_tools if t.read_only)
    if definition.tools == ["*"]:
        return parent_tools
    allowed = set(definition.tools)
    return tuple(t for t in parent_tools if t.name in allowed)


def can_spawn_at_depth(definition: AgentDefinition, current_depth: int) -> bool:
    """Check whether an agent is allowed to spawn children at the given depth."""
    if definition.spawns is None:
        return False
    return current_depth < MAX_SUBAGENT_DEPTH


def yield_result_definition() -> ToolDefinition:
    """Tool definition for child agents to report structured intermediate results."""
    return ToolDefinition(
        name="yield_result",
        description=(
            "Report a structured intermediate result back to the parent agent. "
            "Use this to send findings, summaries, or status updates before completion."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Brief summary of the result.",
                },
                "data": {
                    "type": "object",
                    "description": "Structured data payload.",
                },
                "status": {
                    "type": "string",
                    "enum": ["in_progress", "blocked", "complete"],
                    "default": "in_progress",
                },
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
        read_only=True,
    )


def resolve_agent_type(name: str) -> AgentDefinition:
    """Resolve an agent type name to its definition.

    Falls back to legacy role instructions for backward compatibility.
    """
    registry = _get_registry()
    definition = registry.get(name)
    if definition is not None:
        return definition
    if name in _LEGACY_ROLE_INSTRUCTIONS:
        return AgentDefinition(
            name=name,
            description=f"Legacy {name} role",
            system_prompt=_LEGACY_ROLE_INSTRUCTIONS[name],
            source="legacy",
        )
    raise ValueError(f"unknown agent type: {name}")


@dataclass(frozen=True)
class ScheduledSubagent:
    """Host-normalized subagent dispatch entry."""

    id: str
    agent_type: str
    instruction: str
    allowed_paths: object | None
    max_steps: int
    depends_on: tuple[str, ...]
    proposed_transaction: object | None
    wave: int
    mode: str = "fresh"

    @property
    def role(self) -> SubagentRole:
        """Backward-compatible access — raises ValueError for non-legacy types."""
        return SubagentRole(self.agent_type)


@dataclass(frozen=True)
class SubagentScheduleTraceAnalysis:
    """Aggregate evidence from emitted subagent schedule traces."""

    trace_count: int
    agent_count: int
    max_wave_count: int
    role_counts: dict[str, int]
    transaction_agent_count: int
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "trace_count": self.trace_count,
            "agent_count": self.agent_count,
            "max_wave_count": self.max_wave_count,
            "role_counts": dict(sorted(self.role_counts.items())),
            "transaction_agent_count": self.transaction_agent_count,
            "warnings": list(self.warnings),
        }


def subagent_role_instruction(role: SubagentRole | str) -> str:
    """Return the system-style instruction for a named agent type.

    Looks up the agent definition from the registry first, falling back to the
    legacy ``SUBAGENT_ROLE_INSTRUCTIONS`` dict for backward compatibility.
    """
    name = role.value if isinstance(role, SubagentRole) else role
    definition = resolve_agent_type(name)
    return definition.system_prompt


def normalize_subagent_schedule(
    agents: object,
    *,
    max_agents: int = 8,
    max_steps: int = 30,
) -> tuple[ScheduledSubagent, ...]:
    """Validate and wave-schedule a model-requested subagent graph."""

    if not isinstance(agents, Sequence) or isinstance(agents, (str, bytes)):
        raise ValueError("agents must be an array")
    if not agents:
        raise ValueError("agents must not be empty")
    if len(agents) > max_agents:
        raise ValueError(f"dispatch_subagents supports at most {max_agents} agents")

    specs: dict[str, ScheduledSubagent] = {}
    for raw_agent in agents:
        if not isinstance(raw_agent, Mapping):
            raise ValueError("each subagent must be an object")
        agent_id = raw_agent.get("id")
        if not isinstance(agent_id, str):
            raise ValueError("subagent id must be a non-empty string")
        _validate_subagent_id(agent_id)
        if agent_id in specs:
            raise ValueError(f"duplicate subagent id: {agent_id}")
        agent_type_raw = raw_agent.get("agent_type") or raw_agent.get("role") or "general"
        if not isinstance(agent_type_raw, str) or not agent_type_raw:
            raise ValueError("subagent must have an agent_type (or role) string")
        try:
            resolve_agent_type(agent_type_raw)
        except ValueError as exc:
            raise ValueError(f"unknown agent type: {agent_type_raw}") from exc
        instruction = raw_agent.get("instruction")
        if not isinstance(instruction, str):
            raise ValueError("subagent instruction must be a string")
        instruction = instruction.strip()
        if not instruction or "\x00" in instruction:
            raise ValueError("subagent instruction must be non-blank and NUL-free")
        depends_on = raw_agent.get("depends_on", ())
        if not isinstance(depends_on, Sequence) or isinstance(depends_on, (str, bytes)):
            raise ValueError("subagent depends_on must be an array")
        dependencies = []
        for dependency in depends_on:
            if not isinstance(dependency, str) or not dependency:
                raise ValueError("subagent depends_on entries must be non-empty strings")
            dependencies.append(dependency)
        definition = resolve_agent_type(agent_type_raw)
        default_steps = definition.max_steps
        requested_steps = raw_agent.get("max_steps", default_steps)
        if not isinstance(requested_steps, int) or requested_steps < 1:
            requested_steps = default_steps
        if requested_steps > max_steps:
            requested_steps = max_steps
        mode_raw = raw_agent.get("mode", "fresh")
        if mode_raw not in ("fresh", "fork"):
            mode_raw = "fresh"
        specs[agent_id] = ScheduledSubagent(
            id=agent_id,
            agent_type=agent_type_raw,
            instruction=instruction,
            allowed_paths=raw_agent.get("allowed_paths"),
            max_steps=requested_steps,
            depends_on=tuple(dict.fromkeys(dependencies)),
            proposed_transaction=raw_agent.get("proposed_transaction"),
            wave=-1,
            mode=mode_raw,
        )

    for agent_id, spec in specs.items():
        for dependency in spec.depends_on:
            if dependency not in specs:
                raise ValueError(f"subagent {agent_id} depends on unknown id: {dependency}")

    scheduled: list[ScheduledSubagent] = []
    completed: set[str] = set()
    pending = dict(specs)
    wave = 0
    while pending:
        ready_ids = [
            agent_id
            for agent_id, spec in pending.items()
            if all(dependency in completed for dependency in spec.depends_on)
        ]
        if not ready_ids:
            raise ValueError("subagent dependency cycle detected")
        for agent_id in ready_ids:
            spec = pending.pop(agent_id)
            scheduled.append(
                ScheduledSubagent(
                    id=spec.id,
                    agent_type=spec.agent_type,
                    instruction=spec.instruction,
                    allowed_paths=spec.allowed_paths,
                    max_steps=spec.max_steps,
                    depends_on=spec.depends_on,
                    proposed_transaction=spec.proposed_transaction,
                    wave=wave,
                    mode=spec.mode,
                )
            )
            completed.add(agent_id)
        wave += 1
    return tuple(scheduled)


def analyze_subagent_schedule_events(
    events: Sequence[Mapping[str, Any]],
) -> SubagentScheduleTraceAnalysis:
    """Aggregate ``subagents.schedule_normalized`` events for planner tuning."""

    trace_count = 0
    agent_count = 0
    max_wave_count = 0
    role_counts: dict[str, int] = {}
    transaction_agent_count = 0
    warnings: list[str] = []
    for event in events:
        if event.get("event_type") != "subagents.schedule_normalized":
            continue
        data = event.get("data")
        if not isinstance(data, Mapping):
            warnings.append("schedule trace has invalid data")
            continue
        trace_count += 1
        waves = data.get("waves")
        if isinstance(waves, int):
            max_wave_count = max(max_wave_count, waves)
        agents = data.get("agents")
        if not isinstance(agents, Sequence) or isinstance(agents, (str, bytes)):
            warnings.append("schedule trace agents payload is invalid")
            continue
        agent_count += len(agents)
        for agent in agents:
            if not isinstance(agent, Mapping):
                warnings.append("schedule trace agent entry is invalid")
                continue
            role = agent.get("role")
            if isinstance(role, str):
                role_counts[role] = role_counts.get(role, 0) + 1
            else:
                warnings.append("schedule trace agent role is invalid")
            if agent.get("proposed_transaction") is True:
                transaction_agent_count += 1
    if trace_count == 0:
        warnings.append("no subagents.schedule_normalized traces found")
    elif agent_count and "reviewer" not in role_counts:
        warnings.append("no reviewer role observed in schedule traces")
    return SubagentScheduleTraceAnalysis(
        trace_count=trace_count,
        agent_count=agent_count,
        max_wave_count=max_wave_count,
        role_counts=role_counts,
        transaction_agent_count=transaction_agent_count,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def analyze_subagent_schedule_jsonl(path: str | Path) -> SubagentScheduleTraceAnalysis:
    """Load one event JSONL file and analyze normalized subagent schedule traces."""

    events: list[Mapping[str, Any]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on line {line_number}") from exc
            if not isinstance(event, Mapping):
                raise ValueError(f"event on line {line_number} must be an object")
            events.append(event)
    return analyze_subagent_schedule_events(events)


def derive_subagent_task(
    parent: TaskContract,
    *,
    instruction: str,
    subagent_id: str | None = None,
    allowed_paths: tuple[str, ...] | None = None,
    verification: tuple[VerificationCommand, ...] | None = None,
    limits: Limits | None = None,
) -> TaskContract:
    """Create a child task that keeps the parent's repository/base safety boundary."""

    child_id = subagent_id or uuid4().hex
    _validate_subagent_id(child_id)
    child_instruction = instruction.strip()
    if not child_instruction or "\x00" in child_instruction:
        raise ValueError("subagent instruction must be non-blank and NUL-free")
    child_allowed_paths = allowed_paths or parent.allowed_paths
    _validate_child_allowed_paths(parent.allowed_paths, child_allowed_paths)
    return parent.model_copy(
        update={
            "task_id": f"{parent.task_id}:subagent:{child_id}",
            "instruction": child_instruction,
            "allowed_paths": child_allowed_paths,
            "verification": verification or parent.verification,
            "limits": limits or parent.limits,
        }
    )


def _validate_subagent_id(child_id: str) -> None:
    path = Path(child_id)
    windows = PureWindowsPath(child_id)
    if (
        not child_id
        or "\x00" in child_id
        or child_id in {".", ".."}
        or path.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or path.name != child_id
    ):
        raise ValueError("subagent_id must be a safe identifier")


def _validate_child_allowed_paths(
    parent_allowed_paths: tuple[str, ...],
    child_allowed_paths: tuple[str, ...],
) -> None:
    for child in child_allowed_paths:
        normalized = child.strip().replace("\\", "/")
        if not any(_path_pattern_is_within(normalized, parent) for parent in parent_allowed_paths):
            raise ValueError("subagent allowed_paths cannot exceed parent allowed_paths")


def _path_pattern_is_within(child: str, parent: str) -> bool:
    parent = parent.strip().replace("\\", "/")
    if child == parent:
        return True
    if parent.endswith("/**"):
        prefix = parent[:-3]
        return child == prefix or child.startswith(f"{prefix}/")
    return False


async def run_subagent_task(
    parent: TaskContract,
    model: ModelProvider,
    run_root: str | Path,
    *,
    runner_factory: SubagentRunnerFactory,
    instruction: str,
    subagent_id: str | None = None,
    allowed_paths: tuple[str, ...] | None = None,
    verification: tuple[VerificationCommand, ...] | None = None,
    limits: Limits | None = None,
    event_sink: EventSink | None = None,
    approval_policy: ApprovalPolicy | None = None,
    sandbox_checks: bool = True,
    allow_unsafe_local_exec: bool = False,
    initial_messages: tuple[ConversationItem, ...] | None = None,
    enable_subagent_dispatch: bool = False,
) -> RunResult:
    """Run one child agent in a separate looplane run directory and workspace."""

    task = derive_subagent_task(
        parent,
        instruction=instruction,
        subagent_id=subagent_id,
        allowed_paths=allowed_paths,
        verification=verification,
        limits=limits,
    )
    safe_id = (subagent_id or task.task_id.rsplit(":", 1)[-1]).replace(":", "_")
    return await runner_factory(
        task,
        model,
        Path(run_root) / "subagents",
        run_id=safe_id,
        sandbox_checks=sandbox_checks,
        allow_unsafe_local_exec=allow_unsafe_local_exec,
        approval_policy=approval_policy,
        event_sink=event_sink,
        enable_subagent_dispatch=enable_subagent_dispatch,
        initial_messages=initial_messages,
    ).run()


def dispatch_subagents_definition(
    *,
    project_root: Path | None = None,
) -> ToolDefinition:
    registry = _get_registry()
    agent_names = registry.names()

    return ToolDefinition(
        name="dispatch_subagents",
        description=(
            "Dispatch one or more subagents in isolated child workspaces. "
            "Use this for parallel investigation, staged handoff, or a child-reviewed "
            "transaction proposal. Each agent needs an instruction, optional agent_type "
            f"(one of: {', '.join(agent_names)}; defaults to general), optional "
            "allowed_paths, optional depends_on ids, and optional proposed_transaction "
            "steps. Agent capabilities depend on their type definition."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "agents": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "minLength": 1, "maxLength": 64},
                            "agent_type": {
                                "type": "string",
                                "minLength": 1,
                                "description": (
                                    "Agent type name from a loaded definition. "
                                    f"Available: {', '.join(agent_names)}"
                                ),
                            },
                            "role": {
                                "type": "string",
                                "description": "Deprecated alias for agent_type.",
                            },
                            "instruction": {"type": "string", "minLength": 1},
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1, "maxLength": 64},
                                "default": [],
                            },
                            "allowed_paths": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                            },
                            "max_steps": {"type": "integer", "minimum": 1, "maximum": 30},
                            "proposed_transaction": {
                                "type": "object",
                                "properties": {
                                    "steps": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": 8,
                                        "items": {"type": "object"},
                                    }
                                },
                                "required": ["steps"],
                                "additionalProperties": False,
                            },
                        },
                        "required": ["id", "instruction"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["agents"],
            "additionalProperties": False,
        },
        read_only=True,
    )


def agent_tool_definition(
    *,
    project_root: Path | None = None,
) -> ToolDefinition:
    """Per-call agent tool — replaces batch dispatch_subagents."""
    registry = _get_registry()
    agent_names = registry.names()
    agent_descriptions = []
    for name in agent_names:
        defn = registry.get(name)
        if defn:
            agent_descriptions.append(f"- {name}: {defn.description}")

    return ToolDefinition(
        name="agent",
        description=(
            "Spawn one subagent in an isolated workspace. Call multiple times in "
            "one turn to run agents in parallel. Available agent types:\n"
            + "\n".join(agent_descriptions)
        ),
        input_schema={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["fresh", "fork"],
                    "default": "fresh",
                    "description": (
                        "fresh: zero context, uses agent_type definition. "
                        "fork: inherits parent conversation, same model/tools."
                    ),
                },
                "agent_type": {
                    "type": "string",
                    "description": (
                        f"Agent type name (fresh mode only). Available: {', '.join(agent_names)}"
                    ),
                },
                "prompt": {"type": "string", "minLength": 1},
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": "Stable label for this agent instance.",
                },
                "model": {
                    "type": "string",
                    "description": "Model override (ignored in fork mode).",
                },
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 30},
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 64},
                    "description": "Names of agents that must complete first.",
                },
                "isolation": {
                    "type": "string",
                    "enum": ["worktree", "subdirectory"],
                    "description": "Override the definition's isolation mode.",
                },
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
        read_only=True,
    )


async def run_dispatch_subagents(
    call: ToolCall,
    *,
    task: TaskContract,
    model: ModelProvider,
    subagent_models: Mapping[str, ModelProvider],
    run_dir: Path,
    sandbox_checks: bool,
    state: TurnState,
    emit: EventEmitter,
    remaining: RemainingTime,
    prepare: PrepareToolCall,
    execute: ExecutePreparedCall,
    runner_factory: SubagentRunnerFactory,
    deadline: float,
    parent_messages: Sequence[ConversationItem] | None = None,
    subagent_depth: int = 0,
) -> str:
    scheduled = normalize_subagent_schedule(call.arguments.get("agents"))
    specs = {spec.id: spec for spec in scheduled}
    await emit(
        "subagents.schedule_normalized",
        count=len(scheduled),
        waves=max((spec.wave for spec in scheduled), default=-1) + 1,
        agents=[
            {
                "id": spec.id,
                "role": spec.agent_type,
                "depends_on": list(spec.depends_on),
                "wave": spec.wave,
                "max_steps": spec.max_steps,
                "proposed_transaction": spec.proposed_transaction is not None,
            }
            for spec in scheduled
        ],
    )

    def handoff_context(dependencies: tuple[str, ...], completed: dict[str, RunResult]) -> str:
        if not dependencies:
            return ""
        blocks = ["Prior subagent handoff reports:"]
        for dependency in dependencies:
            result = completed[dependency]
            blocks.append(
                "\n".join(
                    (
                        f"[{dependency}] status={result.status.value}",
                        f"summary={bounded_text(result.summary, 2_000)}",
                        f"changed_files={', '.join(result.changed_files) or '(none)'}",
                    )
                )
            )
        return "\n\n".join(blocks)

    async def execute_subagent_transaction(
        agent_id: str,
        proposed_transaction: object,
        *,
        deadline: float,
    ) -> ToolObservation:
        if not isinstance(proposed_transaction, Mapping):
            raise ValueError("subagent proposed_transaction must be an object")
        steps = proposed_transaction.get("steps")
        if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)):
            raise ValueError("subagent proposed_transaction.steps must be an array")
        transaction_call = ToolCall(
            name="tool_transaction",
            arguments={"steps": list(steps)},
            provider_metadata={"source": "dispatch_subagents", "subagent_id": agent_id},
        )
        await emit(
            "subagents.transaction_started",
            id=agent_id,
            tool_call_id=transaction_call.tool_call_id,
        )
        try:
            prepared = await prepare(transaction_call)
            decision = prepared.decision
        except ToolExecutionError as exc:
            if str(exc) == "repeated_action":
                raise ValueError("subagent proposed_transaction repeated prior action") from exc
            raise
        if decision == ApprovalDecision.CANCEL:
            raise ValueError("subagent proposed_transaction cancelled")
        if decision == ApprovalDecision.DENY:
            observation = ToolObservation(
                tool_call_id=transaction_call.tool_call_id,
                name=transaction_call.name,
                ok=False,
                error="subagent proposed_transaction denied by user",
            )
            await emit(
                "tool.completed",
                tool_call_id=transaction_call.tool_call_id,
                name=transaction_call.name,
                ok=False,
                error=observation.error,
            )
        else:
            observation = await execute(
                prepared,
                deadline=deadline,
            )
        if observation.ok:
            state.made_changes = True
        await emit(
            "subagents.transaction_completed",
            id=agent_id,
            tool_call_id=transaction_call.tool_call_id,
            ok=observation.ok,
            error=observation.error,
        )
        return observation

    async def run_one(
        spec: ScheduledSubagent,
        completed: dict[str, RunResult],
    ) -> tuple[str, RunResult, str, str]:
        agent_id = spec.id
        agent_type = spec.agent_type
        instruction = spec.instruction
        dependencies = spec.depends_on
        is_fork = spec.mode == "fork"

        definition = resolve_agent_type(agent_type)

        forked_messages: tuple[ConversationItem, ...] | None = None
        if is_fork and parent_messages is not None:
            forked_messages = build_forked_messages(parent_messages, instruction)
        else:
            handoff = handoff_context(dependencies, completed)
            role_prompt = subagent_role_instruction(agent_type)
            if handoff:
                instruction = f"{role_prompt}\n\n{handoff}\n\nTask: {instruction}"
            else:
                instruction = f"{role_prompt}\n\nTask: {instruction}"

        allowed_paths = spec.allowed_paths
        if allowed_paths is not None:
            if not isinstance(allowed_paths, Sequence) or isinstance(allowed_paths, (str, bytes)):
                raise ValueError("subagent allowed_paths must be an array")
            child_allowed_paths = tuple(str(path) for path in allowed_paths)
        else:
            child_allowed_paths = task.allowed_paths
        child_model = subagent_models.get(agent_id) or subagent_models.get(agent_type) or model
        child_can_spawn = can_spawn_at_depth(definition, subagent_depth)

        await emit(
            "subagents.agent_started",
            id=agent_id,
            agent_type=agent_type,
            mode=spec.mode,
            can_spawn=child_can_spawn,
            depth=subagent_depth,
        )
        result = await run_subagent_task(
            task,
            child_model,
            run_dir,
            instruction=instruction,
            runner_factory=runner_factory,
            subagent_id=agent_id,
            allowed_paths=child_allowed_paths,
            limits=task.limits.model_copy(update={"max_steps": spec.max_steps}),
            sandbox_checks=sandbox_checks,
            allow_unsafe_local_exec=definition.allow_execute,
            approval_policy=HeadlessApprovalPolicy(
                allow_modify=definition.allow_modify,
                allow_execute=definition.allow_execute,
            ),
            enable_subagent_dispatch=child_can_spawn,
            initial_messages=forked_messages,
        )
        await emit(
            "subagents.agent_completed",
            id=agent_id,
            agent_type=agent_type,
            status=result.status.value,
            summary=bounded_text(result.summary, 500),
            changed_files=list(result.changed_files),
        )
        return agent_id, result, child_model.provider_name, child_model.model_id

    await emit(
        "subagents.dispatch_started",
        count=len(scheduled),
        ids=[spec.id for spec in scheduled],
    )
    completed: dict[str, RunResult] = {}
    transaction_observations: dict[str, ToolObservation] = {}
    results: list[tuple[str, RunResult, str, str]] = []
    pending = dict(specs)
    while pending:
        ready_ids = [
            agent_id
            for agent_id, spec in pending.items()
            if all(dep in completed for dep in spec.depends_on)
        ]
        if not ready_ids:
            raise ValueError("subagent dependency cycle detected")
        wave = [pending.pop(agent_id) for agent_id in ready_ids]
        await emit("subagents.wave_started", ids=ready_ids)
        wave_results = await asyncio.wait_for(
            asyncio.gather(*(run_one(spec, completed) for spec in wave)),
            timeout=remaining(deadline),
        )
        for agent_id, result, _provider_name, _model_id in wave_results:
            proposed_transaction = specs[agent_id].proposed_transaction
            if proposed_transaction is not None:
                if result.status is not RunStatus.COMPLETED:
                    raise ValueError(
                        f"subagent {agent_id} did not complete; transaction not executed"
                    )
                observation = await execute_subagent_transaction(
                    agent_id,
                    proposed_transaction,
                    deadline=deadline,
                )
                transaction_observations[agent_id] = observation
                if not observation.ok:
                    raise ValueError(
                        "subagent proposed_transaction failed: "
                        f"{observation.error or bounded_text(observation.content, 500)}"
                    )
            completed[agent_id] = result
        results.extend(wave_results)
        await emit("subagents.wave_completed", ids=ready_ids)
    await emit(
        "subagents.dispatch_completed",
        count=len(results),
        ids=[agent_id for agent_id, _result, _provider_name, _model_id in results],
    )
    lines = ["[subagents-v1]"]
    for agent_id, result, provider_name, model_id in results:
        agent_type = specs[agent_id].agent_type
        depends_on = specs[agent_id].depends_on
        transaction_observation = transaction_observations.get(agent_id)
        transaction_status = (
            "(none)"
            if transaction_observation is None
            else ("ok" if transaction_observation.ok else "failed")
        )
        lines.append(
            "\n".join(
                (
                    f"## {agent_id}",
                    f"role: {agent_type}",
                    f"depends_on: {', '.join(str(dep) for dep in depends_on) or '(none)'}",
                    f"model: {provider_name}/{model_id}",
                    f"status: {result.status.value}",
                    f"terminal_reason: {result.terminal_reason}",
                    f"transaction: {transaction_status}",
                    f"changed_files: {', '.join(result.changed_files) or '(none)'}",
                    f"summary: {result.summary}",
                    f"events: {result.artifacts.get('events', '')}",
                )
            )
        )
    return "\n\n".join(lines)
