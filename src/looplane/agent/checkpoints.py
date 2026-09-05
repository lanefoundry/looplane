"""Ordered event, checkpoint, and leased manifest persistence for a bounded run."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from looplane.agent.state import ActiveRunClock, TurnState
from looplane.contracts import Checkpoint, Message, RunStatus, TaskContract
from looplane.events import EventSink, RunEvent, atomic_write_json
from looplane.session import (
    SessionManifest,
    SessionPhase,
    SessionStore,
    SessionValidationError,
    SessionWriterLease,
)


def check_resume_identity(
    manifest: SessionManifest, *, provider_name: str, model_id: str, protocol: str
) -> None:
    if (
        manifest.provider_name != provider_name
        or manifest.model_id != model_id
        or manifest.protocol != protocol
    ):
        raise SessionValidationError(
            "resume provider/protocol/model must match the persisted session"
        )


def session_phase(status: RunStatus) -> SessionPhase:
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


@dataclass(frozen=True)
class ClaimedSession:
    store: SessionStore
    lease: SessionWriterLease
    manifest: SessionManifest
    task: TaskContract


async def claim_session(
    run_dir: Path,
    *,
    durable: bool,
    allow_terminal: bool = False,
) -> ClaimedSession:
    store = SessionStore(run_dir, durable=durable)
    lease = store.acquire_writer()
    try:
        manifest, task = await store.claim_and_validate_resume(lease, allow_terminal=allow_terminal)
        return ClaimedSession(store, lease, manifest, task)
    except BaseException:
        lease.release()
        raise


class RunPersistence:
    """Own durable ordering without owning the engine, model, or tool executor."""

    def __init__(self, run_id: str, run_dir: Path, event_sink: EventSink) -> None:
        self.run_id = run_id
        self.run_dir = run_dir
        self.event_sink = event_sink
        self.sequence = 0
        self.writer_token = uuid4().hex
        self.store: SessionStore | None = None
        self.lease: SessionWriterLease | None = None
        self.manifest: SessionManifest | None = None

    async def initialize(
        self,
        task: TaskContract,
        *,
        durable: bool,
        provider_name: str,
        model_id: str,
        protocol: str,
        base_sha: str,
    ) -> None:
        self.store = SessionStore(self.run_dir, durable=durable)
        self.lease = self.store.acquire_writer()
        await atomic_write_json(self.run_dir / "request.json", task)
        self.manifest = await self.store.initialize(
            SessionManifest.new(
                run_id=self.run_id,
                task_id=task.task_id,
                provider_name=provider_name,
                model_id=model_id,
                protocol=protocol,
                base_sha=base_sha,
            ),
            self.lease,
        )

    async def save(self) -> None:
        if self.store is None or self.lease is None or self.manifest is None:
            return
        self.manifest = await self.store.save(self.manifest, self.lease)

    async def emit(
        self,
        task_id: str,
        state: TurnState,
        clock: ActiveRunClock,
        event_type: str,
        **data: Any,
    ) -> None:
        event = RunEvent(
            event_type=event_type,
            run_id=self.run_id,
            task_id=task_id,
            sequence=self.sequence,
            data=data,
        )
        if self.manifest is not None:
            self.manifest = self.manifest.model_copy(
                update={
                    "last_event_sequence": event.sequence,
                    "step": state.step,
                    "messages": tuple(state.messages),
                    "usage": state.usage,
                    "model_usage": tuple(state.model_usage),
                    "last_action_fingerprint": state.last_fingerprint,
                    "repeat_count": state.repeat_count,
                    "verification": state.last_verification,
                    "active_wall_time_seconds": clock.active_wall_time_base,
                    "active_started_at": clock.active_started_at,
                }
            )
            await self.save()
        await self.event_sink.emit(event)
        self.sequence += 1

    async def checkpoint(
        self, task_id: str, state: TurnState, status: RunStatus, **metadata: Any
    ) -> None:
        checkpoint = Checkpoint(
            run_id=self.run_id,
            task_id=task_id,
            status=status,
            step=state.step,
            messages=tuple(state.messages),
            tool_call_count=sum(
                len(item.tool_calls) for item in state.messages if isinstance(item, Message)
            ),
            usage=state.usage,
            active_writer_token=self.writer_token,
            last_action_fingerprint=state.last_fingerprint,
            metadata=metadata,
        )
        await atomic_write_json(self.run_dir / "checkpoint.json", checkpoint)
        if self.manifest is not None:
            phase = session_phase(status)
            self.manifest = self.manifest.model_copy(
                update={
                    "phase": phase,
                    "terminal": phase
                    in {SessionPhase.COMPLETED, SessionPhase.FAILED, SessionPhase.CANCELLED},
                    "step": state.step,
                    "messages": tuple(state.messages),
                    "usage": state.usage,
                    "model_usage": tuple(state.model_usage),
                    "last_action_fingerprint": state.last_fingerprint,
                    "repeat_count": state.repeat_count,
                    "verification": state.last_verification,
                    "verified_workspace_fingerprint": state.verified_workspace_fingerprint,
                }
            )
            await self.save()
