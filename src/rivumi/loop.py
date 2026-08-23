"""Explicit, provider-neutral coding-agent loop."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from collections.abc import Callable
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
    effect_for_tool,
)
from rivumi.console import CompositeEventSink, EventSink, JsonlEventSink
from rivumi.contracts import (
    Checkpoint,
    ConversationItem,
    Message,
    ModelTurn,
    RunResult,
    RunStatus,
    TaskContract,
    ToolCall,
    ToolObservation,
    Usage,
    VerificationCommand,
    VerificationOutcome,
)
from rivumi.events import EventWriter, RunEvent, atomic_write_json
from rivumi.models import ModelProvider, ProviderError
from rivumi.policy import SafePathPolicy
from rivumi.prompts import CODING_AGENT_PROMPT_VERSION, CODING_AGENT_SYSTEM_PROMPT
from rivumi.runtime import (
    LocalGitWorkspace,
    WorkspacePreparationError,
    bounded_text,
    run_bounded_command,
    sanitized_subprocess_env,
)
from rivumi.session import (
    ApprovalAuditRecord,
    SessionManifest,
    SessionPhase,
    SessionStore,
    SessionValidationError,
    SessionWriterLease,
)
from rivumi.tools import ToolExecutionError, ToolExecutor


class UnsafeLocalExecutionError(RuntimeError):
    """Raised unless the caller explicitly accepts unsandboxed repository code execution."""


BlockingResult = TypeVar("BlockingResult")


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
        event_sink: EventSink | None = None,
    ) -> None:
        self.task = task
        self.model = model
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
        self._step = 0
        self._last_fingerprint: str | None = None
        self._repeat_count = 0
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
        self._cancel_requested = asyncio.Event()

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
            runner._step = manifest.step
            runner._last_fingerprint = manifest.last_action_fingerprint
            runner._repeat_count = manifest.repeat_count
            runner._last_verification = manifest.verification
            runner._run_dir_initialized = True
            workspace = resolved / "workspace"
            runner._executor = ToolExecutor(
                workspace=workspace,
                policy=SafePathPolicy(workspace, task.allowed_paths),
                verification_commands=task.verification,
                limits=task.limits,
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
    ) -> ApprovalDecision:
        request = ApprovalRequest(
            run_id=self.run_id,
            action_id=action_id,
            effect=effect,
            reason=reason,
            preview=bounded_text(preview, 16_000),
            tool_call=tool_call,
            command=command,
        )
        if self._manifest is not None and effect in self._manifest.granted_effects:
            self._manifest = self._manifest.model_copy(
                update={
                    "phase": SessionPhase.RUNNING,
                    "pending_action": request,
                }
            )
            await self._save_manifest()
            await self._event(
                "approval.reused",
                request_id=request.request_id,
                action_id=action_id,
                effect=effect.value,
                reason=reason.value,
            )
            return ApprovalDecision.ALLOW_ONCE
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
            preview=request.preview,
        )
        decision = await self.approvals.decide(request)
        if self._manifest is not None:
            granted_effects = self._manifest.granted_effects
            if decision == ApprovalDecision.ALLOW_SESSION:
                granted_effects = frozenset((*granted_effects, effect))
            pending_action = request
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
            decision=decision.value,
        )
        return decision

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

    def _initial_messages(self, base_sha: str) -> list[ConversationItem]:
        checks = "\n".join(
            f"- {command.name}: {list(command.argv)!r}" for command in self.task.verification
        )
        paths = "\n".join(f"- {pattern}" for pattern in self.task.allowed_paths)
        request = (
            f"Task: {self.task.instruction}\n"
            f"Base commit: {base_sha}\n"
            f"Allowed paths:\n{paths}\n"
            f"Required verification:\n{checks}\n"
            "Inspect the repository, make the smallest correct patch, and verify it."
        )
        return [
            Message(role="system", content=CODING_AGENT_SYSTEM_PROMPT),
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

    async def _verify_all(self, deadline: float) -> tuple[VerificationOutcome, ...]:
        assert self._executor is not None
        outcomes: list[VerificationOutcome] = []
        for command in self.task.verification:
            if self._cancel_requested.is_set():
                raise asyncio.CancelledError("run cancellation requested")
            decision = await self._approval(
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
            self.model.complete(self._messages, self._executor.definitions)
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

    async def _finish(
        self,
        *,
        status: RunStatus,
        terminal_reason: str,
        summary: str,
        verification: tuple[VerificationOutcome, ...] = (),
        patch_timeout_seconds: float | None = None,
    ) -> RunResult:
        verification = verification or self._last_verification
        try:
            _, changed_files = await self._collect_patch(patch_timeout_seconds)
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
            terminal_reason=terminal_reason,
            artifacts={
                "request": str(self.run_dir / "request.json"),
                "events": str(self.run_dir / "events.jsonl"),
                "checkpoint": str(self.run_dir / "checkpoint.json"),
                "patch": str(self.run_dir / "changes.patch"),
                "test_log": str(self.run_dir / "test.log"),
                "result": str(self.run_dir / "result.json"),
            },
        )
        await self._checkpoint(status, terminal_reason=terminal_reason)
        await self._event(
            f"run.{status.value}", terminal_reason=terminal_reason, changed_files=changed_files
        )
        await atomic_write_json(self.run_dir / "result.json", result)
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
                    remaining = self._remaining(deadline)
                except TimeoutError:
                    return await self._finish(
                        status=RunStatus.FAILED,
                        terminal_reason="wall_time_exceeded",
                        summary=final_summary,
                        patch_timeout_seconds=1.0,
                    )
                self._step += 1
                await self._event("model.requested", step=self._step)
                turn = await self._complete_model_or_cancel(remaining)
                if turn is None:
                    return await self._finish(
                        status=RunStatus.CANCELLED,
                        terminal_reason="user_cancelled",
                        summary="Run cancelled by user while waiting for the model.",
                    )
                self._usage = self._add_usage(self._usage, turn.usage)
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
                    for call in turn.tool_calls:
                        if self._cancel_requested.is_set():
                            return await self._finish(
                                status=RunStatus.CANCELLED,
                                terminal_reason="user_cancelled",
                                summary="Run cancelled by user before executing the next tool.",
                            )
                        if self._record_fingerprint(call):
                            return await self._finish(
                                status=RunStatus.FAILED,
                                terminal_reason="repeated_action",
                                summary=turn.content or final_summary,
                            )
                        effect = effect_for_tool(call.name)
                        await self._event(
                            "tool.requested",
                            tool_call_id=call.tool_call_id,
                            name=call.name,
                            effect=effect.value,
                            arguments=self._event_arguments(call.arguments),
                        )
                        decision = await self._approval(
                            action_id=call.tool_call_id,
                            effect=effect,
                            reason=ApprovalReason.MODEL_TOOL,
                            preview=self._tool_preview(call),
                            tool_call=call,
                        )
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
                            continue
                        await self._event(
                            "tool.started",
                            tool_call_id=call.tool_call_id,
                            name=call.name,
                            effect=effect.value,
                        )
                        if self._manifest is not None and self._manifest.pending_action is not None:
                            await self._mark_approved_action_started(
                                self._manifest.pending_action.request_id
                            )
                        observation = await self._run_blocking_safely(
                            self._executor.execute,
                            call,
                            timeout_seconds=self._remaining(deadline),
                        )
                        self._messages.append(observation)
                        await self._event(
                            "tool.completed",
                            tool_call_id=call.tool_call_id,
                            name=call.name,
                            ok=observation.ok,
                            error=observation.error,
                            preview=bounded_text(observation.content, 2_000),
                        )
                        await self._checkpoint(RunStatus.IMPLEMENTING, last_tool=call.name)
                        if self._cancel_requested.is_set():
                            return await self._finish(
                                status=RunStatus.CANCELLED,
                                terminal_reason="user_cancelled",
                                summary=(
                                    "Run cancelled by user after the current tool completed."
                                ),
                            )
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
                try:
                    outcomes = await self._verify_all(deadline)
                except asyncio.CancelledError:
                    return await self._finish(
                        status=RunStatus.CANCELLED,
                        terminal_reason="verification_cancelled",
                        summary=final_summary or "Final verification cancelled by user.",
                    )
                if all(outcome.ok for outcome in outcomes):
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
            return await self._finish(
                status=RunStatus.FAILED,
                terminal_reason=f"provider_{exc.kind.value}",
                summary=str(exc),
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
