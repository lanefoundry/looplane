"""Programmatic subagent dispatch on isolated looplane run workspaces."""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Any
from uuid import uuid4

from looplane.approvals import ApprovalPolicy
from looplane.console import EventSink
from looplane.contracts import Limits, RunResult, TaskContract, VerificationCommand
from looplane.models import ModelProvider


class SubagentRole(StrEnum):
    """Named read-only subagent roles supported by looplane-native dispatch."""

    SCOUT = "scout"
    ANALYST = "analyst"
    REVIEWER = "reviewer"


SUBAGENT_ROLE_INSTRUCTIONS: dict[SubagentRole, str] = {
    SubagentRole.SCOUT: (
        "Role: scout. Inspect the requested surface and report concrete files, facts, and risks. "
        "Do not propose broad rewrites."
    ),
    SubagentRole.ANALYST: (
        "Role: analyst. Synthesize evidence into implementation guidance, tradeoffs, and the "
        "smallest next action."
    ),
    SubagentRole.REVIEWER: (
        "Role: reviewer. Review prior findings for correctness, missed risks, and verification "
        "gaps. Prefer concise findings over repetition."
    ),
}


@dataclass(frozen=True)
class ScheduledSubagent:
    """Host-normalized subagent dispatch entry."""

    id: str
    role: SubagentRole
    instruction: str
    allowed_paths: object | None
    max_steps: int
    depends_on: tuple[str, ...]
    proposed_transaction: object | None
    wave: int


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
    """Return the bounded system-style instruction for one named subagent role."""

    return SUBAGENT_ROLE_INSTRUCTIONS[SubagentRole(role)]


def normalize_subagent_schedule(
    agents: object,
    *,
    max_agents: int = 4,
    max_steps: int = 6,
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
        try:
            role = SubagentRole(raw_agent.get("role"))
        except ValueError as exc:
            raise ValueError("unsupported subagent role") from exc
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
        requested_steps = raw_agent.get("max_steps", 3)
        if (
            not isinstance(requested_steps, int)
            or requested_steps < 1
            or requested_steps > max_steps
        ):
            raise ValueError(f"subagent max_steps must be between 1 and {max_steps}")
        specs[agent_id] = ScheduledSubagent(
            id=agent_id,
            role=role,
            instruction=instruction,
            allowed_paths=raw_agent.get("allowed_paths"),
            max_steps=requested_steps,
            depends_on=tuple(dict.fromkeys(dependencies)),
            proposed_transaction=raw_agent.get("proposed_transaction"),
            wave=-1,
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
                    role=spec.role,
                    instruction=spec.instruction,
                    allowed_paths=spec.allowed_paths,
                    max_steps=spec.max_steps,
                    depends_on=spec.depends_on,
                    proposed_transaction=spec.proposed_transaction,
                    wave=wave,
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
    instruction: str,
    subagent_id: str | None = None,
    allowed_paths: tuple[str, ...] | None = None,
    verification: tuple[VerificationCommand, ...] | None = None,
    limits: Limits | None = None,
    event_sink: EventSink | None = None,
    approval_policy: ApprovalPolicy | None = None,
    sandbox_checks: bool = True,
    allow_unsafe_local_exec: bool = False,
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
    looplane_loop = importlib.import_module("looplane.loop")
    AgentRunner = looplane_loop.AgentRunner
    return await AgentRunner(
        task,
        model,
        Path(run_root) / "subagents",
        run_id=safe_id,
        sandbox_checks=sandbox_checks,
        allow_unsafe_local_exec=allow_unsafe_local_exec,
        approval_policy=approval_policy,
        event_sink=event_sink,
        enable_subagent_dispatch=False,
    ).run()
