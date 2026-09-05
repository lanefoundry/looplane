"""Explicit, provider-neutral coding-agent loop."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import json
import random
import shlex
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from looplane.agent import context
from looplane.agent.checkpoints import (
    RunPersistence,
    check_resume_identity,
    claim_session,
    session_phase,
)
from looplane.agent.run_lifecycle import (
    BoundedRunLifecycle,
    validate_run_id,
    validate_run_location,
)
from looplane.agent.state import ContextState, TurnState
from looplane.approvals import (
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalReason,
    ApprovalRequest,
    HeadlessApprovalPolicy,
    ToolEffect,
    effect_for_tool_definition,
)
from looplane.cache_strategy import ProviderCacheTrace
from looplane.console import CompositeEventSink, EventSink, JsonlEventSink
from looplane.context_providers import (
    ContextProviderRunner,
    load_project_context_provider_runner,
)
from looplane.contracts import (
    ConversationItem,
    Message,
    ModelTurn,
    ModelUsageRecord,
    RunResult,
    RunStatus,
    TaskContract,
    ToolCall,
    ToolDefinition,
    ToolObservation,
    Usage,
    VerificationCommand,
    VerificationOutcome,
)
from looplane.events import EventWriter, atomic_write_json
from looplane.hooks import HookDecision, HookEventName, HookRunner, load_project_hook_runner
from looplane.mcp_client import load_native_mcp_server_configs
from looplane.models import ModelProvider, ProviderError
from looplane.permissions import PermissionGuard
from looplane.policy import SafePathPolicy
from looplane.prompts import (
    CODING_AGENT_PROMPT_VERSION,
)
from looplane.provider_catalog import estimate_cost
from looplane.runtime import (
    LocalGitWorkspace,
    WorkspacePreparationError,
    bounded_text,
    run_bounded_command,
    sanitized_subprocess_env,
)
from looplane.session import (
    ApprovalAuditRecord,
    SessionBusyError,
    SessionManifest,
    SessionPhase,
    SessionValidationError,
)
from looplane.tools import ToolExecutionError, ToolExecutor


class UnsafeLocalExecutionError(RuntimeError):
    """Raised unless the caller explicitly accepts unsandboxed repository code execution."""


BlockingResult = TypeVar("BlockingResult")
MODEL_ATTEMPTS = 5
RETRY_BACKOFF_BASE_SECONDS = 1.0
RETRY_MAX_DELAY_SECONDS = 30.0
RETRY_SERVER_HINT_MAX_SECONDS = 300.0
RETRY_JITTER_FRACTION = 0.15
READ_ONLY_STALL_THRESHOLD = 4


def retry_delay_seconds(attempt: int, retry_after_seconds: float | None) -> float:
    """Exponential backoff with ±15% jitter; a server Retry-After hint wins verbatim.

    The hint bypasses the local backoff curve but is capped for safety, mirroring
    how Claude Code treats the header as a server directive above local policy.
    """

    if retry_after_seconds is not None:
        return min(max(retry_after_seconds, 0.0), RETRY_SERVER_HINT_MAX_SECONDS)
    base = min(RETRY_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), RETRY_MAX_DELAY_SECONDS)
    return base * random.uniform(1.0 - RETRY_JITTER_FRACTION, 1.0 + RETRY_JITTER_FRACTION)


class AgentRunner:
    """Run one bounded task and persist an auditable artifact bundle."""

    def __init__(
        self,
        task: TaskContract,
        model: ModelProvider,
        run_root: str | Path,
        *,
        run_id: str | None = None,
        durable_events: bool = True,
        continuation: bool = False,
        allow_unsafe_local_exec: bool = False,
        allow_direct_repo_edit: bool = False,
        approval_policy: ApprovalPolicy | None = None,
        permission_guard: PermissionGuard | None = None,
        fallback_models: Sequence[ModelProvider] = (),
        review_model: ModelProvider | None = None,
        sandbox_checks: bool = False,
        sandbox_profile: str | None = None,
        sandbox_backend: str | None = None,
        sandbox_read_roots: Sequence[Path] = (),
        event_sink: EventSink | None = None,
        hook_runner: HookRunner | None = None,
        context_provider_runner: ContextProviderRunner | None = None,
        enable_subagent_dispatch: bool = True,
        subagent_models: Mapping[str, ModelProvider] | None = None,
    ) -> None:
        self.task = task
        self.model_retry_delay = retry_delay_seconds
        self._model_candidates: tuple[ModelProvider, ...] = (model, *fallback_models)
        self._review_model = review_model
        self._sandbox_checks = sandbox_checks
        self._sandbox_profile = sandbox_profile or "verification"
        self._sandbox_backend = sandbox_backend or "auto"
        self._sandbox_read_roots = tuple(Path(root) for root in sandbox_read_roots)
        self._active_model_index = 0
        self.run_root = Path(run_root).resolve(strict=False)
        self.run_id = run_id or uuid4().hex
        validate_run_id(self.run_id)
        self.run_dir = self.run_root / self.run_id
        self.events = EventWriter(self.run_dir / "events.jsonl", durable=durable_events)
        self.allow_unsafe_local_exec = allow_unsafe_local_exec
        self.allow_direct_repo_edit = allow_direct_repo_edit
        self.permission_guard = permission_guard
        self._hook_runner = hook_runner or load_project_hook_runner(self.task.repository)
        self._context_provider_runner = (
            context_provider_runner or load_project_context_provider_runner(self.task.repository)
        )
        self._enable_subagent_dispatch = enable_subagent_dispatch
        self._subagent_models = dict(subagent_models or {})
        self.approvals = approval_policy or HeadlessApprovalPolicy(
            allow_modify=True,
            allow_execute=allow_unsafe_local_exec,
        )
        self._interactive_approvals = approval_policy is not None
        self._external_event_sink = event_sink
        durable_sink = JsonlEventSink(self.events)
        self._event_sink: EventSink = (
            CompositeEventSink((durable_sink, event_sink)) if event_sink else durable_sink
        )
        self._state = TurnState()
        self._context_state = ContextState()
        self._persistence = RunPersistence(self.run_id, self.run_dir, self._event_sink)
        self._lifecycle = BoundedRunLifecycle(self._persistence)
        self._run_dir_initialized = False
        self._consecutive_read_only_steps = 0
        self._test_log: list[str] = []
        self._executor: ToolExecutor | None = None
        self._checked_workspaces: dict[str, tuple[str, VerificationOutcome, str]] = {}
        self._wall_time_phase = "task execution"
        self._resume_ready = False
        self._continuation = continuation
        self._continuation_fallback_reason: str | None = None
        self._turn_start_step = 0
        self._is_continuation_turn = False
        self._cancel_requested = asyncio.Event()

    @property
    def model(self) -> ModelProvider:
        """The active model candidate; advances when a fallback is applied."""

        return self._model_candidates[self._active_model_index]

    def request_cancel(self) -> None:
        """Request a cooperative stop at the next side-effect-safe boundary."""

        self._cancel_requested.set()

    async def _run_blocking_safely(
        self,
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
                self._cancel_requested.set()

    @classmethod
    async def resume(
        cls,
        run_dir: str | Path,
        model: ModelProvider,
        *,
        approval_policy: ApprovalPolicy,
        event_sink: EventSink | None = None,
        durable_events: bool = True,
    ) -> AgentRunner:
        """Open a non-terminal persisted session after strict workspace/event validation."""

        resolved = Path(run_dir).resolve(strict=True)
        claimed = await claim_session(resolved, durable=durable_events)
        store, lease, manifest, task = (
            claimed.store,
            claimed.lease,
            claimed.manifest,
            claimed.task,
        )
        try:
            cls._check_resume_identity(manifest, model)
            runner = cls(
                task,
                model,
                resolved.parent,
                run_id=resolved.name,
                durable_events=durable_events,
                approval_policy=approval_policy,
                event_sink=event_sink,
            )
            runner._persistence.store = store
            runner._persistence.lease = lease
            runner._restore_state_from_manifest(manifest)
            # A resumed workspace may already contain modifications from before the
            # interruption, so keep the final-verification gate conservatively armed.
            runner._state.made_changes = True
            runner._run_dir_initialized = True
            runner._rebuild_executor_for(resolved / "workspace")
            runner._resume_ready = True
            return runner
        except BaseException:
            lease.release()
            raise

    @staticmethod
    def _check_resume_identity(manifest: SessionManifest, model: ModelProvider) -> None:
        check_resume_identity(
            manifest,
            provider_name=model.provider_name,
            model_id=model.model_id,
            protocol=str(model.protocol),
        )

    def _restore_state_from_manifest(self, manifest: SessionManifest) -> None:
        self._persistence.manifest = manifest
        self._persistence.sequence = manifest.last_event_sequence + 1
        self._state.restore(manifest)
        self._context_state.restore(self._state.messages)

    def _rebuild_executor_for(self, workspace_path: Path) -> None:
        self._executor = ToolExecutor(
            workspace=workspace_path,
            policy=SafePathPolicy(workspace_path, self.task.allowed_paths),
            verification_commands=self.task.verification,
            limits=self.task.limits,
            mcp_servers=load_native_mcp_server_configs(self.task.repository),
            sandbox_checks=self._sandbox_checks,
            sandbox_profile=self._sandbox_profile,
            sandbox_backend=self._sandbox_backend,
            sandbox_read_roots=self._sandbox_read_roots,
        )

    def _rebind_run_location(self, run_id: str) -> None:
        """Point this runner at a different run_id/run_dir, rebuilding the event sink."""

        self.run_id = run_id
        self.run_dir = self.run_root / self.run_id
        self.events = EventWriter(self.run_dir / "events.jsonl", durable=self.events.durable)
        durable_sink = JsonlEventSink(self.events)
        self._event_sink = (
            CompositeEventSink((durable_sink, self._external_event_sink))
            if self._external_event_sink
            else durable_sink
        )
        self._persistence.run_id = self.run_id
        self._persistence.run_dir = self.run_dir
        self._persistence.event_sink = self._event_sink

    async def _open_continuation(self) -> None:
        """Reopen this run's completed session and append a new user turn."""

        resolved = self.run_dir.resolve(strict=True)
        claimed = await claim_session(resolved, durable=self.events.durable, allow_terminal=True)
        store, lease, manifest, persisted_task = (
            claimed.store,
            claimed.lease,
            claimed.manifest,
            claimed.task,
        )
        try:
            self._check_resume_identity(manifest, self.model)
            new_instruction = self.task.instruction
            self.task = persisted_task
            self._restore_state_from_manifest(manifest)
            self._run_dir_initialized = True
            self._rebuild_executor_for(resolved / "workspace")
            try:
                workspace_fingerprint = await self._run_blocking_safely(
                    self._executor.workspace_fingerprint,
                    timeout_seconds=10.0,
                )
            except (OSError, ToolExecutionError, TimeoutError):
                # Legacy sessions and unreadable/mutated workspaces keep the
                # conservative final-verification gate armed.
                self._state.made_changes = True
            else:
                self._state.made_changes = (
                    self._verification_state_fingerprint(workspace_fingerprint)
                    != self._state.verified_workspace_fingerprint
                )
            self._state.messages.append(Message(role="user", content=new_instruction))
            self._turn_start_step = self._state.step
            self._state.repeat_count = 0
            self._state.last_fingerprint = None
            assert self._persistence.manifest is not None
            self._persistence.manifest = self._persistence.manifest.model_copy(
                update={
                    "phase": SessionPhase.RUNNING,
                    "terminal": False,
                    "messages": tuple(self._state.messages),
                    "repeat_count": 0,
                    "last_action_fingerprint": None,
                    "active_wall_time_seconds": 0.0,
                    "active_started_at": None,
                }
            )
            self._persistence.store = store
            self._persistence.lease = lease
            await self._save_manifest()
            self._is_continuation_turn = True
            self._resume_ready = True
        except BaseException:
            lease.release()
            raise

    async def _event(self, event_type: str, **data: Any) -> None:
        await self._persistence.emit(
            self.task.task_id, self._state, self._lifecycle.clock, event_type, **data
        )

    async def _run_hook(
        self,
        event: HookEventName,
        payload: dict[str, Any],
    ) -> HookDecision | None:
        if not self._hook_runner.enabled:
            return None
        try:
            decision = await self._run_blocking_safely(
                self._hook_runner.run,
                event,
                {
                    "run_id": self.run_id,
                    "task_id": self.task.task_id,
                    "sequence": self._persistence.sequence,
                    **payload,
                },
            )
        except Exception as exc:
            decision = HookDecision(
                decision="deny",
                reason=bounded_text(f"hook failed closed: {type(exc).__name__}: {exc}", 2_000),
                hook=event.value,
            )
        await self._event(
            "hook.completed" if decision is None else "hook.denied",
            hook_event=event.value,
            decision=decision.decision if decision is not None else None,
            reason=decision.reason if decision is not None else "",
            hook=decision.hook if decision is not None else "",
        )
        return decision

    async def _save_manifest(self) -> None:
        await self._persistence.save()

    @staticmethod
    def _session_phase(status: RunStatus) -> SessionPhase:
        return session_phase(status)

    async def _approval(
        self,
        *,
        action_id: str,
        effect: ToolEffect,
        reason: ApprovalReason,
        preview: str,
        tool_call: ToolCall | None = None,
        command: VerificationCommand | None = None,
    ) -> tuple[ApprovalDecision, str]:
        preview_text = bounded_text(preview, 16_000)
        guard_subjects = self._guard_subjects(tool_call=tool_call, command=command)
        request = ApprovalRequest(
            run_id=self.run_id,
            action_id=action_id,
            effect=effect,
            reason=reason,
            preview=preview_text,
            tool_call=tool_call,
            command=command,
        )
        hook_decision = await self._run_hook(
            HookEventName.APPROVAL_REQUEST,
            {
                "approval_request": request.model_dump(mode="json"),
                "subjects": list(guard_subjects),
            },
        )
        if self.permission_guard is not None and effect is ToolEffect.EXECUTE:
            policy_reason = self.permission_guard.approval_policy_reason(request, guard_subjects)
            request = request.model_copy(update={"policy_reason": policy_reason})
        # Deny-first guard: forbidden operations win even over session grants
        # and dangerous-mode auto-approval, so evaluate it before reuse.
        pre_decision: ApprovalDecision | None = None
        if hook_decision is not None:
            pre_decision = ApprovalDecision.DENY
            if not request.policy_reason:
                request = request.model_copy(
                    update={"policy_reason": f"hook denied: {hook_decision.reason}"}
                )
        elif self.permission_guard is not None:
            pre_decision = self.permission_guard.pre_decision(
                request,
                guard_subjects,
            )
        if (
            pre_decision is None
            and self._persistence.manifest is not None
            and effect in self._persistence.manifest.granted_effects
        ):
            self._persistence.manifest = self._persistence.manifest.model_copy(
                update={
                    "phase": SessionPhase.RUNNING,
                    "pending_action": None if effect is ToolEffect.READ else request,
                }
            )
            await self._save_manifest()
            await self._event(
                "approval.reused",
                request_id=request.request_id,
                action_id=action_id,
                effect=effect.value,
                reason=reason.value,
                policy_reason=request.policy_reason,
            )
            return ApprovalDecision.ALLOW_ONCE, request.request_id
        if self._persistence.manifest is not None:
            self._persistence.manifest = self._persistence.manifest.model_copy(
                update={
                    "phase": SessionPhase.WAITING_APPROVAL,
                    "pending_action": request,
                }
            )
        if reason is ApprovalReason.FINAL_VERIFICATION:
            self._wall_time_phase = "final verification"
        if pre_decision is None:
            try:
                await self._pause_active_wall_time()
                await self._event(
                    "approval.requested",
                    request_id=request.request_id,
                    action_id=action_id,
                    effect=effect.value,
                    reason=reason.value,
                    policy_reason=request.policy_reason,
                    preview=request.preview,
                )
                decision = await self.approvals.decide(request)
            finally:
                await self._resume_active_wall_time()
        else:
            await self._save_manifest()
            await self._event(
                "approval.requested",
                request_id=request.request_id,
                action_id=action_id,
                effect=effect.value,
                reason=reason.value,
                policy_reason=request.policy_reason,
                preview=request.preview,
            )
            decision = pre_decision
        if self._persistence.manifest is not None:
            granted_effects = self._persistence.manifest.granted_effects
            if decision == ApprovalDecision.ALLOW_SESSION:
                granted_effects = frozenset((*granted_effects, effect))
            pending_action = request if effect is not ToolEffect.READ else None
            if decision in {ApprovalDecision.DENY, ApprovalDecision.CANCEL}:
                pending_action = None
            self._persistence.manifest = self._persistence.manifest.model_copy(
                update={
                    "phase": SessionPhase.RUNNING,
                    "pending_action": pending_action,
                    "granted_effects": granted_effects,
                    "approval_history": (
                        *self._persistence.manifest.approval_history,
                        ApprovalAuditRecord(request=request, decision=decision),
                    ),
                }
            )
            await self._save_manifest()
        await self._event(
            "approval.resolved",
            request_id=request.request_id,
            action_id=action_id,
            effect=effect.value,
            reason=reason.value,
            policy_reason=request.policy_reason,
            decision=decision.value,
        )
        return decision, request.request_id

    async def _mark_approved_action_started(self, request_id: str) -> None:
        """Clear an approved action only after its started event is durable."""

        if self._persistence.manifest is None:
            return
        pending = self._persistence.manifest.pending_action
        if pending is None or pending.request_id != request_id:
            raise SessionValidationError("approved action no longer matches session state")
        self._persistence.manifest = self._persistence.manifest.model_copy(
            update={"pending_action": None}
        )
        await self._save_manifest()

    async def _reconcile_interrupted_approval(self) -> None:
        """Fail closed when a process stopped after requesting but before resolving approval.

        No side effect has started at this point.  Resume records that fact and gives the model a
        canonical failure/user message so it can request the action again.  This avoids both
        silently executing a stale approval and sending an orphaned tool call to the provider.
        """

        if self._persistence.manifest is None or self._persistence.manifest.pending_action is None:
            return
        pending = self._persistence.manifest.pending_action
        if pending.tool_call is not None:
            self._state.messages.append(
                ToolObservation(
                    tool_call_id=pending.tool_call.tool_call_id,
                    name=pending.tool_call.name,
                    ok=False,
                    error=(
                        "approval was interrupted before execution; the action was not performed"
                    ),
                )
            )
        else:
            self._state.messages.append(
                Message(
                    role="user",
                    content=(
                        "Final verification approval was interrupted before the command ran. "
                        "Continue the task and finish again when ready; verification will require "
                        "a new approval."
                    ),
                )
            )
        self._persistence.manifest = self._persistence.manifest.model_copy(
            update={
                "phase": SessionPhase.RUNNING,
                "pending_action": None,
                "messages": tuple(self._state.messages),
            }
        )
        await self._save_manifest()
        await self._event(
            "approval.abandoned",
            request_id=pending.request_id,
            action_id=pending.action_id,
            effect=pending.effect.value,
            reason="process_interrupted_before_decision",
        )

    def _tool_preview(self, call: ToolCall) -> str:
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
        if call.name == "run_check" and self._executor is not None:
            name = call.arguments.get("name")
            if isinstance(name, str):
                command = self._executor.verification_commands.get(name)
                if command is not None:
                    return "$ " + shlex.join(command.argv)
        return json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, indent=2)

    def _guard_subjects(
        self,
        *,
        tool_call: ToolCall | None,
        command: VerificationCommand | None,
    ) -> tuple[str, ...]:
        """Text subjects the permission guard matches deny rules against."""

        parts: list[str] = []
        if command is not None:
            parts.append(" ".join(command.argv))
        if tool_call is not None:
            if tool_call.name == "run_check" and self._executor is not None:
                check = self._executor.verification_commands.get(
                    str(tool_call.arguments.get("name", ""))
                )
                if check is not None:
                    parts.append(" ".join(check.argv))
            if tool_call.name == "tool_transaction" and self._executor is not None:
                steps = tool_call.arguments.get("steps")
                if isinstance(steps, Sequence) and not isinstance(steps, (str, bytes)):
                    for step in steps:
                        if not isinstance(step, Mapping) or step.get("op") != "run_check":
                            continue
                        args = step.get("args", {})
                        if not isinstance(args, Mapping):
                            continue
                        check = self._executor.verification_commands.get(str(args.get("name", "")))
                        if check is not None:
                            parts.append(" ".join(check.argv))
            parts.extend(
                str(value) for value in tool_call.arguments.values() if isinstance(value, str)
            )
        return tuple(parts)

    async def _checkpoint(self, status: RunStatus, **metadata: Any) -> None:
        await self._persistence.checkpoint(self.task.task_id, self._state, status, **metadata)

    @staticmethod
    def _add_usage(left: Usage, right: Usage) -> Usage:
        return Usage(
            input_tokens=left.input_tokens + right.input_tokens,
            output_tokens=left.output_tokens + right.output_tokens,
            cached_input_tokens=left.cached_input_tokens + right.cached_input_tokens,
            reasoning_tokens=left.reasoning_tokens + right.reasoning_tokens,
            provider_total_tokens=left.total_tokens + right.total_tokens,
        )

    def _record_model_usage(self, lane: str, model: ModelProvider, usage: Usage) -> None:
        self._state.usage = self._add_usage(self._state.usage, usage)
        self._state.model_usage.append(
            ModelUsageRecord(
                lane=lane,
                provider=model.provider_name,
                model=model.model_id,
                usage=usage,
                cost=estimate_cost(model.provider_name, model.model_id, usage),
            )
        )

    async def _record_provider_cache_trace(self, lane: str, model: ModelProvider) -> None:
        trace = getattr(model, "last_cache_trace", None)
        if not isinstance(trace, ProviderCacheTrace):
            return
        payload = {
            "step": self._state.step,
            "lane": lane,
            "provider": model.provider_name,
            "model": model.model_id,
            "trace": {
                "provider": trace.provider,
                "prompt_cache_key": trace.prompt_cache_key,
                "tool_schema_fingerprint": trace.tool_schema_fingerprint,
                "cache_control_blocks": trace.cache_control_blocks,
                "warnings": list(trace.warnings),
                "cache_ready": trace.cache_ready,
            },
        }
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with (self.run_dir / "cache-traces.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        await self._event(
            "model.cache_trace",
            step=self._state.step,
            lane=lane,
            provider=model.provider_name,
            model=model.model_id,
            cache_ready=trace.cache_ready,
            prompt_cache_key=trace.prompt_cache_key,
            tool_schema_fingerprint=trace.tool_schema_fingerprint,
            cache_control_blocks=trace.cache_control_blocks,
            warnings=list(trace.warnings),
        )

    def _aggregate_cost(self):
        if not self._state.model_usage:
            return estimate_cost(self.model.provider_name, self.model.model_id, self._state.usage)
        providers = {(record.provider, record.model) for record in self._state.model_usage}
        if len(providers) != 1:
            return None
        provider, model = next(iter(providers))
        return estimate_cost(provider, model, self._state.usage)

    def _current_active_wall_time(self) -> float:
        return self._lifecycle.current_active_wall_time()

    async def _pause_active_wall_time(self) -> None:
        return await self._lifecycle.pause_active_wall_time()

    async def _resume_active_wall_time(self) -> None:
        return await self._lifecycle.resume_active_wall_time()

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._current_active_wall_time()
        if remaining <= 0:
            raise TimeoutError("task wall-time budget exhausted")
        return remaining

    def _wall_time_failure_summary(self, final_summary: str) -> str:
        if self._wall_time_phase == "model request":
            detail = "Model request exceeded the remaining wall-time budget."
        elif self._wall_time_phase == "final verification":
            detail = "Final verification exceeded the remaining wall-time budget."
        else:
            detail = "Task execution exceeded the remaining wall-time budget."
        return f"{final_summary}\n\n{detail}".strip()

    def _event_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Keep event logs bounded without losing the action's audit identity."""

        encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True).encode("utf-8")
        limit = min(self.task.limits.max_tool_output_bytes, 20_000)
        if len(encoded) <= limit:
            return arguments
        return {
            "omitted": True,
            "utf8_bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }

    def _validate_run_location(self) -> None:
        validate_run_location(self.task.repository, self.run_dir)

    def _resolve_base_sha(self, deadline: float) -> str:
        if self.task.base_sha is not None:
            return self.task.base_sha
        result = run_bounded_command(
            ("git", "rev-parse", "HEAD"),
            cwd=self.task.repository.resolve(strict=True),
            timeout_seconds=min(30.0, self._remaining(deadline)),
            max_output_chars=2_000,
            env=sanitized_subprocess_env(),
        )
        sha = result.stdout.strip()
        if not result.ok or len(sha) != 40:
            raise WorkspacePreparationError("could not resolve source repository HEAD")
        return sha

    def _preexisting_dirty_paths(self, deadline: float) -> frozenset[str]:
        """Paths already dirty in the real repo before this run touched anything.

        Read-only; used only to exclude pre-existing dirt from what this run reports
        or is held accountable for when editing the real repository directly.
        """

        result = run_bounded_command(
            ("git", "status", "--porcelain=v1", "--no-renames", "-z"),
            cwd=self.task.repository.resolve(strict=True),
            timeout_seconds=min(30.0, self._remaining(deadline)),
            max_output_chars=2_000_000,
            env=sanitized_subprocess_env(task_home=self.run_dir / ".task-env"),
        )
        if not result.ok or result.stdout_truncated:
            return frozenset()
        paths: set[str] = set()
        for entry in result.stdout.split("\x00"):
            if len(entry) > 3:
                paths.add(entry[3:])
        return frozenset(paths)

    def _initial_git_status(self) -> tuple[str, ...]:
        return context.initial_git_status(
            self._executor.workspace if self._executor is not None else None,
            self.run_dir,
            self.allow_direct_repo_edit,
        )

    def _initial_messages(self, base_sha: str) -> list[ConversationItem]:
        return context.initial_messages(
            self.task,
            self._context_state,
            base_sha,
            provider_tools=self._provider_tool_definitions() if self._executor is not None else (),
            workspace=self._executor.workspace if self._executor is not None else None,
            run_dir=self.run_dir,
            allow_direct_repo_edit=self.allow_direct_repo_edit,
            enable_subagent_dispatch=self._enable_subagent_dispatch,
            sandbox_backend=self._sandbox_backend,
            sandbox_checks=self._sandbox_checks,
            sandbox_profile=self._sandbox_profile,
        )

    @staticmethod
    def _fingerprint(call: ToolCall) -> str:
        payload = json.dumps(
            {"name": call.name, "arguments": call.arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    def _verification_state_fingerprint(self, workspace_fingerprint: str) -> str:
        payload = json.dumps(
            {
                "workspace": workspace_fingerprint,
                "verification": [
                    {
                        "name": command.name,
                        "argv": list(command.argv),
                        "timeout_seconds": command.timeout_seconds,
                    }
                    for command in self.task.verification
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _record_fingerprint(self, call: ToolCall) -> bool:
        fingerprint = self._fingerprint(call)
        if fingerprint == self._state.last_fingerprint:
            self._state.repeat_count += 1
        else:
            self._state.last_fingerprint = fingerprint
            self._state.repeat_count = 1
        return self._state.repeat_count >= 3

    def _tool_definition_by_name(self, name: str):
        assert self._executor is not None
        return next(
            (
                definition
                for definition in self._provider_tool_definitions()
                if definition.name == name
            ),
            None,
        )

    def _provider_tool_definitions(self) -> tuple[ToolDefinition, ...]:
        assert self._executor is not None
        if not self._enable_subagent_dispatch:
            return self._executor.definitions
        return (*self._executor.definitions, self._dispatch_subagents_definition())

    @staticmethod
    def _dispatch_subagents_definition() -> ToolDefinition:
        return ToolDefinition(
            name="dispatch_subagents",
            description=(
                "Dispatch one or more named-role subagents in isolated looplane child workspaces. "
                "Use this for parallel investigation, staged handoff, or a child-reviewed "
                "transaction proposal. Each agent must have role scout, analyst, or reviewer, an "
                "instruction, optional allowed_paths narrowed from the parent, optional "
                "depends_on ids, and optional proposed_transaction steps. Child agents cannot "
                "modify files, run checks, recurse, or bypass parent approvals; proposed "
                "transactions are executed sequentially by the parent through tool_transaction."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agents": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 4,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "minLength": 1, "maxLength": 64},
                                "role": {
                                    "type": "string",
                                    "enum": ["scout", "analyst", "reviewer"],
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
                                "max_steps": {"type": "integer", "minimum": 1, "maximum": 6},
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
                            "required": ["id", "role", "instruction"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["agents"],
                "additionalProperties": False,
            },
            read_only=True,
        )

    def _can_execute_concurrently(self, call: ToolCall) -> bool:
        definition = self._tool_definition_by_name(call.name)
        if definition is None:
            return False
        return (
            definition.read_only
            and definition.concurrency_safe
            and effect_for_tool_definition(call.name, definition) is ToolEffect.READ
        )

    def _token_budget_error(self) -> str | None:
        max_total_tokens = self.task.limits.max_total_tokens
        if max_total_tokens is None or self._state.usage.total_tokens <= max_total_tokens:
            return None
        return (
            f"Token budget exceeded: {self._state.usage.total_tokens:,} tokens "
            f"> limit {max_total_tokens:,}."
        )

    async def _maybe_inject_context_pressure_reminder(self) -> None:
        await self._apply_context_update(
            context.context_pressure_reminder(self.task, self._context_state, self._state.usage)
        )

    async def _maybe_apply_history_summary_fallback(self) -> None:
        plan = context.plan_history_compaction(
            self.task, self._context_state, self._state.messages, self._state.usage
        )
        if plan is None:
            return
        hook_payload = plan.hook_payload()
        pre_decision = await self._run_hook(HookEventName.PRE_COMPACT, hook_payload)
        if pre_decision is not None:
            await self._event(
                "context_pressure.summary_fallback_skipped",
                reason=pre_decision.reason,
                hook=pre_decision.hook,
            )
            return
        summary = context.history_summary(self.task, self._state.messages, plan)
        self._state.messages = [
            *self._state.messages[: plan.start],
            summary,
            *self._state.messages[plan.end :],
        ]
        self._context_state.history_summary_fallback_applied = True
        await self._event(
            "context_pressure.summary_fallback_applied",
            source_start_index=plan.start,
            source_end_index=plan.end,
            retained_messages=len(self._state.messages),
        )
        await self._run_hook(
            HookEventName.POST_COMPACT,
            {
                **hook_payload,
                "summary": {
                    "source": "history_summary_fallback",
                    "content_chars": len(summary.content or ""),
                    "retained_messages": len(self._state.messages),
                },
            },
        )

    def _recent_important_paths(self, *, max_items: int = 12) -> tuple[str, ...]:
        return context.recent_important_paths(self._state.messages, max_items=max_items)

    def _check_status_lines(self) -> tuple[str, ...]:
        return context.check_status_lines(self._state.last_verification)

    def _constraint_lines(self) -> tuple[str, ...]:
        return context.constraint_lines(self.task, self._state.step)

    async def _workspace_changed_files(self, deadline: float) -> tuple[str, ...]:
        if self._executor is None:
            return ()
        try:
            review = await asyncio.to_thread(
                self._executor.reviewable_patch,
                timeout_seconds=min(5.0, self._remaining(deadline)),
            )
        except (ToolExecutionError, TimeoutError):
            return ("changed-file scan unavailable",)
        return review.changed_paths

    async def _maybe_inject_workspace_context_reminder(self, deadline: float) -> None:
        if not context.needs_workspace_reminder(self._context_state):
            return
        changed_files = await self._workspace_changed_files(deadline)
        await self._apply_context_update(
            context.workspace_context_reminder(
                self.task,
                self._context_state,
                self._state.messages,
                self._state.last_verification,
                self._state.step,
                changed_files,
            )
        )

    async def _apply_context_update(self, update: context.ContextUpdate) -> None:
        self._state.messages.extend(update.additions)
        for event in update.events:
            await self._event(event.event_type, **event.data)

    async def _maybe_inject_runtime_context_providers(self) -> None:
        update = await context.runtime_context_providers(
            self.task,
            self._context_provider_runner,
            self._run_blocking_safely,
            run_id=self.run_id,
            sequence=self._persistence.sequence,
            step=self._state.step,
        )
        await self._apply_context_update(update)

    async def _maybe_inject_ide_diagnostics(self) -> None:
        await self._apply_context_update(context.ide_diagnostics(self.task, self._context_state))

    async def _maybe_inject_ide_open_files(self) -> None:
        await self._apply_context_update(context.ide_open_files(self.task, self._context_state))

    async def _maybe_inject_instruction_reload(self) -> None:
        await self._apply_context_update(context.instruction_reload(self.task, self._context_state))

    async def _maybe_inject_project_context_reload(self) -> None:
        await self._apply_context_update(
            context.project_context_reload(self.task, self._context_state)
        )

    async def _verify_all(self, deadline: float) -> tuple[VerificationOutcome, ...]:
        assert self._executor is not None
        self._wall_time_phase = "final verification"
        self._remaining(deadline)
        verification_start_fingerprint = await self._current_verification_state_fingerprint()
        outcomes: list[VerificationOutcome] = []
        for command in self.task.verification:
            if self._cancel_requested.is_set():
                raise asyncio.CancelledError("run cancellation requested")
            cached = self._checked_workspaces.get(command.name)
            if cached is not None:
                fingerprint, prior_outcome, tool_call_id = cached
                current = await self._current_verification_state_fingerprint()
                if (
                    fingerprint == current == verification_start_fingerprint
                    and prior_outcome.ok
                    and prior_outcome.argv == command.argv
                ):
                    outcomes.append(prior_outcome)
                    await self._event(
                        "verification.reused",
                        name=command.name,
                        argv=command.argv,
                        tool_call_id=tool_call_id,
                        ok=True,
                        exit_code=prior_outcome.exit_code,
                    )
                    continue
            decision, _request_id = await self._approval(
                action_id=f"verification:{command.name}",
                effect=ToolEffect.EXECUTE,
                reason=ApprovalReason.FINAL_VERIFICATION,
                preview=f"$ {' '.join(command.argv)}",
                command=command,
            )
            if decision == ApprovalDecision.CANCEL:
                raise asyncio.CancelledError("final verification cancelled by user")
            if self._cancel_requested.is_set():
                raise asyncio.CancelledError("run cancellation requested")
            if decision == ApprovalDecision.DENY:
                outcome = VerificationOutcome(
                    name=command.name,
                    argv=command.argv,
                    ok=False,
                    output="verification denied by user",
                )
                outcomes.append(outcome)
                await self._event(
                    "verification.completed",
                    name=outcome.name,
                    argv=outcome.argv,
                    ok=False,
                    exit_code=None,
                    duration_seconds=0.0,
                    error="approval denied",
                )
                continue
            self._remaining(deadline)
            await self._event(
                "verification.started",
                name=command.name,
                argv=command.argv,
            )
            if (
                self._persistence.manifest is not None
                and self._persistence.manifest.pending_action is not None
            ):
                await self._mark_approved_action_started(
                    self._persistence.manifest.pending_action.request_id
                )
            outcome = await self._run_blocking_safely(
                self._executor.run_check,
                command.name,
                timeout_seconds=self._remaining(deadline),
            )
            outcomes.append(outcome)
            self._test_log.append(
                f"$ {' '.join(outcome.argv)}\n"
                f"exit={outcome.exit_code} duration={outcome.duration_seconds:.3f}s\n"
                f"{outcome.output}\n"
            )
            await self._event(
                "verification.completed",
                name=outcome.name,
                argv=outcome.argv,
                ok=outcome.ok,
                exit_code=outcome.exit_code,
                duration_seconds=outcome.duration_seconds,
            )
            if self._cancel_requested.is_set():
                await self._persist_verification(
                    outcomes,
                    expected_workspace_fingerprint=verification_start_fingerprint,
                )
                raise asyncio.CancelledError("run cancellation requested")
        await self._persist_verification(
            outcomes,
            expected_workspace_fingerprint=verification_start_fingerprint,
        )
        return self._state.last_verification

    async def _run_review_lane(
        self,
        *,
        patch: str,
        changed_files: tuple[str, ...],
        verification: tuple[VerificationOutcome, ...],
        deadline: float,
    ) -> str | None:
        if self._review_model is None or not patch.strip():
            return None
        await self._event(
            "role_lane.requested",
            role="reviewer",
            provider=self._review_model.provider_name,
            model=self._review_model.model_id,
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
                    f"{self.task.instruction}\n\n"
                    "Changed files:\n"
                    + "\n".join(f"- {path}" for path in changed_files)
                    + "\n\nVerification:\n"
                    + verification_summary
                    + "\n\nPatch:\n"
                    + patch,
                    self.task.limits.max_tool_output_bytes,
                ),
            ),
        )
        try:
            turn = await asyncio.wait_for(
                self._review_model.complete(messages, ()),
                timeout=self._remaining(deadline),
            )
            self._record_model_usage("reviewer", self._review_model, turn.usage)
            await self._record_provider_cache_trace("reviewer", self._review_model)
            review = bounded_text(turn.content or "", self.task.limits.max_tool_output_bytes)
            (self.run_dir / "review.md").write_text(review, encoding="utf-8")
            review_cost = self._state.model_usage[-1].cost
            await self._event(
                "role_lane.completed",
                role="reviewer",
                provider=self._review_model.provider_name,
                model=self._review_model.model_id,
                usage=turn.usage.model_dump(mode="json"),
                cost=review_cost.model_dump(mode="json") if review_cost is not None else None,
                preview=bounded_text(review, 2_000),
            )
            return review
        except (TimeoutError, ProviderError, OSError, ValueError) as exc:
            await self._event(
                "role_lane.failed",
                role="reviewer",
                provider=self._review_model.provider_name,
                model=self._review_model.model_id,
                error=bounded_text(f"{type(exc).__name__}: {exc}", 2_000),
            )
            return None

    async def _persist_verification(
        self,
        outcomes: list[VerificationOutcome],
        *,
        expected_workspace_fingerprint: str | None = None,
    ) -> None:
        self._state.last_verification = tuple(outcomes)
        workspace_changed_during_verification = False
        if (
            len(outcomes) == len(self.task.verification)
            and all(outcome.ok for outcome in outcomes)
            and self._executor is not None
        ):
            current_fingerprint = await self._current_verification_state_fingerprint()
            workspace_changed_during_verification = (
                expected_workspace_fingerprint is None
                or current_fingerprint != expected_workspace_fingerprint
            )
            self._state.verified_workspace_fingerprint = (
                None if workspace_changed_during_verification else current_fingerprint
            )
        else:
            self._state.verified_workspace_fingerprint = None
        await atomic_write_json(
            self.run_dir / "verification.json",
            [outcome.model_dump(mode="json") for outcome in outcomes],
        )
        (self.run_dir / "test.log").write_text("\n".join(self._test_log), encoding="utf-8")
        if workspace_changed_during_verification:
            raise ToolExecutionError(
                "workspace changed while final verification was running; "
                "the passing result cannot verify the resulting files"
            )

    async def _current_verification_state_fingerprint(self) -> str:
        assert self._executor is not None
        workspace_fingerprint = await self._run_blocking_safely(
            self._executor.workspace_fingerprint,
            timeout_seconds=10.0,
        )
        return self._verification_state_fingerprint(workspace_fingerprint)

    async def _capture_verified_workspace_fingerprint(self) -> None:
        self._state.verified_workspace_fingerprint = (
            await self._current_verification_state_fingerprint()
        )

    async def _complete_model_or_cancel(self, remaining: float) -> ModelTurn | None:
        """Cancel a pure model wait immediately without interrupting side-effecting tools."""

        model_task = asyncio.create_task(
            self.model.complete(self._state.messages, self._provider_tool_definitions())
        )
        cancel_task = asyncio.create_task(self._cancel_requested.wait())
        try:
            done, _ = await asyncio.wait(
                (model_task, cancel_task),
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                model_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await model_task
                raise TimeoutError("model request exceeded remaining wall time")
            if cancel_task in done and self._cancel_requested.is_set():
                model_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await model_task
                return None
            return await model_task
        finally:
            if not model_task.done():
                model_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await model_task
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task

    async def _complete_model_with_retry(self, deadline: float) -> ModelTurn | None:
        """One logical model step, retrying transient provider failures in place.

        Retryable errors (server 5xx, rate limits, transport drops) are retried up
        to ``MODEL_ATTEMPTS`` times with jittered exponential backoff; auth and
        invalid-request failures re-raise immediately. When a candidate exhausts
        its retry budget, the next fallback model (if any) takes over with a
        fresh budget. Cancellation during backoff shortens the wait, and the
        next attempt observes it immediately.
        """

        last_error: ProviderError | None = None
        for candidate_index, candidate in enumerate(self._model_candidates):
            self._active_model_index = candidate_index
            self._provider_failure_codes = []
            for attempt in range(1, MODEL_ATTEMPTS + 1):
                try:
                    return await self._complete_model_or_cancel(self._remaining(deadline))
                except ProviderError as exc:
                    if not exc.retryable:
                        raise
                    last_error = exc
                    self._provider_failure_codes.append(exc.status_code)
                    if attempt == MODEL_ATTEMPTS:
                        break
                    delay = self.model_retry_delay(attempt, exc.retry_after_seconds)
                    await self._event(
                        "model.retry",
                        attempt=attempt,
                        provider=exc.provider_name,
                        error=str(exc),
                        delay_seconds=delay,
                    )
                    await self._backoff_sleep(delay)
            if candidate_index + 1 < len(self._model_candidates):
                successor = self._model_candidates[candidate_index + 1]
                await self._event(
                    "model.fallback",
                    from_provider=candidate.provider_name,
                    from_model=candidate.model_id,
                    to_provider=successor.provider_name,
                    to_model=successor.model_id,
                    failure_codes=list(self._provider_failure_codes),
                )
                continue
            assert last_error is not None
            raise last_error
        raise AssertionError("unreachable: retry loop must return or raise")

    async def _complete_model_wind_down(self, deadline: float) -> ModelTurn | None:
        """One toolless model call for the wind-down summary.

        Similar to ``_complete_model_or_cancel`` but passes an empty tools
        list so the model can only produce a text response.  Retries once on
        transient errors; anything else is silently swallowed by the caller.
        """

        remaining = self._remaining(deadline)
        model_task = asyncio.create_task(self.model.complete(self._state.messages, tools=()))
        cancel_task = asyncio.create_task(self._cancel_requested.wait())
        try:
            done, _ = await asyncio.wait(
                (model_task, cancel_task),
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                model_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await model_task
                return None
            if cancel_task in done and self._cancel_requested.is_set():
                model_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await model_task
                return None
            return await model_task
        finally:
            if not model_task.done():
                model_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await model_task
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task

    async def _backoff_sleep(self, delay: float) -> None:
        """Wait out the retry backoff; user cancellation ends the wait early."""

        wake = asyncio.create_task(self._cancel_requested.wait())
        try:
            await asyncio.wait((wake,), timeout=delay)
        finally:
            wake.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await wake

    async def _collect_patch(
        self, timeout_seconds: float | None = None
    ) -> tuple[str, tuple[str, ...]]:
        if self._executor is None:
            patch = ""
            changed: tuple[str, ...] = ()
        else:
            review = await asyncio.to_thread(
                self._executor.reviewable_patch,
                timeout_seconds=timeout_seconds,
            )
            patch = review.content
            changed = review.changed_paths
        (self.run_dir / "changes.patch").write_text(patch, encoding="utf-8")
        return patch, changed

    async def _prepare_tool_call(
        self, call: ToolCall
    ) -> tuple[ApprovalDecision, ToolEffect, str | None]:
        if self._record_fingerprint(call):
            raise ToolExecutionError("repeated_action")
        try:
            effect = effect_for_tool_definition(call.name, self._tool_definition_by_name(call.name))
        except ValueError:
            raise ToolExecutionError(f"unknown_tool:{call.name}") from None
        await self._event(
            "tool.requested",
            tool_call_id=call.tool_call_id,
            name=call.name,
            effect=effect.value,
            arguments=self._event_arguments(call.arguments),
        )
        if call.name == "apply_patch" and self._executor is not None:
            try:
                self._executor._validate_unified_diff(call.arguments.get("patch", ""))
            except (ToolExecutionError, ValueError) as exc:
                raise ToolExecutionError(f"invalid_patch:{exc}") from exc
        hook_decision = await self._run_hook(
            HookEventName.PRE_TOOL_USE,
            {
                "tool_call": call.model_dump(mode="json"),
                "effect": effect.value,
            },
        )
        if hook_decision is not None:
            return ApprovalDecision.DENY, effect, None
        decision, request_id = await self._approval(
            action_id=call.tool_call_id,
            effect=effect,
            reason=ApprovalReason.MODEL_TOOL,
            preview=self._tool_preview(call),
            tool_call=call,
        )
        return decision, effect, request_id

    async def _execute_prepared_tool_call(
        self,
        call: ToolCall,
        *,
        effect: ToolEffect,
        request_id: str | None,
        deadline: float,
    ) -> ToolObservation:
        assert self._executor is not None
        check_start: str | None = None
        if call.name == "run_check":
            self._checked_workspaces.pop(str(call.arguments.get("name", "")), None)
            self._executor.verification_outcomes.pop(str(call.arguments.get("name", "")), None)
            with contextlib.suppress(OSError, ToolExecutionError, TimeoutError):
                check_start = await self._current_verification_state_fingerprint()
        await self._event(
            "tool.started",
            tool_call_id=call.tool_call_id,
            name=call.name,
            effect=effect.value,
        )
        if request_id is not None and self._persistence.manifest is not None:
            pending = self._persistence.manifest.pending_action
            if pending is not None and pending.request_id == request_id:
                await self._mark_approved_action_started(request_id)
        if call.name == "dispatch_subagents":
            observation = await self._execute_dispatch_subagents(call, deadline=deadline)
        else:
            observation = await self._run_blocking_safely(
                self._executor.execute,
                call,
                timeout_seconds=self._remaining(deadline),
            )
        verification_data: dict[str, Any] = {}
        if call.name == "run_check":
            outcome = self._executor.verification_outcomes.get(str(call.arguments.get("name", "")))
            if outcome is not None:
                verification_data["verification"] = outcome.model_dump(mode="json")
                verification_data["verification"]["output"] = bounded_text(outcome.output, 2_000)
        await self._event(
            "tool.completed",
            tool_call_id=call.tool_call_id,
            name=call.name,
            ok=observation.ok,
            error=observation.error,
            preview=bounded_text(observation.content, 2_000),
            **verification_data,
        )
        await self._run_hook(
            HookEventName.POST_TOOL_USE,
            {
                "tool_call": call.model_dump(mode="json"),
                "observation": observation.model_dump(mode="json"),
                "effect": effect.value,
            },
        )
        if call.name == "run_check" and observation.ok and check_start is not None:
            name = str(call.arguments.get("name", ""))
            outcome = self._executor.verification_outcomes.get(name)
            try:
                check_end = await self._current_verification_state_fingerprint()
            except (OSError, ToolExecutionError, TimeoutError):
                check_end = None
            if outcome is not None and outcome.ok and check_start == check_end:
                self._checked_workspaces[name] = (check_start, outcome, call.tool_call_id)
                self._test_log.append(
                    f"$ {shlex.join(outcome.argv)}\n"
                    f"exit={outcome.exit_code} duration={outcome.duration_seconds:.3f}s\n"
                    f"{outcome.output}\n"
                )
        return observation

    async def _execute_dispatch_subagents(
        self,
        call: ToolCall,
        *,
        deadline: float,
    ) -> ToolObservation:
        try:
            content = await self._run_dispatch_subagents(call, deadline=deadline)
            return ToolObservation(
                tool_call_id=call.tool_call_id,
                name=call.name,
                ok=True,
                content=bounded_text(content, self.task.limits.max_tool_output_bytes),
            )
        except (ValueError, TimeoutError) as exc:
            return ToolObservation(
                tool_call_id=call.tool_call_id,
                name=call.name,
                ok=False,
                error=bounded_text(f"{type(exc).__name__}: {exc}", 2_000),
            )

    async def _run_dispatch_subagents(self, call: ToolCall, *, deadline: float) -> str:
        subagents = importlib.import_module("looplane.subagents")
        ScheduledSubagent = subagents.ScheduledSubagent
        normalize_subagent_schedule = subagents.normalize_subagent_schedule
        run_subagent_task = subagents.run_subagent_task
        subagent_role_instruction = subagents.subagent_role_instruction

        scheduled = normalize_subagent_schedule(call.arguments.get("agents"))
        specs = {spec.id: spec for spec in scheduled}
        await self._event(
            "subagents.schedule_normalized",
            count=len(scheduled),
            waves=max((spec.wave for spec in scheduled), default=-1) + 1,
            agents=[
                {
                    "id": spec.id,
                    "role": spec.role.value,
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
            await self._event(
                "subagents.transaction_started",
                id=agent_id,
                tool_call_id=transaction_call.tool_call_id,
            )
            try:
                decision, effect, request_id = await self._prepare_tool_call(transaction_call)
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
                await self._event(
                    "tool.completed",
                    tool_call_id=transaction_call.tool_call_id,
                    name=transaction_call.name,
                    ok=False,
                    error=observation.error,
                )
            else:
                observation = await self._execute_prepared_tool_call(
                    transaction_call,
                    effect=effect,
                    request_id=request_id,
                    deadline=deadline,
                )
            if observation.ok:
                self._state.made_changes = True
            await self._event(
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
            role = spec.role
            instruction = spec.instruction
            dependencies = spec.depends_on
            handoff = handoff_context(dependencies, completed)
            if handoff:
                instruction = (
                    f"{subagent_role_instruction(role)}\n\n{handoff}\n\nTask: {instruction}"
                )
            else:
                instruction = f"{subagent_role_instruction(role)}\n\nTask: {instruction}"
            allowed_paths = spec.allowed_paths
            if allowed_paths is not None:
                if not isinstance(allowed_paths, Sequence) or isinstance(
                    allowed_paths, (str, bytes)
                ):
                    raise ValueError("subagent allowed_paths must be an array")
                child_allowed_paths = tuple(str(path) for path in allowed_paths)
            else:
                child_allowed_paths = self.task.allowed_paths
            child_model = (
                self._subagent_models.get(agent_id)
                or self._subagent_models.get(role.value)
                or self.model
            )
            result = await run_subagent_task(
                self.task,
                child_model,
                self.run_dir,
                instruction=instruction,
                subagent_id=agent_id,
                allowed_paths=child_allowed_paths,
                limits=self.task.limits.model_copy(update={"max_steps": spec.max_steps}),
                sandbox_checks=self._sandbox_checks,
                allow_unsafe_local_exec=False,
                approval_policy=HeadlessApprovalPolicy(
                    allow_modify=False,
                    allow_execute=False,
                ),
            )
            return agent_id, result, child_model.provider_name, child_model.model_id

        await self._event(
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
            await self._event("subagents.wave_started", ids=ready_ids)
            wave_results = await asyncio.wait_for(
                asyncio.gather(*(run_one(spec, completed) for spec in wave)),
                timeout=self._remaining(deadline),
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
            await self._event("subagents.wave_completed", ids=ready_ids)
        await self._event(
            "subagents.dispatch_completed",
            count=len(results),
            ids=[agent_id for agent_id, _result, _provider_name, _model_id in results],
        )
        lines = ["[subagents-v1]"]
        for agent_id, result, provider_name, model_id in results:
            role = specs[agent_id].role
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
                        f"role: {role.value}",
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

    async def _execute_read_only_batch(
        self,
        calls: Sequence[tuple[ToolCall, str | None]],
        *,
        deadline: float,
    ) -> list[ToolObservation]:
        if len(calls) > 1:
            await self._event(
                "tool.batch_started",
                count=len(calls),
                tool_call_ids=[call.tool_call_id for call, _request_id in calls],
                mode="read_only_parallel",
            )
        tasks = [
            asyncio.create_task(
                self._execute_prepared_tool_call(
                    call,
                    effect=ToolEffect.READ,
                    request_id=request_id,
                    deadline=deadline,
                )
            )
            for call, request_id in calls
        ]
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
                await self._event(
                    "tool.batch_completed",
                    count=len(calls),
                    tool_call_ids=[call.tool_call_id for call, _request_id in calls],
                    mode="read_only_parallel",
                )

    async def _finish(
        self,
        *,
        status: RunStatus,
        terminal_reason: str,
        summary: str,
        error: str | None = None,
        verification: tuple[VerificationOutcome, ...] | None = None,
        patch_timeout_seconds: float | None = None,
        collected_patch: tuple[str, tuple[str, ...]] | None = None,
    ) -> RunResult:
        if verification is None:
            verification = self._state.last_verification
        try:
            if collected_patch is None:
                _patch, changed_files = await self._collect_patch(patch_timeout_seconds)
            else:
                patch, changed_files = collected_patch
                (self.run_dir / "changes.patch").write_text(patch, encoding="utf-8")
        except (ToolExecutionError, TimeoutError) as exc:
            status = RunStatus.FAILED
            terminal_reason = "patch_artifact_failed"
            summary = f"{summary}\n\nFinal patch artifact refused: {exc}".strip()
            changed_files = ()
            (self.run_dir / "changes.patch").write_text("", encoding="utf-8")
        if not (self.run_dir / "test.log").exists():
            (self.run_dir / "test.log").write_text("", encoding="utf-8")
        result = RunResult(
            run_id=self.run_id,
            task_id=self.task.task_id,
            status=status,
            summary=summary,
            changed_files=changed_files,
            verification=verification,
            usage=self._state.usage,
            model_usage=tuple(self._state.model_usage),
            cost=self._aggregate_cost(),
            terminal_reason=terminal_reason,
            error=error,
            artifacts={
                "request": str(self.run_dir / "request.json"),
                "events": str(self.run_dir / "events.jsonl"),
                "checkpoint": str(self.run_dir / "checkpoint.json"),
                "patch": str(self.run_dir / "changes.patch"),
                "test_log": str(self.run_dir / "test.log"),
                "result": str(self.run_dir / "result.json"),
            }
            | (
                {"cache_traces": str(self.run_dir / "cache-traces.jsonl")}
                if (self.run_dir / "cache-traces.jsonl").is_file()
                else {}
            )
            | (
                {"review": str(self.run_dir / "review.md")}
                if (self.run_dir / "review.md").is_file()
                else {}
            ),
        )
        await self._checkpoint(status, terminal_reason=terminal_reason)
        await self._event(
            f"run.{status.value}", terminal_reason=terminal_reason, changed_files=changed_files
        )
        await atomic_write_json(self.run_dir / "result.json", result)
        if self._executor is not None:
            self._executor.close()
        return result

    async def run(self) -> RunResult:
        """Execute one bounded task through the lifecycle facade."""
        self._wall_time_phase = "task execution"
        return await self._lifecycle.run(self._run_turns)

    async def _run_turns(self) -> RunResult:
        """Coordinate turns; scheduling and completion stay here until Slices 2.5/2.6."""
        deadline = self.task.limits.wall_time_seconds
        final_summary = ""
        try:
            if not self.allow_unsafe_local_exec and not self._interactive_approvals:
                raise UnsafeLocalExecutionError(
                    "local verification executes repository code without an OS sandbox; "
                    "set allow_unsafe_local_exec=True only for a trusted repository"
                )
            if not self.model.capabilities.tool_calling:
                raise ValueError(
                    f"model {self.model.provider_name}/{self.model.model_id} does not advertise "
                    "tool calling"
                )
            if self._continuation:
                original_task = self.task
                try:
                    await self._open_continuation()
                except (SessionValidationError, SessionBusyError, OSError) as exc:
                    self._continuation_fallback_reason = str(exc)
                    self._continuation = False
                    self._resume_ready = False
                    self._is_continuation_turn = False
                    self.task = original_task
                    self._persistence.manifest = None
                    self._persistence.store = None
                    self._persistence.lease = None
                    self._state.messages = []
                    self._state.made_changes = False
                    self._run_dir_initialized = False
                    self._state.step = 0
                    self._turn_start_step = 0
                    self._state.repeat_count = 0
                    self._state.last_fingerprint = None
                    self._executor = None
                    self._rebind_run_location(uuid4().hex)
            if self._resume_ready:
                if self.task.base_sha is None or self._persistence.manifest is None:
                    raise SessionValidationError("resumed session has no pinned base commit")
                base_sha = self.task.base_sha
                final_summary = (
                    "" if self._is_continuation_turn else self._persistence.manifest.final_summary
                )
                await self._event(
                    "session.continued" if self._is_continuation_turn else "session.resumed",
                    provider=self.model.provider_name,
                    model=self.model.model_id,
                    base_sha=base_sha,
                    resumed_step=self._state.step,
                )
                await self._reconcile_interrupted_approval()
            else:
                self._validate_run_location()
                base_sha = self._resolve_base_sha(deadline)
                self.run_dir.mkdir(parents=True, exist_ok=False)
                self._run_dir_initialized = True
                effective_task = self.task.model_copy(update={"base_sha": base_sha})
                self.task = effective_task
                await self._persistence.initialize(
                    effective_task,
                    durable=self.events.durable,
                    provider_name=self.model.provider_name,
                    model_id=self.model.model_id,
                    protocol=str(self.model.protocol),
                    base_sha=base_sha,
                )
                await self._event(
                    "run.created",
                    provider=self.model.provider_name,
                    model=self.model.model_id,
                    prompt_version=CODING_AGENT_PROMPT_VERSION,
                    base_sha=base_sha,
                )
                if self._continuation_fallback_reason is not None:
                    await self._event(
                        "run.continuation_fallback",
                        reason=self._continuation_fallback_reason,
                    )
                    self._continuation_fallback_reason = None

                if self.allow_direct_repo_edit:
                    workspace_path = self.task.repository.resolve(strict=True)
                    preexisting_dirty_paths = self._preexisting_dirty_paths(deadline)
                    policy = SafePathPolicy(workspace_path, self.task.allowed_paths)
                    self._executor = ToolExecutor(
                        workspace=workspace_path,
                        policy=policy,
                        verification_commands=self.task.verification,
                        limits=self.task.limits,
                        base_sha=base_sha,
                        task_home=self.run_dir / ".check-task-env",
                        preexisting_dirty_paths=preexisting_dirty_paths,
                        mcp_servers=load_native_mcp_server_configs(self.task.repository),
                        sandbox_checks=self._sandbox_checks,
                        sandbox_profile=self._sandbox_profile,
                        sandbox_backend=self._sandbox_backend,
                        sandbox_read_roots=self._sandbox_read_roots,
                    )
                    await self._event(
                        "workspace.direct_edit_enabled",
                        repository=str(workspace_path),
                        base_sha=base_sha,
                    )
                    if preexisting_dirty_paths:
                        await self._event(
                            "workspace.dirty_source_detected",
                            status_lines=sorted(preexisting_dirty_paths),
                        )
                else:
                    workspace = LocalGitWorkspace(
                        source_repo=self.task.repository,
                        run_dir=self.run_dir,
                        base_sha=base_sha,
                    )
                    workspace_path = await self._run_blocking_safely(
                        workspace.prepare,
                        timeout_seconds=self._remaining(deadline),
                    )
                    policy = SafePathPolicy(workspace_path, self.task.allowed_paths)
                    self._executor = ToolExecutor(
                        workspace=workspace,
                        policy=policy,
                        verification_commands=self.task.verification,
                        limits=self.task.limits,
                        mcp_servers=load_native_mcp_server_configs(self.task.repository),
                        sandbox_checks=self._sandbox_checks,
                        sandbox_profile=self._sandbox_profile,
                        sandbox_backend=self._sandbox_backend,
                        sandbox_read_roots=self._sandbox_read_roots,
                    )
                await self._event("workspace.prepared", workspace="workspace", base_sha=base_sha)

                try:
                    # A fresh run starts from a trusted caller-supplied baseline.
                    # Any later drift, including a failed tool's partial side
                    # effect, must pass final verification before completion.
                    await self._capture_verified_workspace_fingerprint()
                except (OSError, ToolExecutionError, TimeoutError):
                    self._state.verified_workspace_fingerprint = None

                self._state.messages = self._initial_messages(base_sha)
                await self._checkpoint(RunStatus.INSPECTING)
                final_summary = ""

            while (self._state.step - self._turn_start_step) < self.task.limits.max_steps:
                if self._cancel_requested.is_set():
                    return await self._finish(
                        status=RunStatus.CANCELLED,
                        terminal_reason="user_cancelled",
                        summary="Run cancelled by user.",
                    )
                try:
                    self._remaining(deadline)
                except TimeoutError:
                    return await self._finish(
                        status=RunStatus.FAILED,
                        terminal_reason="wall_time_exceeded",
                        summary=final_summary,
                        patch_timeout_seconds=1.0,
                    )
                await self._maybe_inject_instruction_reload()
                await self._maybe_inject_project_context_reload()
                await self._maybe_apply_history_summary_fallback()
                await self._maybe_inject_context_pressure_reminder()
                await self._maybe_inject_workspace_context_reminder(deadline)
                await self._maybe_inject_runtime_context_providers()
                await self._maybe_inject_ide_open_files()
                await self._maybe_inject_ide_diagnostics()
                mcp_tools_changed = await asyncio.to_thread(
                    self._executor.refresh_mcp_tool_definitions
                )
                if mcp_tools_changed:
                    await self._event(
                        "mcp.tools_refreshed",
                        tools=[
                            definition.name
                            for definition in self._executor.definitions
                            if definition.name.startswith("mcp__")
                        ],
                    )
                self._state.step += 1
                await self._event("model.requested", step=self._state.step)
                self._wall_time_phase = "model request"
                turn = await self._complete_model_with_retry(deadline)
                self._wall_time_phase = "task execution"
                if turn is None:
                    return await self._finish(
                        status=RunStatus.CANCELLED,
                        terminal_reason="user_cancelled",
                        summary="Run cancelled by user while waiting for the model.",
                    )
                self._record_model_usage("primary", self.model, turn.usage)
                await self._record_provider_cache_trace("primary", self.model)
                if error := self._token_budget_error():
                    return await self._finish(
                        status=RunStatus.FAILED,
                        terminal_reason="token_budget_exceeded",
                        summary=final_summary,
                        error=error,
                        patch_timeout_seconds=1.0,
                    )
                assistant = turn.as_message()
                self._state.messages.append(assistant)
                await self._event(
                    "model.completed",
                    step=self._state.step,
                    finish_reason=turn.finish_reason,
                    tool_calls=[call.name for call in turn.tool_calls],
                    content=bounded_text(turn.content or "", 2_000),
                    usage=turn.usage.model_dump(mode="json"),
                )

                if turn.tool_calls:
                    turn_had_non_read = False
                    call_index = 0
                    while call_index < len(turn.tool_calls):
                        call = turn.tool_calls[call_index]
                        if self._cancel_requested.is_set():
                            return await self._finish(
                                status=RunStatus.CANCELLED,
                                terminal_reason="user_cancelled",
                                summary="Run cancelled by user before executing the next tool.",
                            )
                        try:
                            decision, effect, request_id = await self._prepare_tool_call(call)
                        except ToolExecutionError as exc:
                            exc_str = str(exc)
                            if exc_str == "repeated_action":
                                return await self._finish(
                                    status=RunStatus.FAILED,
                                    terminal_reason="repeated_action",
                                    summary=turn.content or final_summary,
                                )
                            if exc_str.startswith("invalid_patch:"):
                                observation = ToolObservation(
                                    tool_call_id=call.tool_call_id,
                                    name=call.name,
                                    ok=False,
                                    error=exc_str.removeprefix("invalid_patch:"),
                                )
                                self._state.messages.append(observation)
                                await self._event(
                                    "tool.completed",
                                    tool_call_id=call.tool_call_id,
                                    name=call.name,
                                    ok=False,
                                    error=observation.error,
                                )
                                call_index += 1
                                continue
                            if exc_str.startswith("unknown_tool:"):
                                unknown_name = exc_str.removeprefix("unknown_tool:")
                                available = ", ".join(
                                    sorted(t.name for t in self._provider_tool_definitions())
                                )
                                observation = ToolObservation(
                                    tool_call_id=call.tool_call_id,
                                    name=call.name,
                                    ok=False,
                                    error=(
                                        f"Unknown tool '{unknown_name}'. "
                                        f"Available tools: {available}"
                                    ),
                                )
                                self._state.messages.append(observation)
                                await self._event(
                                    "tool.completed",
                                    tool_call_id=call.tool_call_id,
                                    name=call.name,
                                    ok=False,
                                    error=observation.error,
                                )
                                call_index += 1
                                continue
                            raise
                        if effect is not ToolEffect.READ:
                            turn_had_non_read = True
                        if decision == ApprovalDecision.CANCEL:
                            return await self._finish(
                                status=RunStatus.CANCELLED,
                                terminal_reason="approval_cancelled",
                                summary="Run cancelled by user before executing a tool.",
                            )
                        if self._cancel_requested.is_set():
                            return await self._finish(
                                status=RunStatus.CANCELLED,
                                terminal_reason="user_cancelled",
                                summary="Run cancelled by user before executing a tool.",
                            )
                        if decision == ApprovalDecision.DENY:
                            observation = ToolObservation(
                                tool_call_id=call.tool_call_id,
                                name=call.name,
                                ok=False,
                                error="action denied by user",
                            )
                            self._state.messages.append(observation)
                            await self._event(
                                "tool.completed",
                                tool_call_id=call.tool_call_id,
                                name=call.name,
                                ok=False,
                                error=observation.error,
                            )
                            await self._checkpoint(
                                RunStatus.IMPLEMENTING,
                                last_tool=call.name,
                                approval="denied",
                            )
                            call_index += 1
                            continue

                        if self._can_execute_concurrently(call):
                            batch = [(call, request_id)]
                            lookahead = call_index + 1
                            deferred_denial: tuple[ToolCall, ToolObservation] | None = None
                            while lookahead < len(turn.tool_calls):
                                candidate = turn.tool_calls[lookahead]
                                if not self._can_execute_concurrently(candidate):
                                    break
                                if self._cancel_requested.is_set():
                                    return await self._finish(
                                        status=RunStatus.CANCELLED,
                                        terminal_reason="user_cancelled",
                                        summary=(
                                            "Run cancelled by user before executing the next tool."
                                        ),
                                    )
                                try:
                                    (
                                        candidate_decision,
                                        candidate_effect,
                                        candidate_request_id,
                                    ) = await self._prepare_tool_call(candidate)
                                except ToolExecutionError as exc:
                                    exc_str = str(exc)
                                    if exc_str == "repeated_action" or exc_str.startswith(
                                        "unknown_tool:"
                                    ):
                                        break
                                    raise
                                if candidate_decision == ApprovalDecision.CANCEL:
                                    return await self._finish(
                                        status=RunStatus.CANCELLED,
                                        terminal_reason="approval_cancelled",
                                        summary="Run cancelled by user before executing a tool.",
                                    )
                                if candidate_decision == ApprovalDecision.DENY:
                                    deferred_denial = (
                                        candidate,
                                        ToolObservation(
                                            tool_call_id=candidate.tool_call_id,
                                            name=candidate.name,
                                            ok=False,
                                            error="action denied by user",
                                        ),
                                    )
                                    break
                                if candidate_effect is not ToolEffect.READ:
                                    break
                                batch.append((candidate, candidate_request_id))
                                lookahead += 1
                            observations = await self._execute_read_only_batch(
                                batch,
                                deadline=deadline,
                            )
                            self._state.messages.extend(observations)
                            for completed_call, _request_id in batch:
                                await self._checkpoint(
                                    RunStatus.IMPLEMENTING,
                                    last_tool=completed_call.name,
                                )
                            processed = len(batch)
                            if deferred_denial is not None:
                                denied_call, denied_observation = deferred_denial
                                self._state.messages.append(denied_observation)
                                await self._event(
                                    "tool.completed",
                                    tool_call_id=denied_call.tool_call_id,
                                    name=denied_call.name,
                                    ok=False,
                                    error=denied_observation.error,
                                )
                                await self._checkpoint(
                                    RunStatus.IMPLEMENTING,
                                    last_tool=denied_call.name,
                                    approval="denied",
                                )
                                processed += 1
                            call_index += processed
                            if self._cancel_requested.is_set():
                                return await self._finish(
                                    status=RunStatus.CANCELLED,
                                    terminal_reason="user_cancelled",
                                    summary=(
                                        "Run cancelled by user after the current tool completed."
                                    ),
                                )
                            continue

                        observation = await self._execute_prepared_tool_call(
                            call,
                            effect=effect,
                            request_id=request_id,
                            deadline=deadline,
                        )
                        self._state.messages.append(observation)
                        if (
                            effect in {ToolEffect.MODIFY, ToolEffect.MODIFY_EXECUTE}
                            and observation.ok
                        ):
                            self._state.made_changes = True
                        await self._checkpoint(RunStatus.IMPLEMENTING, last_tool=call.name)
                        if self._cancel_requested.is_set():
                            return await self._finish(
                                status=RunStatus.CANCELLED,
                                terminal_reason="user_cancelled",
                                summary=("Run cancelled by user after the current tool completed."),
                            )
                        call_index += 1
                    # -- read-only stall guard --
                    if turn_had_non_read:
                        self._consecutive_read_only_steps = 0
                    else:
                        self._consecutive_read_only_steps += 1
                        if self._consecutive_read_only_steps >= READ_ONLY_STALL_THRESHOLD:
                            steps_left = self.task.limits.max_steps - self._state.step
                            nudge = (
                                f"Warning: You have spent {self._consecutive_read_only_steps} "
                                "consecutive steps only reading files without making any changes. "
                                f"You are running low on steps ({steps_left} remaining). "
                                "Stop exploring and start implementing the solution now. "
                                "Use create_file, replace_text, or apply_patch for the necessary "
                                "changes."
                            )
                            self._state.messages.append(Message(role="user", content=nudge))
                            await self._event(
                                "loop.read_only_stall_nudge",
                                consecutive_read_only_steps=self._consecutive_read_only_steps,
                                steps_remaining=steps_left,
                            )
                            self._consecutive_read_only_steps = 0
                    continue

                if turn.finish_reason == "length":
                    self._state.messages.append(
                        Message(
                            role="user",
                            content=(
                                "Your previous response reached the provider output limit before "
                                "a complete action or final answer. Continue concisely. Use the "
                                "available tools instead of repeating analysis."
                            ),
                        )
                    )
                    await self._checkpoint(RunStatus.PLANNING, output_truncated=True)
                    continue

                final_summary = turn.content or final_summary
                if self._persistence.manifest is not None:
                    self._persistence.manifest = self._persistence.manifest.model_copy(
                        update={"final_summary": final_summary}
                    )
                    await self._save_manifest()
                try:
                    current_fingerprint = await self._current_verification_state_fingerprint()
                except (OSError, ToolExecutionError, TimeoutError):
                    current_fingerprint = None
                self._state.made_changes = self._state.made_changes or (
                    current_fingerprint is None
                    or current_fingerprint != self._state.verified_workspace_fingerprint
                )
                if not self._state.made_changes:
                    return await self._finish(
                        status=RunStatus.COMPLETED,
                        terminal_reason="no_changes",
                        summary=final_summary,
                        verification=(),
                    )
                try:
                    outcomes = await self._verify_all(deadline)
                except asyncio.CancelledError:
                    return await self._finish(
                        status=RunStatus.CANCELLED,
                        terminal_reason="verification_cancelled",
                        summary=final_summary or "Final verification cancelled by user.",
                    )
                self._wall_time_phase = "task execution"
                if all(outcome.ok for outcome in outcomes):
                    patch, changed_files = await self._collect_patch(self._remaining(deadline))
                    review = await self._run_review_lane(
                        patch=patch,
                        changed_files=changed_files,
                        verification=outcomes,
                        deadline=deadline,
                    )
                    if review:
                        final_summary = f"{final_summary}\n\nReviewer lane:\n{review}".strip()
                    if error := self._token_budget_error():
                        return await self._finish(
                            status=RunStatus.FAILED,
                            terminal_reason="token_budget_exceeded",
                            summary=final_summary,
                            error=error,
                            verification=outcomes,
                            patch_timeout_seconds=1.0,
                        )
                    try:
                        final_fingerprint = await self._current_verification_state_fingerprint()
                    except (OSError, ToolExecutionError, TimeoutError) as exc:
                        return await self._finish(
                            status=RunStatus.FAILED,
                            terminal_reason="verification_state_unreadable",
                            summary=final_summary,
                            error=f"Could not confirm verified workspace state: {exc}",
                            verification=outcomes,
                            patch_timeout_seconds=1.0,
                        )
                    if final_fingerprint != self._state.verified_workspace_fingerprint:
                        self._state.verified_workspace_fingerprint = None
                        return await self._finish(
                            status=RunStatus.FAILED,
                            terminal_reason="workspace_changed_after_verification",
                            summary=final_summary,
                            error=(
                                "Workspace changed after final verification; "
                                "rerun checks before treating it as verified."
                            ),
                            verification=outcomes,
                            patch_timeout_seconds=1.0,
                        )
                    return await self._finish(
                        status=RunStatus.COMPLETED,
                        terminal_reason="verified",
                        summary=final_summary,
                        verification=outcomes,
                        collected_patch=(patch, changed_files),
                    )
                feedback = "\n\n".join(
                    f"Verification {outcome.name!r} failed (exit {outcome.exit_code}):\n"
                    f"{outcome.output}"
                    for outcome in outcomes
                    if not outcome.ok
                )
                self._state.messages.append(
                    Message(
                        role="user",
                        content=(
                            "The harness reran the required checks and they failed. Inspect this "
                            f"untrusted test output, fix the code, and retry:\n{feedback}"
                        ),
                    )
                )
                await self._checkpoint(RunStatus.VERIFYING, verification_passed=False)

            # -- wind-down: give the model one last toolless call to summarize --
            wind_down_message = Message(
                role="user",
                content=(
                    "You have used all available steps. Tools are now disabled. "
                    "Provide a brief text summary of what you accomplished and what remains."
                ),
            )
            self._state.messages.append(wind_down_message)
            try:
                await self._event("loop.wind_down_started", step=self._state.step)
                wind_down_turn = await self._complete_model_wind_down(deadline)
                if wind_down_turn is not None:
                    self._record_model_usage("wind_down", self.model, wind_down_turn.usage)
                    wind_down_text = wind_down_turn.content or ""
                    if wind_down_text:
                        final_summary = wind_down_text
                    self._state.messages.append(wind_down_turn.as_message())
                    await self._event(
                        "loop.wind_down_completed",
                        content=bounded_text(wind_down_text, 2_000),
                    )
            except (TimeoutError, ProviderError):
                await self._event("loop.wind_down_failed")

            return await self._finish(
                status=RunStatus.FAILED,
                terminal_reason="max_steps_exceeded",
                summary=final_summary,
            )
        except TimeoutError:
            return await self._finish(
                status=RunStatus.FAILED,
                terminal_reason="wall_time_exceeded",
                summary=self._wall_time_failure_summary(final_summary),
                patch_timeout_seconds=1.0,
            )
        except ProviderError as exc:
            await self._event(
                "model.failed",
                provider=exc.provider_name,
                kind=exc.kind.value,
                retryable=exc.retryable,
                error=str(exc),
            )
            error_text: str | None = None
            if exc.retryable:
                attempts = len(self._provider_failure_codes)
                codes = ", ".join(
                    str(code) if code is not None else "transport error"
                    for code in self._provider_failure_codes
                )
                error_text = (
                    f"{exc.provider_name} failed {attempts} consecutive model requests "
                    f"({codes}); the service is temporarily unavailable. "
                    "Retry shortly or switch to another provider/model."
                )
            else:
                error_text = f"{exc.provider_name} rejected the request ({exc.kind.value}): {exc}"
            return await self._finish(
                status=RunStatus.FAILED,
                terminal_reason=f"provider_{exc.kind.value}",
                summary=str(exc),
                error=error_text,
            )
        except Exception as exc:
            if self._run_dir_initialized:
                await self._event("run.error", error_type=type(exc).__name__, error=str(exc))
                return await self._finish(
                    status=RunStatus.FAILED,
                    terminal_reason=f"error_{type(exc).__name__}",
                    summary=str(exc),
                )
            raise
