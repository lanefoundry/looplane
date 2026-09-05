"""Declared-check evidence and advisory review policy, independent of the runner."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import shlex
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from looplane.agent.context import BlockingCall
from looplane.agent.ports import EventEmitter, RemainingTime
from looplane.agent.state import TurnState
from looplane.approvals import ApprovalDecision, ApprovalReason, ToolEffect
from looplane.contracts import (
    Message,
    ToolCall,
    ToolObservation,
    Usage,
    VerificationCommand,
    VerificationOutcome,
)
from looplane.events import JsonValue, atomic_write_json
from looplane.execution.capture import bounded_text
from looplane.models import ModelProvider, ProviderError
from looplane.tooling.types import ToolExecutionError


class CheckExecutor(Protocol):
    @property
    def verification_outcomes(self) -> MutableMapping[str, VerificationOutcome]: ...

    def workspace_fingerprint(self, *, timeout_seconds: float) -> str: ...

    def run_check(self, name: str, *, timeout_seconds: float) -> VerificationOutcome: ...


class VerificationApproval(Protocol):
    async def __call__(
        self,
        *,
        action_id: str,
        effect: ToolEffect,
        reason: ApprovalReason,
        preview: str,
        command: VerificationCommand,
    ) -> tuple[ApprovalDecision, str]: ...


class JsonWriter(Protocol):
    async def __call__(self, path: str | Path, value: JsonValue) -> None: ...


@dataclass(frozen=True)
class VerificationInputs:
    commands: tuple[VerificationCommand, ...]
    instruction: str
    max_output_bytes: int
    run_dir: Path


@dataclass(frozen=True)
class CheckedCommandEvidence:
    fingerprint: str
    outcome: VerificationOutcome
    tool_call_id: str


@dataclass(frozen=True)
class ManualCheck:
    name: str
    fingerprint: str | None


@dataclass
class VerificationCache:
    """Ephemeral evidence, never restored as trusted evidence from a session."""

    checked_workspaces: dict[str, CheckedCommandEvidence] = field(default_factory=dict)
    test_log: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class VerificationPorts:
    blocking: BlockingCall
    remaining: RemainingTime
    emit: EventEmitter
    approve: VerificationApproval
    mark_pending_started: Callable[[], Awaitable[None]]
    fingerprint: Callable[[], Awaitable[str]]
    record_usage: Callable[[str, ModelProvider, Usage], None]
    record_cache_trace: Callable[[str, ModelProvider], Awaitable[None]]
    write_json: JsonWriter = atomic_write_json


class VerificationService:
    """Check policy over explicit durable state, ephemeral evidence and narrow ports.

    A fresh service view can be bound after continuation changes the run location;
    only VerificationCache persists across calls, alongside the existing TurnState.
    """

    def __init__(
        self,
        inputs: VerificationInputs,
        state: TurnState,
        cache: VerificationCache,
        executor: CheckExecutor | None,
        ports: VerificationPorts,
        cancel_requested: asyncio.Event,
        review_model: ModelProvider | None,
    ) -> None:
        self.inputs = inputs
        self.state = state
        self.cache = cache
        self.executor = executor
        self.ports = ports
        self.cancel_requested = cancel_requested
        self.review_model = review_model

    def state_fingerprint(self, workspace_fingerprint: str) -> str:
        payload = json.dumps(
            {
                "workspace": workspace_fingerprint,
                "verification": [
                    {
                        "name": command.name,
                        "argv": list(command.argv),
                        "timeout_seconds": command.timeout_seconds,
                    }
                    for command in self.inputs.commands
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    async def verify_all(self, deadline: float) -> tuple[VerificationOutcome, ...]:
        assert self.executor is not None
        self.ports.remaining(deadline)
        verification_start_fingerprint = await self.ports.fingerprint()
        outcomes: list[VerificationOutcome] = []
        for command in self.inputs.commands:
            if self.cancel_requested.is_set():
                raise asyncio.CancelledError("run cancellation requested")
            cached = self.cache.checked_workspaces.get(command.name)
            if cached is not None:
                fingerprint, prior_outcome, tool_call_id = (
                    cached.fingerprint,
                    cached.outcome,
                    cached.tool_call_id,
                )
                current = await self.ports.fingerprint()
                if (
                    fingerprint == current == verification_start_fingerprint
                    and prior_outcome.ok
                    and prior_outcome.argv == command.argv
                ):
                    outcomes.append(prior_outcome)
                    await self.ports.emit(
                        "verification.reused",
                        name=command.name,
                        argv=command.argv,
                        tool_call_id=tool_call_id,
                        ok=True,
                        exit_code=prior_outcome.exit_code,
                    )
                    continue
            decision, _request_id = await self.ports.approve(
                action_id=f"verification:{command.name}",
                effect=ToolEffect.EXECUTE,
                reason=ApprovalReason.FINAL_VERIFICATION,
                preview=f"$ {' '.join(command.argv)}",
                command=command,
            )
            if decision == ApprovalDecision.CANCEL:
                raise asyncio.CancelledError("final verification cancelled by user")
            if self.cancel_requested.is_set():
                raise asyncio.CancelledError("run cancellation requested")
            if decision == ApprovalDecision.DENY:
                outcome = VerificationOutcome(
                    name=command.name,
                    argv=command.argv,
                    ok=False,
                    output="verification denied by user",
                )
                outcomes.append(outcome)
                await self.ports.emit(
                    "verification.completed",
                    name=outcome.name,
                    argv=outcome.argv,
                    ok=False,
                    exit_code=None,
                    duration_seconds=0.0,
                    error="approval denied",
                )
                continue
            self.ports.remaining(deadline)
            await self.ports.emit(
                "verification.started",
                name=command.name,
                argv=command.argv,
            )
            await self.ports.mark_pending_started()
            outcome = await self.ports.blocking(
                self.executor.run_check,
                command.name,
                timeout_seconds=self.ports.remaining(deadline),
            )
            outcomes.append(outcome)
            self.cache.test_log.append(
                f"$ {' '.join(outcome.argv)}\n"
                f"exit={outcome.exit_code} duration={outcome.duration_seconds:.3f}s\n"
                f"{outcome.output}\n"
            )
            await self.ports.emit(
                "verification.completed",
                name=outcome.name,
                argv=outcome.argv,
                ok=outcome.ok,
                exit_code=outcome.exit_code,
                duration_seconds=outcome.duration_seconds,
            )
            if self.cancel_requested.is_set():
                await self.persist(
                    outcomes,
                    expected_workspace_fingerprint=verification_start_fingerprint,
                )
                raise asyncio.CancelledError("run cancellation requested")
        await self.persist(
            outcomes,
            expected_workspace_fingerprint=verification_start_fingerprint,
        )
        return self.state.last_verification

    async def review(
        self,
        *,
        patch: str,
        changed_files: tuple[str, ...],
        verification: tuple[VerificationOutcome, ...],
        deadline: float,
    ) -> str | None:
        if self.review_model is None or not patch.strip():
            return None
        await self.ports.emit(
            "role_lane.requested",
            role="reviewer",
            provider=self.review_model.provider_name,
            model=self.review_model.model_id,
            changed_files=changed_files,
        )
        verification_summary = "\n".join(
            f"- {outcome.name}: {'passed' if outcome.ok else 'failed'} (exit {outcome.exit_code})"
            for outcome in verification
        )
        messages = (
            Message(
                role="system",
                content=(
                    "You are looplane's read-only reviewer lane. Review the verified patch for "
                    "correctness risks, regressions, missed tests, and security concerns. Do not "
                    "request tools or edits. Return a concise verdict with concrete findings."
                ),
            ),
            Message(
                role="user",
                content=bounded_text(
                    "Task:\n"
                    f"{self.inputs.instruction}\n\n"
                    "Changed files:\n"
                    + "\n".join(f"- {path}" for path in changed_files)
                    + "\n\nVerification:\n"
                    + verification_summary
                    + "\n\nPatch:\n"
                    + patch,
                    self.inputs.max_output_bytes,
                ),
            ),
        )
        try:
            turn = await asyncio.wait_for(
                self.review_model.complete(messages, ()),
                timeout=self.ports.remaining(deadline),
            )
            self.ports.record_usage("reviewer", self.review_model, turn.usage)
            await self.ports.record_cache_trace("reviewer", self.review_model)
            review = bounded_text(turn.content or "", self.inputs.max_output_bytes)
            (self.inputs.run_dir / "review.md").write_text(review, encoding="utf-8")
            review_cost = self.state.model_usage[-1].cost
            await self.ports.emit(
                "role_lane.completed",
                role="reviewer",
                provider=self.review_model.provider_name,
                model=self.review_model.model_id,
                usage=turn.usage.model_dump(mode="json"),
                cost=review_cost.model_dump(mode="json") if review_cost is not None else None,
                preview=bounded_text(review, 2_000),
            )
            return review
        except (TimeoutError, ProviderError, OSError, ValueError) as exc:
            await self.ports.emit(
                "role_lane.failed",
                role="reviewer",
                provider=self.review_model.provider_name,
                model=self.review_model.model_id,
                error=bounded_text(f"{type(exc).__name__}: {exc}", 2_000),
            )
            return None

    async def persist(
        self,
        outcomes: list[VerificationOutcome],
        *,
        expected_workspace_fingerprint: str | None = None,
    ) -> None:
        self.state.last_verification = tuple(outcomes)
        workspace_changed_during_verification = False
        if (
            len(outcomes) == len(self.inputs.commands)
            and all(outcome.ok for outcome in outcomes)
            and self.executor is not None
        ):
            current_fingerprint = await self.ports.fingerprint()
            workspace_changed_during_verification = (
                expected_workspace_fingerprint is None
                or current_fingerprint != expected_workspace_fingerprint
            )
            self.state.verified_workspace_fingerprint = (
                None if workspace_changed_during_verification else current_fingerprint
            )
        else:
            self.state.verified_workspace_fingerprint = None
        await self.ports.write_json(
            self.inputs.run_dir / "verification.json",
            [outcome.model_dump(mode="json") for outcome in outcomes],
        )
        (self.inputs.run_dir / "test.log").write_text(
            "\n".join(self.cache.test_log), encoding="utf-8"
        )
        if workspace_changed_during_verification:
            raise ToolExecutionError(
                "workspace changed while final verification was running; "
                "the passing result cannot verify the resulting files"
            )

    async def current_fingerprint(self) -> str:
        assert self.executor is not None
        workspace_fingerprint = await self.ports.blocking(
            self.executor.workspace_fingerprint,
            timeout_seconds=10.0,
        )
        return self.state_fingerprint(workspace_fingerprint)

    async def begin_manual_check(self, call: ToolCall) -> ManualCheck | None:
        if call.name != "run_check":
            return None
        assert self.executor is not None
        name = str(call.arguments.get("name", ""))
        self.cache.checked_workspaces.pop(name, None)
        self.executor.verification_outcomes.pop(name, None)
        fingerprint: str | None = None
        with contextlib.suppress(OSError, ToolExecutionError, TimeoutError):
            fingerprint = await self.ports.fingerprint()
        return ManualCheck(name, fingerprint)

    async def finish_manual_check(
        self,
        check: ManualCheck | None,
        observation: ToolObservation,
        tool_call_id: str,
    ) -> None:
        """Admit check evidence only after the scheduler's post-tool hook returns."""
        if check is None or not observation.ok or check.fingerprint is None:
            return
        assert self.executor is not None
        outcome = self.executor.verification_outcomes.get(check.name)
        try:
            check_end = await self.ports.fingerprint()
        except (OSError, ToolExecutionError, TimeoutError):
            check_end = None
        if outcome is not None and outcome.ok and check.fingerprint == check_end:
            self.cache.checked_workspaces[check.name] = CheckedCommandEvidence(
                check.fingerprint,
                outcome,
                tool_call_id,
            )
            self.cache.test_log.append(
                f"$ {shlex.join(outcome.argv)}\n"
                f"exit={outcome.exit_code} duration={outcome.duration_seconds:.3f}s\n"
                f"{outcome.output}\n"
            )
