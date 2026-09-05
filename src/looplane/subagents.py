"""Compatibility entry points for native subagent tasks and schedule analysis."""

from __future__ import annotations

from pathlib import Path

from looplane.agent.ports import SubagentRunnerFactory
from looplane.agent.subagent_dispatch import (
    SUBAGENT_ROLE_INSTRUCTIONS as SUBAGENT_ROLE_INSTRUCTIONS,
)
from looplane.agent.subagent_dispatch import (
    ScheduledSubagent as ScheduledSubagent,
)
from looplane.agent.subagent_dispatch import (
    SubagentRole as SubagentRole,
)
from looplane.agent.subagent_dispatch import (
    SubagentScheduleTraceAnalysis as SubagentScheduleTraceAnalysis,
)
from looplane.agent.subagent_dispatch import (
    _path_pattern_is_within as _path_pattern_is_within,
)
from looplane.agent.subagent_dispatch import (
    _validate_child_allowed_paths as _validate_child_allowed_paths,
)
from looplane.agent.subagent_dispatch import (
    _validate_subagent_id as _validate_subagent_id,
)
from looplane.agent.subagent_dispatch import (
    analyze_subagent_schedule_events as analyze_subagent_schedule_events,
)
from looplane.agent.subagent_dispatch import (
    analyze_subagent_schedule_jsonl as analyze_subagent_schedule_jsonl,
)
from looplane.agent.subagent_dispatch import (
    derive_subagent_task as derive_subagent_task,
)
from looplane.agent.subagent_dispatch import (
    normalize_subagent_schedule as normalize_subagent_schedule,
)
from looplane.agent.subagent_dispatch import (
    run_subagent_task as _run_subagent_task,
)
from looplane.agent.subagent_dispatch import (
    subagent_role_instruction as subagent_role_instruction,
)
from looplane.approvals import ApprovalPolicy
from looplane.contracts import Limits, RunResult, TaskContract, VerificationCommand
from looplane.events import EventSink
from looplane.models import ModelProvider


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
    runner_factory: SubagentRunnerFactory | None = None,
) -> RunResult:
    """Supply the legacy default factory at the public boundary, never in a leaf."""

    if runner_factory is None:
        from looplane.loop import AgentRunner

        runner_factory = AgentRunner
    return await _run_subagent_task(
        parent,
        model,
        run_root,
        runner_factory=runner_factory,
        instruction=instruction,
        subagent_id=subagent_id,
        allowed_paths=allowed_paths,
        verification=verification,
        limits=limits,
        event_sink=event_sink,
        approval_policy=approval_policy,
        sandbox_checks=sandbox_checks,
        allow_unsafe_local_exec=allow_unsafe_local_exec,
    )
