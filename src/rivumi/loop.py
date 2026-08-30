"""Explicit, provider-neutral coding-agent loop."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import random
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any, TypeVar
from uuid import uuid4

from rivumi.approvals import (
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalReason,
    ApprovalRequest,
    HeadlessApprovalPolicy,
    ToolEffect,
    effect_for_tool_definition,
)
from rivumi.cache_strategy import ProviderCacheTrace
from rivumi.console import CompositeEventSink, EventSink, JsonlEventSink
from rivumi.context_providers import (
    ContextProviderRunner,
    load_project_context_provider_runner,
)
from rivumi.context_watch import (
    ProjectContextWatchSnapshot,
    project_context_watch_snapshot,
    render_project_context_reload,
)
from rivumi.contracts import (
    Checkpoint,
    ConversationItem,
    InjectedContext,
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
from rivumi.events import EventWriter, RunEvent, atomic_write_json
from rivumi.hooks import HookDecision, HookEventName, HookRunner, load_project_hook_runner
from rivumi.ide import (
    IdeBridgeError,
    load_project_ide_diagnostics,
    load_project_open_files,
    render_ide_diagnostics_context,
    render_ide_open_files_context,
)
from rivumi.instructions import (
    instruction_documents_fingerprint,
    render_instruction_context,
    render_instruction_diagnostics,
    resolve_instruction_documents,
)
from rivumi.mcp_client import load_native_mcp_server_configs
from rivumi.memory import relevant_memory_entries, render_known_context
from rivumi.models import ModelProvider, ProviderError
from rivumi.permissions import PermissionGuard
from rivumi.policy import SafePathPolicy
from rivumi.prompts import (
    CODING_AGENT_PROMPT_VERSION,
    CONTEXT_PRESSURE_REMINDER_VERSION,
    CONTEXT_SUMMARY_FALLBACK_VERSION,
    WORKSPACE_CONTEXT_REMINDER_VERSION,
    build_coding_agent_system_prompt,
    build_context_pressure_reminder,
    build_history_summary_fallback_message,
    build_workspace_context_reminder,
    render_interaction_prompt_context,
    render_runtime_prompt_context,
    render_subagent_planner_policy,
    render_tool_prompt_context,
    render_workspace_prompt_context,
)
from rivumi.provider_catalog import estimate_cost
from rivumi.runtime import (
    LocalGitWorkspace,
    WorkspacePreparationError,
    bounded_text,
    run_bounded_command,
    sanitized_subprocess_env,
)
from rivumi.runtime_semantics import (
    history_summary_fallback_span,
    should_apply_history_summary_fallback,
    should_inject_workspace_context_reminder,
    should_remind_context_pressure,
)
from rivumi.session import (
    ApprovalAuditRecord,
    SessionManifest,
    SessionPhase,
    SessionStore,
    SessionValidationError,
    SessionWriterLease,
)
from rivumi.skills import load_project_skills, render_skill_context, select_project_skills
from rivumi.tools import ToolExecutionError, ToolExecutor


class UnsafeLocalExecutionError(RuntimeError):
    """Raised unless the caller explicitly accepts unsandboxed repository code execution."""


BlockingResult = TypeVar("BlockingResult")
MODEL_ATTEMPTS = 5
RETRY_BACKOFF_BASE_SECONDS = 1.0
RETRY_MAX_DELAY_SECONDS = 30.0
RETRY_SERVER_HINT_MAX_SECONDS = 300.0
RETRY_JITTER_FRACTION = 0.15


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
        allow_unsafe_local_exec: bool = False,
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
        run_id_path = Path(self.run_id)
        windows_path = PureWindowsPath(self.run_id)
        if (
            not self.run_id
            or "\x00" in self.run_id
            or self.run_id in {".", ".."}
            or run_id_path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or run_id_path.name != self.run_id
        ):
            raise ValueError("run_id must be one safe relative path segment")
        self.run_dir = self.run_root / self.run_id
        self.events = EventWriter(self.run_dir / "events.jsonl", durable=durable_events)
        self.allow_unsafe_local_exec = allow_unsafe_local_exec
        self.permission_guard = permission_guard
        self._hook_runner = hook_runner or load_project_hook_runner(self.task.repository)
        self._context_provider_runner = (
            context_provider_runner
            or load_project_context_provider_runner(self.task.repository)
        )
        self._enable_subagent_dispatch = enable_subagent_dispatch
        self._subagent_models = dict(subagent_models or {})
        self.approvals = approval_policy or HeadlessApprovalPolicy(
            allow_modify=True,
            allow_execute=allow_unsafe_local_exec,
        )
        self._interactive_approvals = approval_policy is not None
        durable_sink = JsonlEventSink(self.events)
        self._event_sink: EventSink = (
            CompositeEventSink((durable_sink, event_sink)) if event_sink else durable_sink
        )
        self._run_dir_initialized = False
        self._sequence = 0
        self._writer_token = uuid4().hex
        self._messages: list[ConversationItem] = []
        self._usage = Usage()
        self._model_usage: list[ModelUsageRecord] = []
        self._step = 0
        self._last_fingerprint: str | None = None
        self._repeat_count = 0
        self._made_changes = False
        self._test_log: list[str] = []
        self._executor: ToolExecutor | None = None
        self._last_verification: tuple[VerificationOutcome, ...] = ()
        self._active_wall_time_base = 0.0
        self._run_started_monotonic: float | None = None
        self._active_started_at: datetime | None = None
        self._session_store: SessionStore | None = None
        self._session_lease: SessionWriterLease | None = None
        self._manifest: SessionManifest | None = None
        self._resume_ready = False
        self._context_pressure_reminder_sent = False
        self._history_summary_fallback_applied = False
        self._workspace_context_reminder_sent = False
        self._last_ide_diagnostics_fingerprint: str | None = None
        self._last_ide_open_files_fingerprint: str | None = None
        self._last_instruction_fingerprint: str | None = None
        self._last_project_context_watch: ProjectContextWatchSnapshot | None = None
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
        store = SessionStore(resolved, durable=durable_events)
        lease = store.acquire_writer()
        try:
            manifest, task = await store.claim_and_validate_resume(lease)
            if (
                manifest.provider_name != model.provider_name
                or manifest.model_id != model.model_id
                or manifest.protocol != str(model.protocol)
            ):
                raise SessionValidationError(
                    "resume provider/protocol/model must match the persisted session"
                )
            runner = cls(
                task,
                model,
                resolved.parent,
                run_id=resolved.name,
                durable_events=durable_events,
                approval_policy=approval_policy,
                event_sink=event_sink,
            )
            runner._session_store = store
            runner._session_lease = lease
            runner._manifest = manifest
            runner._sequence = manifest.last_event_sequence + 1
            runner._messages = list(manifest.messages)
            runner._usage = manifest.usage
            runner._model_usage = list(manifest.model_usage)
            runner._step = manifest.step
            runner._last_fingerprint = manifest.last_action_fingerprint
            runner._last_verification = manifest.verification
            runner._context_pressure_reminder_sent = any(
                isinstance(item, InjectedContext)
                and item.source == "context_pressure"
                and item.content is not None
                and CONTEXT_PRESSURE_REMINDER_VERSION in item.content
                for item in runner._messages
            )
            runner._history_summary_fallback_applied = any(
                isinstance(item, InjectedContext)
                and item.source == "history_summary_fallback"
                and item.content is not None
                and CONTEXT_SUMMARY_FALLBACK_VERSION in item.content
                for item in runner._messages
            )
            runner._workspace_context_reminder_sent = any(
                isinstance(item, InjectedContext)
                and item.source == "workspace_context_reminder"
                and item.content is not None
                and WORKSPACE_CONTEXT_REMINDER_VERSION in item.content
                for item in runner._messages
            )
            # A resumed workspace may already contain modifications from before the
            # interruption, so keep the final-verification gate conservatively armed.
            runner._made_changes = True
            runner._run_dir_initialized = True
            workspace = resolved / "workspace"
            runner._executor = ToolExecutor(
                workspace=workspace,
                policy=SafePathPolicy(workspace, task.allowed_paths),
                verification_commands=task.verification,
                limits=task.limits,
                mcp_servers=load_native_mcp_server_configs(task.repository),
                sandbox_checks=runner._sandbox_checks,
                sandbox_profile=runner._sandbox_profile,
                sandbox_backend=runner._sandbox_backend,
                sandbox_read_roots=runner._sandbox_read_roots,
            )
            runner._resume_ready = True
            return runner
        except BaseException:
            lease.release()
            raise

    async def _event(self, event_type: str, **data: Any) -> None:
        event = RunEvent(
            event_type=event_type,
            run_id=self.run_id,
            task_id=self.task.task_id,
            sequence=self._sequence,
            data=data,
        )
        if self._manifest is not None:
            self._manifest = self._manifest.model_copy(
                update={
                    "last_event_sequence": event.sequence,
                    "step": self._step,
                    "messages": tuple(self._messages),
                    "usage": self._usage,
                    "model_usage": tuple(self._model_usage),
                    "last_action_fingerprint": self._last_fingerprint,
                    "repeat_count": self._repeat_count,
                    "verification": self._last_verification,
                    "active_wall_time_seconds": self._active_wall_time_base,
                    "active_started_at": self._active_started_at,
                }
            )
            await self._save_manifest()
        await self._event_sink.emit(event)
        self._sequence += 1

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
                    "sequence": self._sequence,
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
        if self._session_store is None or self._session_lease is None or self._manifest is None:
            return
        self._manifest = await self._session_store.save(self._manifest, self._session_lease)

    @staticmethod
    def _session_phase(status: RunStatus) -> SessionPhase:
        return {
            RunStatus.CREATED: SessionPhase.CREATED,
            RunStatus.PREPARING: SessionPhase.PREPARING,
            RunStatus.INSPECTING: SessionPhase.RUNNING,
            RunStatus.PLANNING: SessionPhase.RUNNING,
            RunStatus.IMPLEMENTING: SessionPhase.RUNNING,
            RunStatus.VERIFYING: SessionPhase.VERIFYING,
            RunStatus.COMPLETED: SessionPhase.COMPLETED,
            RunStatus.FAILED: SessionPhase.FAILED,
            RunStatus.CANCELLED: SessionPhase.CANCELLED,
        }[status]

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
            policy_reason = self.permission_guard.approval_policy_reason(
                request, guard_subjects
            )
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
            and self._manifest is not None
            and effect in self._manifest.granted_effects
        ):
            self._manifest = self._manifest.model_copy(
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
        if self._manifest is not None:
            self._manifest = self._manifest.model_copy(
                update={
                    "phase": SessionPhase.WAITING_APPROVAL,
                    "pending_action": request,
                }
            )
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
        decision = (
            pre_decision
            if pre_decision is not None
            else await self.approvals.decide(request)
        )
        if self._manifest is not None:
            granted_effects = self._manifest.granted_effects
            if decision == ApprovalDecision.ALLOW_SESSION:
                granted_effects = frozenset((*granted_effects, effect))
            pending_action = request if effect is not ToolEffect.READ else None
            if decision in {ApprovalDecision.DENY, ApprovalDecision.CANCEL}:
                pending_action = None
            self._manifest = self._manifest.model_copy(
                update={
                    "phase": SessionPhase.RUNNING,
                    "pending_action": pending_action,
                    "granted_effects": granted_effects,
                    "approval_history": (
                        *self._manifest.approval_history,
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

        if self._manifest is None:
            return
        pending = self._manifest.pending_action
        if pending is None or pending.request_id != request_id:
            raise SessionValidationError("approved action no longer matches session state")
        self._manifest = self._manifest.model_copy(update={"pending_action": None})
        await self._save_manifest()

    async def _reconcile_interrupted_approval(self) -> None:
        """Fail closed when a process stopped after requesting but before resolving approval.

        No side effect has started at this point.  Resume records that fact and gives the model a
        canonical failure/user message so it can request the action again.  This avoids both
        silently executing a stale approval and sending an orphaned tool call to the provider.
        """

        if self._manifest is None or self._manifest.pending_action is None:
            return
        pending = self._manifest.pending_action
        if pending.tool_call is not None:
            self._messages.append(
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
            self._messages.append(
                Message(
                    role="user",
                    content=(
                        "Final verification approval was interrupted before the command ran. "
                        "Continue the task and finish again when ready; verification will require "
                        "a new approval."
                    ),
                )
            )
        self._manifest = self._manifest.model_copy(
            update={
                "phase": SessionPhase.RUNNING,
                "pending_action": None,
                "messages": tuple(self._messages),
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

    @staticmethod
    def _tool_preview(call: ToolCall) -> str:
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
        checkpoint = Checkpoint(
            run_id=self.run_id,
            task_id=self.task.task_id,
            status=status,
            step=self._step,
            messages=tuple(self._messages),
            tool_call_count=sum(
                len(item.tool_calls) for item in self._messages if isinstance(item, Message)
            ),
            usage=self._usage,
            active_writer_token=self._writer_token,
            last_action_fingerprint=self._last_fingerprint,
            metadata=metadata,
        )
        await atomic_write_json(self.run_dir / "checkpoint.json", checkpoint)
        if self._manifest is not None:
            phase = self._session_phase(status)
            self._manifest = self._manifest.model_copy(
                update={
                    "phase": phase,
                    "terminal": phase
                    in {SessionPhase.COMPLETED, SessionPhase.FAILED, SessionPhase.CANCELLED},
                    "step": self._step,
                    "messages": tuple(self._messages),
                    "usage": self._usage,
                    "model_usage": tuple(self._model_usage),
                    "last_action_fingerprint": self._last_fingerprint,
                    "repeat_count": self._repeat_count,
                    "verification": self._last_verification,
                }
            )
            await self._save_manifest()

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
        self._usage = self._add_usage(self._usage, usage)
        self._model_usage.append(
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
            "step": self._step,
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
            step=self._step,
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
        if not self._model_usage:
            return estimate_cost(self.model.provider_name, self.model.model_id, self._usage)
        providers = {(record.provider, record.model) for record in self._model_usage}
        if len(providers) != 1:
            return None
        provider, model = next(iter(providers))
        return estimate_cost(provider, model, self._usage)

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("task wall-time budget exhausted")
        return remaining

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
        source = self.task.repository.resolve(strict=True)
        candidate = self.run_dir.resolve(strict=False)
        try:
            candidate.relative_to(source)
        except ValueError:
            return
        raise WorkspacePreparationError("run directory must not be inside the source repository")

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

    def _initial_git_status(self) -> tuple[str, ...]:
        if self._executor is None:
            return ("unavailable: workspace executor is not initialized",)
        result = run_bounded_command(
            ("git", "status", "--short", "--branch"),
            cwd=self._executor.workspace,
            timeout_seconds=10.0,
            max_output_chars=4_000,
            env=sanitized_subprocess_env(task_home=self._executor.workspace.parent / ".task-env"),
        )
        if not result.ok:
            reason = result.stderr.strip() or result.stdout.strip() or "git status failed"
            return (f"unavailable: {reason}",)
        lines = tuple(line for line in result.stdout.splitlines() if line.strip())
        if result.stdout_truncated:
            return (*lines, "... truncated")
        return lines

    def _initial_messages(self, base_sha: str) -> list[ConversationItem]:
        checks = "\n".join(
            f"- {command.name}: {list(command.argv)!r}" for command in self.task.verification
        )
        paths = "\n".join(f"- {pattern}" for pattern in self.task.allowed_paths)
        known_context = render_known_context(
            relevant_memory_entries(project=self.task.repository)
        )
        instruction_resolution = resolve_instruction_documents(
            project_root=self.task.repository,
            start_dir=Path.cwd(),
        )
        instruction_documents = instruction_resolution.documents
        self._last_instruction_fingerprint = instruction_documents_fingerprint(
            instruction_documents
        )
        instruction_context = render_instruction_context(instruction_documents)
        instruction_diagnostics = render_instruction_diagnostics(
            instruction_resolution.diagnostics
        )
        if instruction_diagnostics:
            known_context = (
                f"{known_context}\n\n{instruction_diagnostics}"
                if known_context
                else instruction_diagnostics
            )
        skills = select_project_skills(
            load_project_skills(self.task.repository),
            self.task.enabled_skills,
        )
        skill_context = render_skill_context(skills)
        provider_tools = self._provider_tool_definitions() if self._executor is not None else ()
        tool_context_parts = [render_tool_prompt_context(provider_tools)]
        if self._enable_subagent_dispatch:
            tool_context_parts.append(render_subagent_planner_policy())
        tool_context = "\n\n".join(part for part in tool_context_parts if part)
        workspace_context = render_workspace_prompt_context(
            base_sha=base_sha,
            allowed_paths=self.task.allowed_paths,
            verification=self.task.verification,
            git_status=self._initial_git_status(),
        )
        interaction_context = render_interaction_prompt_context()
        runtime_context = render_runtime_prompt_context(
            {
                "mode": "native_loop",
                "sandbox_backend": self._sandbox_backend,
                "sandbox_checks": self._sandbox_checks,
                "sandbox_profile": self._sandbox_profile,
                "max_steps": self.task.limits.max_steps,
                "max_tool_output_bytes": self.task.limits.max_tool_output_bytes,
            }
        )
        self._last_project_context_watch = project_context_watch_snapshot(
            self.task.repository,
            start_dir=Path.cwd(),
        )
        request = (
            f"Task: {self.task.instruction}\n"
            f"Base commit: {base_sha}\n"
            f"Allowed paths:\n{paths}\n"
            f"Required verification:\n{checks}\n"
            "Inspect the repository, make the smallest correct patch, and verify it."
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

    @staticmethod
    def _fingerprint(call: ToolCall) -> str:
        payload = json.dumps(
            {"name": call.name, "arguments": call.arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    def _record_fingerprint(self, call: ToolCall) -> bool:
        fingerprint = self._fingerprint(call)
        if fingerprint == self._last_fingerprint:
            self._repeat_count += 1
        else:
            self._last_fingerprint = fingerprint
            self._repeat_count = 1
        return self._repeat_count >= 3

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
                "Dispatch one or more named-role subagents in isolated Rivumi child workspaces. "
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
        if max_total_tokens is None or self._usage.total_tokens <= max_total_tokens:
            return None
        return (
            f"Token budget exceeded: {self._usage.total_tokens:,} tokens "
            f"> limit {max_total_tokens:,}."
        )

    async def _maybe_inject_context_pressure_reminder(self) -> None:
        max_total_tokens = self.task.limits.max_total_tokens
        if self._context_pressure_reminder_sent or max_total_tokens is None:
            return
        if not should_remind_context_pressure(
            total_tokens=self._usage.total_tokens,
            max_total_tokens=max_total_tokens,
        ):
            return
        self._messages.append(
            InjectedContext(
                source="context_pressure",
                content=build_context_pressure_reminder(
                    total_tokens=self._usage.total_tokens,
                    max_total_tokens=max_total_tokens,
                ),
            )
        )
        self._context_pressure_reminder_sent = True
        await self._event(
            "context_pressure.reminder_injected",
            total_tokens=self._usage.total_tokens,
            max_total_tokens=max_total_tokens,
        )

    async def _maybe_apply_history_summary_fallback(self) -> None:
        max_total_tokens = self.task.limits.max_total_tokens
        if not should_apply_history_summary_fallback(
            total_tokens=self._usage.total_tokens,
            max_total_tokens=max_total_tokens,
            message_count=len(self._messages),
            already_applied=self._history_summary_fallback_applied,
        ):
            return

        span = history_summary_fallback_span(message_count=len(self._messages))
        if span is None:
            return
        start, end = span
        while end < len(self._messages) and isinstance(self._messages[end], ToolObservation):
            end += 1
        if end - start < 2:
            return

        hook_payload = {
            "compaction": {
                "kind": "history_summary_fallback",
                "source_start_index": start,
                "source_end_index": end,
                "source_message_count": end - start,
                "message_count": len(self._messages),
                "total_tokens": self._usage.total_tokens,
                "max_total_tokens": max_total_tokens,
            }
        }
        pre_decision = await self._run_hook(HookEventName.PRE_COMPACT, hook_payload)
        if pre_decision is not None:
            await self._event(
                "context_pressure.summary_fallback_skipped",
                reason=pre_decision.reason,
                hook=pre_decision.hook,
            )
            return

        summary = build_history_summary_fallback_message(
            self._messages[start:end],
            source_start_index=start,
            source_end_index=end,
            max_chars=max(512, min(self.task.limits.max_tool_output_bytes, 12_000)),
        )
        self._messages = [
            *self._messages[:start],
            InjectedContext(source="history_summary_fallback", content=summary.content or ""),
            *self._messages[end:],
        ]
        self._history_summary_fallback_applied = True
        await self._event(
            "context_pressure.summary_fallback_applied",
            source_start_index=start,
            source_end_index=end,
            retained_messages=len(self._messages),
        )
        await self._run_hook(
            HookEventName.POST_COMPACT,
            {
                **hook_payload,
                "summary": {
                    "source": "history_summary_fallback",
                    "content_chars": len(summary.content or ""),
                    "retained_messages": len(self._messages),
                },
            },
        )

    def _recent_important_paths(self, *, max_items: int = 12) -> tuple[str, ...]:
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

        for item in reversed(self._messages):
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

    def _check_status_lines(self) -> tuple[str, ...]:
        if not self._last_verification:
            return ()
        return tuple(
            f"{outcome.name}: {'passed' if outcome.ok else 'failed'}"
            + (
                f" (exit {outcome.exit_code})"
                if outcome.exit_code is not None
                else ""
            )
            for outcome in self._last_verification
        )

    def _constraint_lines(self) -> tuple[str, ...]:
        verification = "; ".join(
            f"{command.name}={list(command.argv)!r}" for command in self.task.verification
        )
        token_limit = (
            f"max_total_tokens={self.task.limits.max_total_tokens}"
            if self.task.limits.max_total_tokens is not None
            else "max_total_tokens=unbounded"
        )
        remaining_steps = max(0, self.task.limits.max_steps - self._step)
        return (
            "allowed_paths=" + ", ".join(self.task.allowed_paths),
            "verification=" + verification,
            f"remaining_steps_before_next_request={remaining_steps}",
            token_limit,
        )

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
        if not should_inject_workspace_context_reminder(
            compacted_context=self._history_summary_fallback_applied,
            already_injected=self._workspace_context_reminder_sent,
        ):
            return
        changed_files = await self._workspace_changed_files(deadline)
        recent_paths = tuple(
            dict.fromkeys((*changed_files, *self._recent_important_paths())).keys()
        )
        self._messages.append(
            InjectedContext(
                source="workspace_context_reminder",
                content=build_workspace_context_reminder(
                    changed_files=changed_files,
                    check_status=self._check_status_lines(),
                    recent_paths=recent_paths,
                    constraints=self._constraint_lines(),
                    max_chars=max(512, min(self.task.limits.max_tool_output_bytes, 4_000)),
                ).content
                or "",
            )
        )
        self._workspace_context_reminder_sent = True
        await self._event(
            "context_pressure.workspace_reminder_injected",
            changed_files=changed_files,
            recent_paths=recent_paths,
        )

    async def _maybe_inject_runtime_context_providers(self) -> None:
        if not self._context_provider_runner.enabled:
            return
        try:
            items = await self._run_blocking_safely(
                self._context_provider_runner.collect,
                {
                    "run_id": self.run_id,
                    "task_id": self.task.task_id,
                    "sequence": self._sequence,
                    "step": self._step,
                    "repository": str(self.task.repository),
                },
            )
        except Exception as exc:
            await self._event(
                "context_provider.failed",
                error=bounded_text(f"{type(exc).__name__}: {exc}", 2_000),
            )
            return
        for item in items:
            self._messages.append(
                InjectedContext(
                    source=bounded_text(f"context_provider:{item.source}", 128).rstrip(),
                    content=item.content,
                )
            )
        if items:
            await self._event(
                "context_provider.injected",
                sources=[item.source for item in items],
                items=len(items),
            )

    async def _maybe_inject_ide_diagnostics(self) -> None:
        try:
            snapshot = load_project_ide_diagnostics(self.task.repository)
        except IdeBridgeError as exc:
            await self._event("ide.diagnostics_ignored", error=str(exc))
            return
        if snapshot is None:
            return
        fingerprint = snapshot.fingerprint
        if fingerprint == self._last_ide_diagnostics_fingerprint:
            return
        content = render_ide_diagnostics_context(
            snapshot,
            project_root=self.task.repository,
        )
        if not content:
            self._last_ide_diagnostics_fingerprint = fingerprint
            return
        self._messages.append(InjectedContext(source="ide_diagnostics", content=content))
        self._last_ide_diagnostics_fingerprint = fingerprint
        paths = tuple(dict.fromkeys(diagnostic.path for diagnostic in snapshot.diagnostics))
        await self._event(
            "ide.diagnostics_injected",
            diagnostic_count=len(snapshot.diagnostics),
            paths=paths[:20],
        )

    async def _maybe_inject_ide_open_files(self) -> None:
        try:
            snapshot = load_project_open_files(self.task.repository)
        except IdeBridgeError as exc:
            await self._event("ide.open_files_ignored", error=str(exc))
            return
        if snapshot is None:
            return
        fingerprint = snapshot.fingerprint
        if fingerprint == self._last_ide_open_files_fingerprint:
            return
        content = render_ide_open_files_context(
            snapshot,
            project_root=self.task.repository,
        )
        if not content:
            self._last_ide_open_files_fingerprint = fingerprint
            return
        self._messages.append(InjectedContext(source="ide_open_files", content=content))
        self._last_ide_open_files_fingerprint = fingerprint
        await self._event(
            "ide.open_files_injected",
            file_count=len(snapshot.files),
            paths=[file.path for file in snapshot.files[:20]],
        )

    async def _maybe_inject_instruction_reload(self) -> None:
        try:
            resolution = resolve_instruction_documents(
                project_root=self.task.repository,
                start_dir=Path.cwd(),
            )
        except ValueError as exc:
            await self._event("instructions.reload_ignored", error=str(exc))
            return
        documents = resolution.documents
        fingerprint = instruction_documents_fingerprint(documents)
        if fingerprint == self._last_instruction_fingerprint:
            return
        self._last_instruction_fingerprint = fingerprint
        content = render_instruction_context(documents)
        diagnostics = render_instruction_diagnostics(resolution.diagnostics)
        if not content:
            content = "No configured user/project instruction files are currently loaded."
        if diagnostics:
            content = f"{content}\n\n{diagnostics}"
        self._messages.append(
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
        await self._event(
            "instructions.reloaded",
            document_count=len(documents),
            sources=[document.source for document in documents],
            source_priority=[
                {
                    "source": diagnostic.source,
                    "scope": diagnostic.scope,
                    "status": diagnostic.status,
                    "reason": diagnostic.reason,
                }
                for diagnostic in resolution.diagnostics
            ],
        )

    async def _maybe_inject_project_context_reload(self) -> None:
        previous = self._last_project_context_watch
        if previous is None:
            self._last_project_context_watch = project_context_watch_snapshot(
                self.task.repository,
                start_dir=Path.cwd(),
            )
            return
        try:
            current = project_context_watch_snapshot(
                self.task.repository,
                start_dir=Path.cwd(),
            )
        except ValueError as exc:
            await self._event("project_context.reload_ignored", error=str(exc))
            return
        changed = current.changed_categories(previous)
        if not changed:
            return
        self._last_project_context_watch = current
        non_instruction_changes = tuple(
            category for category in changed if category != "instructions"
        )
        if not non_instruction_changes:
            return
        content = render_project_context_reload(
            previous,
            current,
            categories=non_instruction_changes,
        )
        if {"skills", "plugins"} & set(non_instruction_changes):
            skills = select_project_skills(
                load_project_skills(self.task.repository),
                self.task.enabled_skills,
            )
            skill_context = render_skill_context(skills)
            if skill_context:
                content = f"{content}\n\nReloaded project skill context:\n{skill_context}"
        if content:
            self._messages.append(
                InjectedContext(source="project_context_reload", content=content)
            )
        await self._event(
            "project_context.reloaded",
            categories=list(non_instruction_changes),
            sources={
                category: list(current.sources.get(category, ()))
                for category in non_instruction_changes
            },
        )

    async def _verify_all(self, deadline: float) -> tuple[VerificationOutcome, ...]:
        assert self._executor is not None
        outcomes: list[VerificationOutcome] = []
        for command in self.task.verification:
            if self._cancel_requested.is_set():
                raise asyncio.CancelledError("run cancellation requested")
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
                    ok=False,
                    exit_code=None,
                    duration_seconds=0.0,
                    error="approval denied",
                )
                continue
            await self._event(
                "verification.started",
                name=command.name,
                argv=command.argv,
            )
            if self._manifest is not None and self._manifest.pending_action is not None:
                await self._mark_approved_action_started(
                    self._manifest.pending_action.request_id
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
                ok=outcome.ok,
                exit_code=outcome.exit_code,
                duration_seconds=outcome.duration_seconds,
            )
            if self._cancel_requested.is_set():
                await self._persist_verification(outcomes)
                raise asyncio.CancelledError("run cancellation requested")
        await self._persist_verification(outcomes)
        return self._last_verification

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
            f"- {outcome.name}: {'passed' if outcome.ok else 'failed'}"
            f" (exit {outcome.exit_code})"
            for outcome in verification
        )
        messages = (
            Message(
                role="system",
                content=(
                    "You are Rivumi's read-only reviewer lane. Review the verified patch for "
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
            review_cost = self._model_usage[-1].cost
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
        self, outcomes: list[VerificationOutcome]
    ) -> None:
        self._last_verification = tuple(outcomes)
        await atomic_write_json(
            self.run_dir / "verification.json",
            [outcome.model_dump(mode="json") for outcome in outcomes],
        )
        (self.run_dir / "test.log").write_text("\n".join(self._test_log), encoding="utf-8")

    async def _complete_model_or_cancel(self, remaining: float) -> ModelTurn | None:
        """Cancel a pure model wait immediately without interrupting side-effecting tools."""

        model_task = asyncio.create_task(
            self.model.complete(self._messages, self._provider_tool_definitions())
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
        effect = effect_for_tool_definition(call.name, self._tool_definition_by_name(call.name))
        await self._event(
            "tool.requested",
            tool_call_id=call.tool_call_id,
            name=call.name,
            effect=effect.value,
            arguments=self._event_arguments(call.arguments),
        )
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
        await self._event(
            "tool.started",
            tool_call_id=call.tool_call_id,
            name=call.name,
            effect=effect.value,
        )
        if request_id is not None and self._manifest is not None:
            pending = self._manifest.pending_action
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
        await self._event(
            "tool.completed",
            tool_call_id=call.tool_call_id,
            name=call.name,
            ok=observation.ok,
            error=observation.error,
            preview=bounded_text(observation.content, 2_000),
        )
        await self._run_hook(
            HookEventName.POST_TOOL_USE,
            {
                "tool_call": call.model_dump(mode="json"),
                "observation": observation.model_dump(mode="json"),
                "effect": effect.value,
            },
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
        from rivumi.subagents import (
            ScheduledSubagent,
            normalize_subagent_schedule,
            run_subagent_task,
            subagent_role_instruction,
        )

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
                self._made_changes = True
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
        verification: tuple[VerificationOutcome, ...] = (),
        patch_timeout_seconds: float | None = None,
    ) -> RunResult:
        verification = verification or self._last_verification
        try:
            patch, changed_files = await self._collect_patch(patch_timeout_seconds)
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
            usage=self._usage,
            model_usage=tuple(self._model_usage),
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
        """Execute until verified success or a deterministic terminal guard fires."""

        self._active_wall_time_base = (
            self._manifest.active_wall_time_seconds if self._manifest is not None else 0.0
        )
        now = datetime.now(UTC)
        if self._manifest is not None and self._manifest.active_started_at is not None:
            self._active_wall_time_base += max(
                0.0,
                (now - self._manifest.active_started_at).total_seconds(),
            )
        self._run_started_monotonic = time.monotonic()
        self._active_started_at = now
        if self._manifest is not None:
            self._manifest = self._manifest.model_copy(
                update={
                    "active_wall_time_seconds": self._active_wall_time_base,
                    "active_started_at": self._active_started_at,
                }
            )
            await self._save_manifest()
        remaining_wall_time = max(
            0.0,
            self.task.limits.wall_time_seconds - self._active_wall_time_base,
        )
        deadline = self._run_started_monotonic + remaining_wall_time
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
            if self._resume_ready:
                if self.task.base_sha is None or self._manifest is None:
                    raise SessionValidationError("resumed session has no pinned base commit")
                base_sha = self.task.base_sha
                final_summary = self._manifest.final_summary
                await self._event(
                    "session.resumed",
                    provider=self.model.provider_name,
                    model=self.model.model_id,
                    base_sha=base_sha,
                    resumed_step=self._step,
                )
                await self._reconcile_interrupted_approval()
            else:
                self._validate_run_location()
                base_sha = self._resolve_base_sha(deadline)
                self.run_dir.mkdir(parents=True, exist_ok=False)
                self._run_dir_initialized = True
                self._session_store = SessionStore(
                    self.run_dir,
                    durable=self.events.durable,
                )
                self._session_lease = self._session_store.acquire_writer()
                effective_task = self.task.model_copy(update={"base_sha": base_sha})
                self.task = effective_task
                await atomic_write_json(self.run_dir / "request.json", effective_task)
                self._manifest = await self._session_store.initialize(
                    SessionManifest.new(
                        run_id=self.run_id,
                        task_id=self.task.task_id,
                        provider_name=self.model.provider_name,
                        model_id=self.model.model_id,
                        protocol=str(self.model.protocol),
                        base_sha=base_sha,
                    ),
                    self._session_lease,
                )
                await self._event(
                    "run.created",
                    provider=self.model.provider_name,
                    model=self.model.model_id,
                    prompt_version=CODING_AGENT_PROMPT_VERSION,
                    base_sha=base_sha,
                )

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

                self._messages = self._initial_messages(base_sha)
                await self._checkpoint(RunStatus.INSPECTING)
                final_summary = ""

            while self._step < self.task.limits.max_steps:
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
                self._step += 1
                await self._event("model.requested", step=self._step)
                turn = await self._complete_model_with_retry(deadline)
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
                self._messages.append(assistant)
                await self._event(
                    "model.completed",
                    step=self._step,
                    finish_reason=turn.finish_reason,
                    tool_calls=[call.name for call in turn.tool_calls],
                    content=bounded_text(turn.content or "", 2_000),
                    usage=turn.usage.model_dump(mode="json"),
                )

                if turn.tool_calls:
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
                            if str(exc) == "repeated_action":
                                return await self._finish(
                                    status=RunStatus.FAILED,
                                    terminal_reason="repeated_action",
                                    summary=turn.content or final_summary,
                                )
                            raise
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
                            self._messages.append(observation)
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
                                    ) = (
                                        await self._prepare_tool_call(candidate)
                                    )
                                except ToolExecutionError as exc:
                                    if str(exc) == "repeated_action":
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
                            self._messages.extend(observations)
                            for completed_call, _request_id in batch:
                                await self._checkpoint(
                                    RunStatus.IMPLEMENTING,
                                    last_tool=completed_call.name,
                                )
                            processed = len(batch)
                            if deferred_denial is not None:
                                denied_call, denied_observation = deferred_denial
                                self._messages.append(denied_observation)
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
                        self._messages.append(observation)
                        if (
                            effect in {ToolEffect.MODIFY, ToolEffect.MODIFY_EXECUTE}
                            and observation.ok
                        ):
                            self._made_changes = True
                        await self._checkpoint(RunStatus.IMPLEMENTING, last_tool=call.name)
                        if self._cancel_requested.is_set():
                            return await self._finish(
                                status=RunStatus.CANCELLED,
                                terminal_reason="user_cancelled",
                                summary=(
                                    "Run cancelled by user after the current tool completed."
                                ),
                            )
                        call_index += 1
                    continue

                if turn.finish_reason == "length":
                    self._messages.append(
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
                if self._manifest is not None:
                    self._manifest = self._manifest.model_copy(
                        update={"final_summary": final_summary}
                    )
                    await self._save_manifest()
                if not self._made_changes:
                    return await self._finish(
                        status=RunStatus.COMPLETED,
                        terminal_reason="no_changes",
                        summary=final_summary,
                    )
                try:
                    outcomes = await self._verify_all(deadline)
                except asyncio.CancelledError:
                    return await self._finish(
                        status=RunStatus.CANCELLED,
                        terminal_reason="verification_cancelled",
                        summary=final_summary or "Final verification cancelled by user.",
                    )
                if all(outcome.ok for outcome in outcomes):
                    patch, changed_files = await self._collect_patch(self._remaining(deadline))
                    review = await self._run_review_lane(
                        patch=patch,
                        changed_files=changed_files,
                        verification=outcomes,
                        deadline=deadline,
                    )
                    if review:
                        final_summary = (
                            f"{final_summary}\n\nReviewer lane:\n{review}".strip()
                        )
                    if error := self._token_budget_error():
                        return await self._finish(
                            status=RunStatus.FAILED,
                            terminal_reason="token_budget_exceeded",
                            summary=final_summary,
                            error=error,
                            verification=outcomes,
                            patch_timeout_seconds=1.0,
                        )
                    return await self._finish(
                        status=RunStatus.COMPLETED,
                        terminal_reason="verified",
                        summary=final_summary,
                        verification=outcomes,
                        patch_timeout_seconds=self._remaining(deadline),
                    )
                feedback = "\n\n".join(
                    f"Verification {outcome.name!r} failed (exit {outcome.exit_code}):\n"
                    f"{outcome.output}"
                    for outcome in outcomes
                    if not outcome.ok
                )
                self._messages.append(
                    Message(
                        role="user",
                        content=(
                            "The harness reran the required checks and they failed. Inspect this "
                            f"untrusted test output, fix the code, and retry:\n{feedback}"
                        ),
                    )
                )
                await self._checkpoint(RunStatus.VERIFYING, verification_passed=False)

            return await self._finish(
                status=RunStatus.FAILED,
                terminal_reason="max_steps_exceeded",
                summary=final_summary,
            )
        except TimeoutError:
            return await self._finish(
                status=RunStatus.FAILED,
                terminal_reason="wall_time_exceeded",
                summary="Model request exceeded the remaining wall-time budget.",
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
                error_text = (
                    f"{exc.provider_name} rejected the request ({exc.kind.value}): {exc}"
                )
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
        finally:
            if (
                self._manifest is not None
                and self._session_lease is not None
                and self._session_lease.active
                and self._run_started_monotonic is not None
            ):
                elapsed = time.monotonic() - self._run_started_monotonic
                self._active_wall_time_base += max(0.0, elapsed)
                self._active_started_at = None
                self._manifest = self._manifest.model_copy(
                    update={
                        "active_wall_time_seconds": self._active_wall_time_base,
                        "active_started_at": None,
                    }
                )
                await asyncio.shield(self._save_manifest())
            if self._session_lease is not None:
                self._session_lease.release()
