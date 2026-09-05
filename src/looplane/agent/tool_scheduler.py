"""Tool preparation, approval lookahead, and ordered concurrent read execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from looplane.agent.checkpoints import RunPersistence
from looplane.agent.context import BlockingCall
from looplane.agent.ports import (
    ApprovalCall,
    DispatchSubagents,
    EventEmitter,
    ExecutePreparedCall,
    HookCall,
    MarkActionStarted,
    PreparedToolCall,
    PrepareToolCall,
    RemainingTime,
    ToolExecutionPort,
)
from looplane.agent.state import TurnState
from looplane.approvals import (
    ApprovalDecision,
    ApprovalReason,
    ToolEffect,
    effect_for_tool_definition,
)
from looplane.contracts import ToolCall, ToolDefinition, ToolObservation, VerificationCommand
from looplane.execution.capture import bounded_text
from looplane.hooks import HookEventName
from looplane.tooling.types import ToolExecutionError

BlockingResult = TypeVar("BlockingResult")


async def run_blocking_safely(
    cancel_requested: asyncio.Event,
    function: Callable[..., BlockingResult],
    /,
    *args: Any,
    **kwargs: Any,
) -> BlockingResult:
    """Defer task cancellation until one started blocking side effect has returned."""

    blocking_task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    while True:
        try:
            return await asyncio.shield(blocking_task)
        except asyncio.CancelledError:
            cancel_requested.set()


def fingerprint(call: ToolCall) -> str:
    payload = json.dumps(
        {"name": call.name, "arguments": call.arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def record_fingerprint(state: TurnState, call: ToolCall) -> bool:
    value = fingerprint(call)
    if value == state.last_fingerprint:
        state.repeat_count += 1
    else:
        state.last_fingerprint = value
        state.repeat_count = 1
    return state.repeat_count >= 3


def event_arguments(arguments: dict[str, Any], max_output_bytes: int) -> dict[str, Any]:
    """Keep event logs bounded without losing the action's audit identity."""

    encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True).encode("utf-8")
    limit = min(max_output_bytes, 20_000)
    if len(encoded) <= limit:
        return arguments
    return {
        "omitted": True,
        "utf8_bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def tool_preview(call: ToolCall, verification_commands: Mapping[str, VerificationCommand]) -> str:
    if call.name == "apply_patch":
        return str(call.arguments.get("patch", ""))
    if call.name == "replace_text":
        return json.dumps(
            {
                "path": call.arguments.get("path"),
                "old_text": call.arguments.get("old_text"),
                "new_text": call.arguments.get("new_text"),
            },
            ensure_ascii=False,
            indent=2,
        )
    if call.name == "run_check":
        name = call.arguments.get("name")
        if isinstance(name, str):
            command = verification_commands.get(name)
            if command is not None:
                return "$ " + shlex.join(command.argv)
    return json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, indent=2)


def definition_by_name(definitions: Sequence[ToolDefinition], name: str) -> ToolDefinition | None:
    return next((definition for definition in definitions if definition.name == name), None)


def can_execute_concurrently(call: ToolCall, definitions: Sequence[ToolDefinition]) -> bool:
    definition = definition_by_name(definitions, call.name)
    if definition is None:
        return False
    return (
        definition.read_only
        and definition.concurrency_safe
        and effect_for_tool_definition(call.name, definition) is ToolEffect.READ
    )


async def prepare_tool_call(
    call: ToolCall,
    *,
    state: TurnState,
    definitions: Sequence[ToolDefinition],
    verification_commands: Mapping[str, VerificationCommand],
    validate_patch: Callable[[Any], object],
    max_output_bytes: int,
    emit: EventEmitter,
    hook: HookCall,
    approve: ApprovalCall,
) -> PreparedToolCall:
    if record_fingerprint(state, call):
        raise ToolExecutionError("repeated_action")
    try:
        effect = effect_for_tool_definition(call.name, definition_by_name(definitions, call.name))
    except ValueError:
        raise ToolExecutionError(f"unknown_tool:{call.name}") from None
    await emit(
        "tool.requested",
        tool_call_id=call.tool_call_id,
        name=call.name,
        effect=effect.value,
        arguments=event_arguments(call.arguments, max_output_bytes),
    )
    if call.name == "apply_patch":
        try:
            validate_patch(call.arguments.get("patch", ""))
        except (ToolExecutionError, ValueError) as exc:
            raise ToolExecutionError(f"invalid_patch:{exc}") from exc
    hook_decision = await hook(
        HookEventName.PRE_TOOL_USE,
        {
            "tool_call": call.model_dump(mode="json"),
            "effect": effect.value,
        },
    )
    if hook_decision is not None:
        return PreparedToolCall(call, ApprovalDecision.DENY, effect, None)
    decision, request_id = await approve(
        action_id=call.tool_call_id,
        effect=effect,
        reason=ApprovalReason.MODEL_TOOL,
        preview=tool_preview(call, verification_commands),
        tool_call=call,
    )
    return PreparedToolCall(call, decision, effect, request_id)


async def execute_prepared_tool_call(
    prepared: PreparedToolCall,
    *,
    executor: ToolExecutionPort,
    persistence: RunPersistence,
    blocking: BlockingCall,
    remaining: RemainingTime,
    emit: EventEmitter,
    hook: HookCall,
    mark_started: MarkActionStarted,
    dispatch: DispatchSubagents,
    deadline: float,
) -> ToolObservation:
    call = prepared.call
    effect = prepared.effect
    request_id = prepared.request_id
    await emit(
        "tool.started",
        tool_call_id=call.tool_call_id,
        name=call.name,
        effect=effect.value,
    )
    if request_id is not None and persistence.manifest is not None:
        pending = persistence.manifest.pending_action
        if pending is not None and pending.request_id == request_id:
            await mark_started(request_id)
    if call.name == "dispatch_subagents":
        observation = await dispatch(call, deadline=deadline)
    else:
        observation = await blocking(
            executor.execute,
            call,
            timeout_seconds=remaining(deadline),
        )
    verification_data: dict[str, Any] = {}
    if call.name == "run_check":
        outcome = executor.verification_outcomes.get(str(call.arguments.get("name", "")))
        if outcome is not None:
            verification_data["verification"] = outcome.model_dump(mode="json")
            verification_data["verification"]["output"] = bounded_text(outcome.output, 2_000)
    await emit(
        "tool.completed",
        tool_call_id=call.tool_call_id,
        name=call.name,
        ok=observation.ok,
        error=observation.error,
        preview=bounded_text(observation.content, 2_000),
        **verification_data,
    )
    await hook(
        HookEventName.POST_TOOL_USE,
        {
            "tool_call": call.model_dump(mode="json"),
            "observation": observation.model_dump(mode="json"),
            "effect": effect.value,
        },
    )
    return observation


@dataclass(frozen=True)
class ReadOnlyBatch:
    calls: tuple[PreparedToolCall, ...]
    deferred_denial: tuple[ToolCall, ToolObservation] | None = None
    cancel_reason: Literal["user_cancelled", "approval_cancelled"] | None = None


async def prepare_read_only_batch(
    first: PreparedToolCall,
    candidates: Sequence[ToolCall],
    *,
    can_execute: Callable[[ToolCall], bool],
    prepare: PrepareToolCall,
    cancel_requested: asyncio.Event,
) -> ReadOnlyBatch:
    calls = [first]
    for candidate in candidates:
        if not can_execute(candidate):
            break
        if cancel_requested.is_set():
            return ReadOnlyBatch(tuple(calls), cancel_reason="user_cancelled")
        try:
            prepared = await prepare(candidate)
        except ToolExecutionError as exc:
            error = str(exc)
            if error == "repeated_action" or error.startswith("unknown_tool:"):
                break
            raise
        if prepared.decision == ApprovalDecision.CANCEL:
            return ReadOnlyBatch(tuple(calls), cancel_reason="approval_cancelled")
        if prepared.decision == ApprovalDecision.DENY:
            return ReadOnlyBatch(
                tuple(calls),
                deferred_denial=(
                    candidate,
                    ToolObservation(
                        tool_call_id=candidate.tool_call_id,
                        name=candidate.name,
                        ok=False,
                        error="action denied by user",
                    ),
                ),
            )
        if prepared.effect is not ToolEffect.READ:
            break
        calls.append(prepared)
    return ReadOnlyBatch(tuple(calls))


async def execute_read_only_batch(
    calls: Sequence[PreparedToolCall],
    *,
    execute: ExecutePreparedCall,
    emit: EventEmitter,
    deadline: float,
) -> list[ToolObservation]:
    if len(calls) > 1:
        await emit(
            "tool.batch_started",
            count=len(calls),
            tool_call_ids=[prepared.call.tool_call_id for prepared in calls],
            mode="read_only_parallel",
        )
    tasks = [asyncio.create_task(execute(prepared, deadline=deadline)) for prepared in calls]
    try:
        observations = await asyncio.gather(*tasks)
        return list(observations)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if len(calls) > 1:
            await emit(
                "tool.batch_completed",
                count=len(calls),
                tool_call_ids=[prepared.call.tool_call_id for prepared in calls],
                mode="read_only_parallel",
            )
