"""Crash-safe session manifest storage and single-writer resume fencing."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from rivumi.approvals import ApprovalDecision, ApprovalRequest, ToolEffect
from rivumi.contracts import (
    ContractModel,
    ConversationItem,
    TaskContract,
    Usage,
    VerificationOutcome,
)
from rivumi.events import RunEvent, atomic_write_json
from rivumi.prompts import CODING_AGENT_PROMPT_VERSION
from rivumi.runtime import sanitized_subprocess_env

_MAX_JSON_BYTES = 16 * 1024 * 1024
_SCHEMA_VERSION = 1


class SessionError(RuntimeError):
    """Base error for invalid or unavailable persisted sessions."""


class SessionBusyError(SessionError):
    """Raised when another process already owns the session writer lease."""


class SessionValidationError(SessionError):
    """Raised when persisted state is incomplete, inconsistent, or unsafe to resume."""


class SessionPhase(StrEnum):
    CREATED = "created"
    PREPARING = "preparing"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    VERIFYING = "verifying"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL_PHASES = {
    SessionPhase.COMPLETED,
    SessionPhase.FAILED,
    SessionPhase.CANCELLED,
}


class ApprovalAuditRecord(ContractModel):
    request: ApprovalRequest
    decision: ApprovalDecision
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SessionManifest(ContractModel):
    """Versioned state sufficient to continue one provider-neutral agent loop."""

    schema_version: Literal[1] = _SCHEMA_VERSION
    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    provider_name: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    protocol: str = Field(min_length=1)
    # Schema-v1 manifests written before M3 did not record a prompt version. Keep that
    # migration value distinct instead of relabelling an old in-flight session as M3.
    prompt_version: str = Field(default="m2-unversioned-patch", min_length=1)
    base_sha: str = Field(pattern=r"^[0-9a-fA-F]{40}$")
    phase: SessionPhase = SessionPhase.CREATED
    step: int = Field(default=0, ge=0)
    messages: tuple[ConversationItem, ...] = ()
    usage: Usage = Field(default_factory=Usage)
    final_summary: str = ""
    last_action_fingerprint: str | None = None
    repeat_count: int = Field(default=0, ge=0)
    last_event_sequence: int = Field(default=-1, ge=-1)
    verification: tuple[VerificationOutcome, ...] = ()
    pending_action: ApprovalRequest | None = None
    approval_history: tuple[ApprovalAuditRecord, ...] = ()
    granted_effects: frozenset[ToolEffect] = frozenset()
    active_wall_time_seconds: float = Field(default=0.0, ge=0)
    active_started_at: datetime | None = None
    terminal: bool = False
    active_writer_token: str | None = None
    writer_claimed_at: datetime | None = None
    writer_heartbeat_at: datetime | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("run_id", "task_id", "provider_name", "model_id", "protocol")
    @classmethod
    def no_blank_identifiers(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("session identifiers cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_lifecycle(self) -> SessionManifest:
        if self.terminal != (self.phase in _TERMINAL_PHASES):
            raise ValueError("terminal flag must agree with the terminal session phase")
        if self.phase == SessionPhase.WAITING_APPROVAL and self.pending_action is None:
            raise ValueError("waiting_approval phase requires pending_action")
        if self.pending_action is not None and self.pending_action.run_id != self.run_id:
            raise ValueError("pending approval run_id does not match session")
        return self

    @classmethod
    def new(
        cls,
        *,
        run_id: str,
        task_id: str,
        provider_name: str,
        model_id: str,
        protocol: str,
        base_sha: str,
        prompt_version: str = CODING_AGENT_PROMPT_VERSION,
    ) -> SessionManifest:
        return cls(
            run_id=run_id,
            task_id=task_id,
            provider_name=provider_name,
            model_id=model_id,
            protocol=protocol,
            prompt_version=prompt_version,
            base_sha=base_sha,
        )


@dataclass
class SessionWriterLease:
    """An OS-backed exclusive writer lease; the open descriptor is the real fence."""

    run_dir: Path
    token: str
    _descriptor: int
    _released: bool = False

    @property
    def active(self) -> bool:
        return not self._released

    def release(self) -> None:
        if self._released:
            return
        fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        os.close(self._descriptor)
        self._released = True

    def __enter__(self) -> SessionWriterLease:
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def _read_json(path: Path) -> object:
    try:
        size = path.stat().st_size
    except FileNotFoundError as exc:
        raise SessionValidationError(f"missing session artifact: {path.name}") from exc
    if size > _MAX_JSON_BYTES:
        raise SessionValidationError(f"session artifact is too large: {path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SessionValidationError(f"invalid JSON session artifact: {path.name}") from exc


class SessionStore:
    """Own atomic manifest updates and strict validation of an existing run directory."""

    def __init__(self, run_dir: str | Path, *, durable: bool = True) -> None:
        self.run_dir = Path(run_dir)
        self.manifest_path = self.run_dir / "session.json"
        self.durable = durable

    def acquire_writer(self) -> SessionWriterLease:
        if self.run_dir.is_symlink():
            raise SessionValidationError("session run directory cannot be a symlink")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.run_dir / ".writer.lock", flags, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise SessionBusyError("session already has an active writer") from exc
        token = uuid4().hex
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"pid={os.getpid()} token={token}\n".encode())
        if self.durable:
            os.fsync(descriptor)
        return SessionWriterLease(self.run_dir.resolve(), token, descriptor)

    def _require_lease(self, lease: SessionWriterLease) -> None:
        if not lease.active or lease.run_dir != self.run_dir.resolve():
            raise SessionValidationError("an active writer lease for this session is required")

    @staticmethod
    def _manifest_payload(manifest: SessionManifest) -> dict[str, object]:
        # Pydantic computed fields (notably Usage.total_tokens) are output-only values and must
        # not be written back into strict input contracts.
        return manifest.model_dump(mode="json", exclude_computed_fields=True)

    async def load(self) -> SessionManifest:
        payload = await asyncio.to_thread(_read_json, self.manifest_path)
        try:
            return SessionManifest.model_validate(payload)
        except ValueError as exc:
            raise SessionValidationError(
                "session.json does not match the supported schema"
            ) from exc

    async def initialize(
        self, manifest: SessionManifest, lease: SessionWriterLease
    ) -> SessionManifest:
        self._require_lease(lease)
        if self.manifest_path.exists():
            raise SessionValidationError("session.json already exists")
        if manifest.run_id != self.run_dir.name:
            raise SessionValidationError("run_id must match the run directory name")
        claimed = self._claim_value(manifest, lease)
        await atomic_write_json(
            self.manifest_path, self._manifest_payload(claimed), durable=self.durable
        )
        return claimed

    async def claim(self, lease: SessionWriterLease) -> SessionManifest:
        self._require_lease(lease)
        current = await self.load()
        claimed = self._claim_value(current, lease)
        await atomic_write_json(
            self.manifest_path, self._manifest_payload(claimed), durable=self.durable
        )
        return claimed

    @staticmethod
    def _claim_value(
        manifest: SessionManifest, lease: SessionWriterLease
    ) -> SessionManifest:
        now = datetime.now(UTC)
        return manifest.model_copy(
            update={
                "active_writer_token": lease.token,
                "writer_claimed_at": now,
                "writer_heartbeat_at": now,
                "updated_at": now,
            }
        )

    async def save(
        self, manifest: SessionManifest, lease: SessionWriterLease
    ) -> SessionManifest:
        self._require_lease(lease)
        if manifest.active_writer_token != lease.token:
            raise SessionValidationError("manifest is fenced by a different writer token")
        on_disk = await self.load()
        if on_disk.active_writer_token != lease.token:
            raise SessionValidationError("session writer token changed on disk")
        now = datetime.now(UTC)
        updated = manifest.model_copy(
            update={"writer_heartbeat_at": now, "updated_at": now}
        )
        await atomic_write_json(
            self.manifest_path, self._manifest_payload(updated), durable=self.durable
        )
        return updated

    async def claim_and_validate_resume(
        self, lease: SessionWriterLease, *, allow_terminal: bool = False
    ) -> tuple[SessionManifest, TaskContract]:
        manifest = await self.claim(lease)
        task = await self._validate_request(manifest)
        actual_last, last_event_type = await self._validate_events(manifest)
        manifest_was_ahead = manifest.last_event_sequence == actual_last + 1
        if manifest_was_ahead:
            manifest = manifest.model_copy(update={"last_event_sequence": actual_last})
            manifest = await self.save(manifest, lease)
        if not manifest_was_ahead and last_event_type in {
            "tool.started",
            "verification.started",
        }:
            raise SessionValidationError(
                "session stopped during a side effect; automatic resume cannot prove whether "
                "the action completed"
            )
        await asyncio.to_thread(self._validate_workspace, manifest)
        if manifest.terminal and not allow_terminal:
            raise SessionValidationError("terminal sessions cannot be resumed")
        return manifest, task

    async def _validate_request(self, manifest: SessionManifest) -> TaskContract:
        payload = await asyncio.to_thread(_read_json, self.run_dir / "request.json")
        try:
            task = TaskContract.model_validate(payload)
        except ValueError as exc:
            raise SessionValidationError("request.json is not a valid task contract") from exc
        if task.task_id != manifest.task_id:
            raise SessionValidationError("request task_id does not match session")
        if task.base_sha is None or task.base_sha.lower() != manifest.base_sha.lower():
            raise SessionValidationError("request base_sha does not match session")
        return task

    async def _validate_events(self, manifest: SessionManifest) -> tuple[int, str | None]:
        path = self.run_dir / "events.jsonl"
        if not path.exists():
            if manifest.last_event_sequence == -1:
                return -1, None
            raise SessionValidationError("events.jsonl is missing")
        if path.stat().st_size > _MAX_JSON_BYTES:
            raise SessionValidationError("events.jsonl is too large to resume safely")

        def read_events() -> list[RunEvent]:
            events: list[RunEvent] = []
            try:
                for raw in path.read_text(encoding="utf-8").splitlines():
                    if raw.strip():
                        events.append(RunEvent.model_validate_json(raw))
            except (OSError, UnicodeError, ValueError) as exc:
                raise SessionValidationError("events.jsonl contains an invalid event") from exc
            return events

        events = await asyncio.to_thread(read_events)
        for expected, event in enumerate(events):
            if event.sequence != expected:
                raise SessionValidationError("event sequence is not contiguous")
            if event.run_id != manifest.run_id:
                raise SessionValidationError("event run_id does not match session")
            if event.task_id is not None and event.task_id != manifest.task_id:
                raise SessionValidationError("event task_id does not match session")
        actual_last = events[-1].sequence if events else -1
        if manifest.last_event_sequence not in {actual_last, actual_last + 1}:
            raise SessionValidationError("manifest event sequence does not match events.jsonl")
        last_event_type = events[-1].event_type if events else None
        return actual_last, last_event_type

    def _validate_workspace(self, manifest: SessionManifest) -> None:
        workspace = self.run_dir / "workspace"
        if workspace.is_symlink() or not workspace.is_dir():
            raise SessionValidationError("session workspace is missing or is a symlink")
        env = sanitized_subprocess_env()

        def git(*args: str) -> str:
            try:
                result = subprocess.run(
                    ("git", "-C", str(workspace), *args),
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                    env=env,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise SessionValidationError("unable to validate session workspace") from exc
            if result.returncode != 0:
                raise SessionValidationError("session workspace is not a valid Git worktree")
            return result.stdout.strip()

        top_level = Path(git("rev-parse", "--show-toplevel")).resolve()
        if top_level != workspace.resolve():
            raise SessionValidationError("workspace Git root does not match the session workspace")
        if git("rev-parse", "HEAD").lower() != manifest.base_sha.lower():
            raise SessionValidationError("workspace HEAD does not match the recorded base_sha")
