"""Explicit, provider-neutral coding-agent loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from looplane.agent import completion, context, model_calls, subagent_dispatch, tool_scheduler
from looplane.agent.checkpoints import (
    RunPersistence,
    check_resume_identity,
    claim_session,
    session_phase,
)
from looplane.agent.model_calls import (
    MODEL_ATTEMPTS as MODEL_ATTEMPTS,
)
from looplane.agent.model_calls import (
    RETRY_BACKOFF_BASE_SECONDS as RETRY_BACKOFF_BASE_SECONDS,
)
from looplane.agent.model_calls import (
    RETRY_JITTER_FRACTION as RETRY_JITTER_FRACTION,
)
from looplane.agent.model_calls import (
    RETRY_MAX_DELAY_SECONDS as RETRY_MAX_DELAY_SECONDS,
)
from looplane.agent.model_calls import (
    RETRY_SERVER_HINT_MAX_SECONDS as RETRY_SERVER_HINT_MAX_SECONDS,
)
from looplane.agent.model_calls import (
    retry_delay_seconds as retry_delay_seconds,
)
from looplane.agent.ports import PreparedToolCall
from looplane.agent.run_lifecycle import (
    BoundedRunLifecycle,
    validate_run_id,
    validate_run_location,
)
from looplane.agent.state import ContextState, TurnState
from looplane.agent.verification import (
    VerificationCache,
    VerificationInputs,
    VerificationPorts,
    VerificationService,
)
from looplane.approvals import (
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalReason,
    ApprovalRequest,
    HeadlessApprovalPolicy,
    ToolEffect,
)
from looplane.console import CompositeEventSink, JsonlEventSink
from looplane.context_providers import (
    ContextProviderRunner,
    load_project_context_provider_runner,
)
from looplane.contracts import (
    ConversationItem,
    Message,
    ModelTurn,
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
from looplane.events import EventSink, EventWriter, atomic_write_json
from looplane.execution.capture import bounded_text
from looplane.execution.environment import sanitized_subprocess_env
from looplane.execution.local_process import run_local_process as run_bounded_command
from looplane.hooks import HookDecision, HookEventName, HookRunner, load_project_hook_runner
from looplane.mcp_client import load_native_mcp_server_configs
from looplane.models import ModelProvider, ProviderError
from looplane.permissions import PermissionGuard
from looplane.policy import SafePathPolicy
from looplane.prompts import (
    CODING_AGENT_PROMPT_VERSION,
)
from looplane.session import (
    ApprovalAuditRecord,
    SessionBusyError,
    SessionManifest,
    SessionPhase,
    SessionValidationError,
)
from looplane.tooling.executor import ToolExecutor
from looplane.tooling.types import ToolExecutionError
from looplane.workspace.local_git import LocalGitWorkspace, WorkspacePreparationError


class UnsafeLocalExecutionError(RuntimeError):
    """Raised unless the caller explicitly accepts unsandboxed repository code execution."""


BlockingResult = TypeVar("BlockingResult")
READ_ONLY_STALL_THRESHOLD = 4


class AgentRunner:
    """Run one bounded task and persist an auditable artifact bundle."""

    # Dependency seams are explicit, not an attribute proxy or mutable module registry.
    _tool_executor_factory = staticmethod(ToolExecutor)
    _workspace_factory = staticmethod(LocalGitWorkspace)
    _event_writer_factory = staticmethod(EventWriter)
    _load_hooks = staticmethod(load_project_hook_runner)
    _load_context_providers = staticmethod(load_project_context_provider_runner)
    _load_mcp_configs = staticmethod(load_native_mcp_server_configs)
    _run_command = staticmethod(run_bounded_command)
    _environment = staticmethod(sanitized_subprocess_env)
    _write_json = staticmethod(atomic_write_json)
    _claim_session = staticmethod(claim_session)
    _retry_delay = staticmethod(retry_delay_seconds)
    _new_id = staticmethod(uuid4)

    def _verification_service(self) -> VerificationService:
        return VerificationService(
            VerificationInputs(
                tuple(self.task.verification),
                self.task.instruction,
                self.task.limits.max_tool_output_bytes,
                self.run_dir,
            ),
            self._state,
            self._verification_cache,
            self._executor,
            VerificationPorts(
                blocking=self._run_blocking_safely,
                remaining=self._remaining,
                emit=self._event,
                approve=self._approval,
                mark_pending_started=self._mark_pending_verification_started,
                fingerprint=self._current_verification_state_fingerprint,
                record_usage=self._record_model_usage,
                record_cache_trace=self._record_provider_cache_trace,
                write_json=self._write_json,
            ),
            self._cancel_requested,
            self._review_model,
        )

    async def _mark_pending_verification_started(self) -> None:
        manifest = self._persistence.manifest
        if manifest is not None and manifest.pending_action is not None:
            await self._mark_approved_action_started(manifest.pending_action.request_id)

    def _result_accounting(self) -> completion.ResultAccounting:
        return completion.ResultAccounting(
            self._state.usage,
            tuple(self._state.model_usage),
            self._aggregate_cost(),
        )

    def _close_executor(self) -> None:
        if self._executor is not None:
            self._executor.close()

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
        self.model_retry_delay = self._retry_delay
        self._model_calls = model_calls.ModelCallState((model, *fallback_models))
        self._review_model = review_model
        self._sandbox_checks = sandbox_checks
        self._sandbox_profile = sandbox_profile or "verification"
        self._sandbox_backend = sandbox_backend or "auto"
        self._sandbox_read_roots = tuple(Path(root) for root in sandbox_read_roots)
        self.run_root = Path(run_root).resolve(strict=False)
        self.run_id = run_id or self._new_id().hex
        validate_run_id(self.run_id)
        self.run_dir = self.run_root / self.run_id
        self.events = self._event_writer_factory(
            self.run_dir / "events.jsonl", durable=durable_events
        )
        self.allow_unsafe_local_exec = allow_unsafe_local_exec
        self.allow_direct_repo_edit = allow_direct_repo_edit
        self.permission_guard = permission_guard
        self._hook_runner = hook_runner or self._load_hooks(self.task.repository)
        self._context_provider_runner = context_provider_runner or self._load_context_providers(
            self.task.repository
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
        self._verification_cache = VerificationCache()
        self._executor: ToolExecutor | None = None
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
        return self._model_calls.model

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
        return await tool_scheduler.run_blocking_safely(
            self._cancel_requested, function, *args, **kwargs
        )

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
        claimed = await cls._claim_session(resolved, durable=durable_events)
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
        self._executor = self._tool_executor_factory(
            workspace=workspace_path,
            policy=SafePathPolicy(workspace_path, self.task.allowed_paths),
            verification_commands=self.task.verification,
            limits=self.task.limits,
            mcp_servers=self._load_mcp_configs(self.task.repository),
            sandbox_checks=self._sandbox_checks,
            sandbox_profile=self._sandbox_profile,
            sandbox_backend=self._sandbox_backend,
            sandbox_read_roots=self._sandbox_read_roots,
        )

    def _rebind_run_location(self, run_id: str) -> None:
        """Point this runner at a different run_id/run_dir, rebuilding the event sink."""

        self.run_id = run_id
        self.run_dir = self.run_root / self.run_id
        self.events = self._event_writer_factory(
            self.run_dir / "events.jsonl", durable=self.events.durable
        )
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
        claimed = await self._claim_session(
            resolved, durable=self.events.durable, allow_terminal=True
        )
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
        return tool_scheduler.tool_preview(
            call, self._executor.verification_commands if self._executor is not None else {}
        )

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
        return model_calls.add_usage(left, right)

    def _record_model_usage(self, lane: str, model: ModelProvider, usage: Usage) -> None:
        model_calls.record_model_usage(self._state, lane, model, usage)

    async def _record_provider_cache_trace(self, lane: str, model: ModelProvider) -> None:
        await model_calls.record_provider_cache_trace(
            lane, model, step=self._state.step, run_dir=self.run_dir, emit=self._event
        )

    def _aggregate_cost(self):
        return model_calls.aggregate_cost(self._state, self.model)

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
        return tool_scheduler.event_arguments(arguments, self.task.limits.max_tool_output_bytes)

    def _validate_run_location(self) -> None:
        validate_run_location(self.task.repository, self.run_dir)

    def _resolve_base_sha(self, deadline: float) -> str:
        if self.task.base_sha is not None:
            return self.task.base_sha
        result = self._run_command(
            ("git", "rev-parse", "HEAD"),
            cwd=self.task.repository.resolve(strict=True),
            timeout_seconds=min(30.0, self._remaining(deadline)),
            max_output_chars=2_000,
            env=self._environment(),
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

        result = self._run_command(
            ("git", "status", "--porcelain=v1", "--no-renames", "-z"),
            cwd=self.task.repository.resolve(strict=True),
            timeout_seconds=min(30.0, self._remaining(deadline)),
            max_output_chars=2_000_000,
            env=self._environment(task_home=self.run_dir / ".task-env"),
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
        return tool_scheduler.fingerprint(call)

    def _verification_state_fingerprint(self, workspace_fingerprint: str) -> str:
        return self._verification_service().state_fingerprint(workspace_fingerprint)

    def _record_fingerprint(self, call: ToolCall) -> bool:
        return tool_scheduler.record_fingerprint(self._state, call)

    def _tool_definition_by_name(self, name: str) -> ToolDefinition | None:
        assert self._executor is not None
        return tool_scheduler.definition_by_name(self._provider_tool_definitions(), name)

    def _provider_tool_definitions(self) -> tuple[ToolDefinition, ...]:
        assert self._executor is not None
        if not self._enable_subagent_dispatch:
            return self._executor.definitions
        return (*self._executor.definitions, self._dispatch_subagents_definition())

    @staticmethod
    def _dispatch_subagents_definition() -> ToolDefinition:
        return subagent_dispatch.dispatch_subagents_definition()

    def _can_execute_concurrently(self, call: ToolCall) -> bool:
        return tool_scheduler.can_execute_concurrently(call, self._provider_tool_definitions())

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
        self._wall_time_phase = "final verification"
        return await self._verification_service().verify_all(deadline)

    async def _run_review_lane(
        self,
        *,
        patch: str,
        changed_files: tuple[str, ...],
        verification: tuple[VerificationOutcome, ...],
        deadline: float,
    ) -> str | None:
        return await self._verification_service().review(
            patch=patch,
            changed_files=changed_files,
            verification=verification,
            deadline=deadline,
        )

    async def _persist_verification(
        self,
        outcomes: list[VerificationOutcome],
        *,
        expected_workspace_fingerprint: str | None = None,
    ) -> None:
        await self._verification_service().persist(
            outcomes,
            expected_workspace_fingerprint=expected_workspace_fingerprint,
        )

    async def _current_verification_state_fingerprint(self) -> str:
        return await self._verification_service().current_fingerprint()

    async def _capture_verified_workspace_fingerprint(self) -> None:
        self._state.verified_workspace_fingerprint = (
            await self._current_verification_state_fingerprint()
        )

    async def _complete_model_or_cancel(self, remaining: float) -> ModelTurn | None:
        return await model_calls.complete_model_or_cancel(
            self.model,
            self._state.messages,
            self._provider_tool_definitions(),
            self._cancel_requested,
            remaining,
        )

    async def _complete_model_with_retry(self, deadline: float) -> ModelTurn | None:
        return await model_calls.complete_model_with_retry(
            self._model_calls,
            self._state,
            tool_definitions=self._provider_tool_definitions,
            cancel_requested=self._cancel_requested,
            remaining=self._remaining,
            deadline=deadline,
            emit=self._event,
            retry_delay=self.model_retry_delay,
        )

    async def _complete_model_wind_down(self, deadline: float) -> ModelTurn | None:
        return await model_calls.complete_model_wind_down(
            self.model, self._state.messages, self._cancel_requested, self._remaining(deadline)
        )

    async def _backoff_sleep(self, delay: float) -> None:
        await model_calls.backoff_sleep(self._cancel_requested, delay)

    async def _collect_patch(
        self,
        timeout_seconds: float | None = None,
    ) -> tuple[str, tuple[str, ...]]:
        return await completion.collect_patch(self.run_dir, self._executor, timeout_seconds)

    async def _prepare_tool_call(
        self, call: ToolCall
    ) -> tuple[ApprovalDecision, ToolEffect, str | None]:
        prepared = await self._prepare_scheduled_call(call)
        return prepared.decision, prepared.effect, prepared.request_id

    async def _prepare_scheduled_call(self, call: ToolCall) -> PreparedToolCall:
        assert self._executor is not None
        return await tool_scheduler.prepare_tool_call(
            call,
            state=self._state,
            definitions=self._provider_tool_definitions(),
            verification_commands=self._executor.verification_commands,
            validate_patch=self._executor._validate_unified_diff,
            max_output_bytes=self.task.limits.max_tool_output_bytes,
            emit=self._event,
            hook=self._run_hook,
            approve=self._approval,
        )

    async def _execute_prepared_tool_call(
        self,
        call: ToolCall,
        *,
        effect: ToolEffect,
        request_id: str | None,
        deadline: float,
    ) -> ToolObservation:
        assert self._executor is not None
        verification = self._verification_service()
        check = await verification.begin_manual_check(call)
        observation = await tool_scheduler.execute_prepared_tool_call(
            PreparedToolCall(call, ApprovalDecision.ALLOW_ONCE, effect, request_id),
            executor=self._executor,
            persistence=self._persistence,
            blocking=self._run_blocking_safely,
            remaining=self._remaining,
            emit=self._event,
            hook=self._run_hook,
            mark_started=self._mark_approved_action_started,
            dispatch=self._execute_dispatch_subagents,
            deadline=deadline,
        )
        await verification.finish_manual_check(check, observation, call.tool_call_id)
        return observation

    async def _execute_scheduled_call(
        self, prepared: PreparedToolCall, *, deadline: float
    ) -> ToolObservation:
        return await self._execute_prepared_tool_call(
            prepared.call, effect=prepared.effect, request_id=prepared.request_id, deadline=deadline
        )

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
        return await subagent_dispatch.run_dispatch_subagents(
            call,
            task=self.task,
            model=self.model,
            subagent_models=self._subagent_models,
            run_dir=self.run_dir,
            sandbox_checks=self._sandbox_checks,
            state=self._state,
            emit=self._event,
            remaining=self._remaining,
            prepare=self._prepare_scheduled_call,
            execute=self._execute_scheduled_call,
            runner_factory=type(self),
            deadline=deadline,
        )

    async def _execute_read_only_batch(
        self,
        calls: Sequence[tuple[ToolCall, str | None]],
        *,
        deadline: float,
    ) -> list[ToolObservation]:
        prepared = tuple(
            PreparedToolCall(call, ApprovalDecision.ALLOW_ONCE, ToolEffect.READ, request_id)
            for call, request_id in calls
        )
        return await tool_scheduler.execute_read_only_batch(
            prepared, execute=self._execute_scheduled_call, emit=self._event, deadline=deadline
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
        return await completion.finish(
            completion.CompletionInputs(
                self.run_id,
                self.task.task_id,
                self.run_dir,
                self._state.last_verification,
            ),
            completion.CompletionRequest(
                status,
                terminal_reason,
                summary,
                error,
                verification,
                patch_timeout_seconds,
                collected_patch,
            ),
            completion.CompletionPorts(
                collect_patch=self._collect_patch,
                accounting=self._result_accounting,
                checkpoint=self._checkpoint,
                emit=self._event,
                close_executor=self._close_executor,
                write_json=self._write_json,
            ),
        )

    async def run(self) -> RunResult:
        """Execute one bounded task through the lifecycle facade."""
        self._wall_time_phase = "task execution"
        return await self._lifecycle.run(self._run_turns)

    async def _run_turns(self) -> RunResult:
        """Keep engine and final-state decisions visible over explicit leaf services."""
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
                    self._rebind_run_location(self._new_id().hex)
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
                    self._executor = self._tool_executor_factory(
                        workspace=workspace_path,
                        policy=policy,
                        verification_commands=self.task.verification,
                        limits=self.task.limits,
                        base_sha=base_sha,
                        task_home=self.run_dir / ".check-task-env",
                        preexisting_dirty_paths=preexisting_dirty_paths,
                        mcp_servers=self._load_mcp_configs(self.task.repository),
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
                    workspace = self._workspace_factory(
                        source_repo=self.task.repository,
                        run_dir=self.run_dir,
                        base_sha=base_sha,
                    )
                    workspace_path = await self._run_blocking_safely(
                        workspace.prepare,
                        timeout_seconds=self._remaining(deadline),
                    )
                    policy = SafePathPolicy(workspace_path, self.task.allowed_paths)
                    self._executor = self._tool_executor_factory(
                        workspace=workspace,
                        policy=policy,
                        verification_commands=self.task.verification,
                        limits=self.task.limits,
                        mcp_servers=self._load_mcp_configs(self.task.repository),
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
                            selection = await tool_scheduler.prepare_read_only_batch(
                                PreparedToolCall(call, decision, effect, request_id),
                                turn.tool_calls[call_index + 1 :],
                                can_execute=self._can_execute_concurrently,
                                prepare=self._prepare_scheduled_call,
                                cancel_requested=self._cancel_requested,
                            )
                            if selection.cancel_reason is not None:
                                return await self._finish(
                                    status=RunStatus.CANCELLED,
                                    terminal_reason=selection.cancel_reason,
                                    summary=(
                                        "Run cancelled by user before executing the next tool."
                                        if selection.cancel_reason == "user_cancelled"
                                        else "Run cancelled by user before executing a tool."
                                    ),
                                )
                            batch = [
                                (prepared.call, prepared.request_id) for prepared in selection.calls
                            ]
                            deferred_denial = selection.deferred_denial
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
                attempts = len(self._model_calls.provider_failure_codes)
                codes = ", ".join(
                    str(code) if code is not None else "transport error"
                    for code in self._model_calls.provider_failure_codes
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
