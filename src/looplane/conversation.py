"""looplane-owned durable conversations for read-only Ask mode.

Conversation state is deliberately separate from coding-run artifacts.  It contains no
vendor session identifiers, credentials, repository workspace, patch, or verification state.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from looplane.contracts import ContractModel
from looplane.events import atomic_write_json

SCHEMA_VERSION = 1
MAX_CONVERSATION_BYTES = 16 * 1024 * 1024
MAX_EVENT_BYTES = 128 * 1024
MAX_MESSAGE_CHARS = 16_000
MAX_REPLAY_CHARS = 48_000
MAX_REPLAY_MESSAGES = 12
_ID_LENGTH = 32


class ConversationError(RuntimeError):
    """Base error for unavailable or invalid looplane conversations."""


class ConversationBusyError(ConversationError):
    """Raised when another process owns a conversation's writer lease."""


class ConversationValidationError(ConversationError, ValueError):
    """Raised when persisted conversation state is unsafe or inconsistent."""


class ConversationEventKind(StrEnum):
    CREATED = "conversation.created"
    CONTEXT_CHANGED = "context.changed"
    CONTEXT_COMPACTED = "context.compacted"
    USER_MESSAGE = "user.message"
    ASSISTANT_CHUNK = "assistant.chunk"
    TURN_COMPLETED = "turn.completed"
    TURN_FAILED = "turn.failed"
    TURN_CANCELLED = "turn.cancelled"
    TURN_INTERRUPTED = "turn.interrupted"


_TURN_TERMINALS = {
    ConversationEventKind.TURN_COMPLETED,
    ConversationEventKind.TURN_FAILED,
    ConversationEventKind.TURN_CANCELLED,
    ConversationEventKind.TURN_INTERRUPTED,
}


def _safe_id(value: str, *, label: str) -> str:
    candidate = value.strip()
    windows = PureWindowsPath(candidate)
    path = Path(candidate)
    if (
        len(candidate) != _ID_LENGTH
        or any(character not in "0123456789abcdef" for character in candidate)
        or path.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or path.name != candidate
    ):
        raise ConversationValidationError(f"{label} must be one lowercase looplane identifier")
    return candidate


def _bounded_text(value: str, *, label: str, allow_blank: bool = False) -> str:
    if "\x00" in value:
        raise ValueError(f"{label} cannot contain NUL")
    if len(value) > MAX_MESSAGE_CHARS:
        raise ValueError(f"{label} exceeds {MAX_MESSAGE_CHARS} characters")
    if not allow_blank and not value:
        raise ValueError(f"{label} cannot be blank")
    return value


def _validate_runtime_slug(value: object) -> object:
    """Accept any runtime registered in the runtime registry (``None`` passes through)."""

    if value is None:
        return value
    from looplane.runtime_registry import RUNTIME_REGISTRY

    if value not in RUNTIME_REGISTRY:
        raise ValueError(f"unknown runtime: {value!r}")
    return value


class ConversationManifest(ContractModel):
    """Small versioned index for one looplane-owned conversation."""

    schema_version: Literal[1] = SCHEMA_VERSION
    conversation_id: str
    runtime: str
    model_override: str | None = None
    title: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_event_sequence: int = Field(default=-1, ge=-1)
    turn_count: int = Field(default=0, ge=0)
    active_turn_id: str | None = None
    active_writer_token: str | None = None

    @field_validator("runtime", mode="before")
    @classmethod
    def validate_runtime(cls, value: object) -> object:
        return _validate_runtime_slug(value)

    @field_validator("conversation_id")
    @classmethod
    def validate_conversation_id(cls, value: str) -> str:
        return _safe_id(value, label="conversation_id")

    @field_validator("active_turn_id")
    @classmethod
    def validate_active_turn_id(cls, value: str | None) -> str | None:
        return None if value is None else _safe_id(value, label="active_turn_id")

    @field_validator("model_override")
    @classmethod
    def validate_model_override(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or len(value) > 256 or not value.isprintable():
            raise ValueError("model_override must be a printable model name")
        return value

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        value = value.strip()
        if "\x00" in value or len(value) > 200:
            raise ValueError("title must contain at most 200 characters and no NUL")
        return value


class ConversationEvent(ContractModel):
    """One closed, provider-neutral record in a conversation event log."""

    schema_version: Literal[1] = SCHEMA_VERSION
    conversation_id: str
    sequence: int = Field(ge=0)
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    event_type: ConversationEventKind
    runtime: str | None = None
    model_override: str | None = None
    turn_id: str | None = None
    text: str | None = None
    reason: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("runtime", mode="before")
    @classmethod
    def validate_runtime(cls, value: object) -> object:
        return _validate_runtime_slug(value)

    @field_validator("conversation_id")
    @classmethod
    def validate_conversation_id(cls, value: str) -> str:
        return _safe_id(value, label="conversation_id")

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, value: str) -> str:
        return _safe_id(value, label="event_id")

    @field_validator("turn_id")
    @classmethod
    def validate_turn_id(cls, value: str | None) -> str | None:
        return None if value is None else _safe_id(value, label="turn_id")

    @field_validator("model_override")
    @classmethod
    def validate_model_override(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or len(value) > 256 or not value.isprintable():
            raise ValueError("model_override must be a printable model name")
        return value

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        return None if value is None else _bounded_text(value, label="event text")

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or len(value) > 256 or not value.isprintable():
            raise ValueError("reason must be a printable bounded value")
        return value

    @field_validator("error")
    @classmethod
    def validate_error(cls, value: str | None) -> str | None:
        return None if value is None else _bounded_text(value, label="event error")

    @model_validator(mode="after")
    def validate_shape(self) -> ConversationEvent:
        if self.event_type == ConversationEventKind.CREATED:
            if (
                self.runtime is not None
                or self.model_override is not None
                or self.turn_id is not None
                or self.text is not None
                or self.reason is not None
                or self.error is not None
            ):
                raise ValueError("conversation.created cannot contain turn data")
            return self
        if self.event_type == ConversationEventKind.CONTEXT_CHANGED:
            if (
                self.runtime is None
                or self.turn_id is not None
                or self.text is not None
                or self.reason is not None
                or self.error is not None
            ):
                raise ValueError(
                    "context.changed requires only runtime and optional model_override"
                )
            return self
        if self.event_type == ConversationEventKind.CONTEXT_COMPACTED:
            if (
                self.runtime is not None
                or self.model_override is not None
                or self.turn_id is not None
                or self.text is None
                or self.reason is not None
                or self.error is not None
            ):
                raise ValueError("context.compacted requires only checkpoint text")
            return self
        if self.runtime is not None or self.model_override is not None:
            raise ValueError("only context.changed can contain runtime context")
        if self.turn_id is None:
            raise ValueError("turn events require turn_id")
        if self.event_type in {
            ConversationEventKind.USER_MESSAGE,
            ConversationEventKind.ASSISTANT_CHUNK,
        }:
            if self.text is None or self.reason is not None or self.error is not None:
                raise ValueError("message events require only text")
        elif self.event_type == ConversationEventKind.TURN_COMPLETED:
            if self.text is not None or self.reason is not None or self.error is not None:
                raise ValueError("turn.completed cannot contain terminal details")
        elif self.text is not None:
            raise ValueError("terminal failure events cannot contain text")
        if self.event_type != ConversationEventKind.TURN_FAILED and self.error is not None:
            raise ValueError("only turn.failed can contain an error")
        return self


class ConversationMessage(ContractModel):
    """A bounded completed message suitable for looplane-owned prompt replay."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    turn_id: str

    @field_validator("turn_id")
    @classmethod
    def validate_turn_id(cls, value: str) -> str:
        return _safe_id(value, label="turn_id")


class ConversationSnapshot(ContractModel):
    manifest: ConversationManifest
    events: tuple[ConversationEvent, ...] = ()


@dataclass
class ConversationWriterLease:
    conversation_dir: Path
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

    def __enter__(self) -> ConversationWriterLease:
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def default_conversation_root() -> Path:
    configured = os.environ.get("LOOPLANE_CONVERSATION_ROOT") or os.environ.get(
        "PCA_CONVERSATION_ROOT"
    )
    if configured:
        return Path(configured)
    state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    root = state_root / "looplane" / "conversations"
    legacy_root = state_root / "python-coding-agent" / "conversations"
    return legacy_root if not root.exists() and legacy_root.exists() else root


class ConversationStore:
    """Strict storage, lifecycle validation, and writer fencing for Ask conversations."""

    def __init__(self, root: str | Path | None = None, *, durable: bool = True) -> None:
        self.root = Path(root) if root is not None else default_conversation_root()
        self.durable = durable

    def _ensure_directory(self, path: Path) -> None:
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_dir():
                raise ConversationValidationError(f"conversation path is unsafe: {path.name}")
        else:
            path.mkdir(parents=True, mode=0o700)
        os.chmod(path, 0o700, follow_symlinks=False)

    def _ensure_root(self) -> None:
        self._ensure_directory(self.root)

    def _directory(self, conversation_id: str) -> Path:
        conversation_id = _safe_id(conversation_id, label="conversation_id")
        return self.root / conversation_id

    def _require_regular(self, path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise ConversationValidationError(f"conversation artifact is unsafe: {path.name}")

    def acquire_writer(
        self, conversation_id: str, *, create: bool = False
    ) -> ConversationWriterLease:
        self._ensure_root()
        directory = self._directory(conversation_id)
        if create:
            if directory.exists() or directory.is_symlink():
                raise ConversationValidationError("conversation already exists")
            directory.mkdir(mode=0o700)
        self._ensure_directory(directory)
        lock_path = directory / ".writer.lock"
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise ConversationValidationError("conversation writer lock is unsafe") from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise ConversationBusyError("conversation already has an active writer") from exc
        os.fchmod(descriptor, 0o600)
        token = uuid4().hex
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"pid={os.getpid()} token={token}\n".encode())
        if self.durable:
            os.fsync(descriptor)
        return ConversationWriterLease(directory.resolve(), token, descriptor)

    def _require_lease(self, lease: ConversationWriterLease) -> None:
        if not lease.active:
            raise ConversationValidationError("an active conversation writer lease is required")
        root = self.root.resolve()
        if lease.conversation_dir.parent != root:
            raise ConversationValidationError("writer lease belongs to another store")
        _safe_id(lease.conversation_dir.name, label="conversation_id")

    async def _write_manifest(self, manifest: ConversationManifest) -> None:
        path = self._directory(manifest.conversation_id) / "conversation.json"
        if path.is_symlink():
            raise ConversationValidationError("conversation manifest cannot be a symlink")
        await atomic_write_json(path, manifest, durable=self.durable)
        os.chmod(path, 0o600, follow_symlinks=False)

    def _read_json(self, path: Path) -> object:
        self._require_regular(path)
        if path.stat().st_size > MAX_CONVERSATION_BYTES:
            raise ConversationValidationError(f"conversation artifact is too large: {path.name}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConversationValidationError(
                f"conversation artifact is invalid JSON: {path.name}"
            ) from exc

    def _read_events(self, directory: Path, conversation_id: str) -> tuple[ConversationEvent, ...]:
        path = directory / "events.jsonl"
        self._require_regular(path)
        size = path.stat().st_size
        if size > MAX_CONVERSATION_BYTES:
            raise ConversationValidationError("conversation event log is too large")
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ConversationValidationError("conversation event log is unreadable") from exc
        if payload and not payload.endswith(b"\n"):
            raise ConversationValidationError("conversation event log has a partial final record")
        events: list[ConversationEvent] = []
        for expected, raw in enumerate(payload.splitlines()):
            if not raw or len(raw) > MAX_EVENT_BYTES:
                raise ConversationValidationError(
                    "conversation event record is invalid or too large"
                )
            try:
                event = ConversationEvent.model_validate_json(raw)
            except ValueError as exc:
                raise ConversationValidationError(
                    "conversation event does not match the supported schema"
                ) from exc
            if event.sequence != expected:
                raise ConversationValidationError("conversation event sequence is not contiguous")
            if event.conversation_id != conversation_id:
                raise ConversationValidationError("conversation event id does not match directory")
            events.append(event)
        self._validate_lifecycle(tuple(events))
        return tuple(events)

    @staticmethod
    def _validate_lifecycle(events: tuple[ConversationEvent, ...]) -> None:
        if not events:
            raise ConversationValidationError("conversation event log cannot be empty")
        active: str | None = None
        user_seen = False
        assistant_seen = False
        assistant_chars = 0
        for index, event in enumerate(events):
            if index == 0:
                if event.event_type != ConversationEventKind.CREATED:
                    raise ConversationValidationError(
                        "conversation must begin with conversation.created"
                    )
                continue
            if event.event_type == ConversationEventKind.CREATED:
                raise ConversationValidationError("conversation.created may appear only once")
            if event.event_type == ConversationEventKind.CONTEXT_CHANGED:
                if active is not None:
                    raise ConversationValidationError(
                        "conversation context cannot change during an active turn"
                    )
                continue
            if event.event_type == ConversationEventKind.CONTEXT_COMPACTED:
                if active is not None:
                    raise ConversationValidationError(
                        "conversation context cannot compact during an active turn"
                    )
                continue
            if event.event_type == ConversationEventKind.USER_MESSAGE:
                if active is not None:
                    raise ConversationValidationError("conversation contains overlapping turns")
                active = event.turn_id
                user_seen = True
                assistant_seen = False
                assistant_chars = 0
            elif active is None or event.turn_id != active or not user_seen:
                raise ConversationValidationError("conversation turn lifecycle is inconsistent")
            elif event.event_type == ConversationEventKind.ASSISTANT_CHUNK:
                assistant_seen = True
                assistant_chars += len(event.text or "")
                if assistant_chars > MAX_MESSAGE_CHARS:
                    raise ConversationValidationError(
                        "completed assistant message exceeds replay limit"
                    )
            elif event.event_type in _TURN_TERMINALS:
                if event.event_type == ConversationEventKind.TURN_COMPLETED and not assistant_seen:
                    raise ConversationValidationError("completed turn has no assistant content")
                active = None
                user_seen = False
                assistant_seen = False
                assistant_chars = 0

    @staticmethod
    def _turn_state(events: tuple[ConversationEvent, ...]) -> tuple[int, str | None]:
        completed = 0
        active: str | None = None
        for event in events:
            if event.event_type == ConversationEventKind.USER_MESSAGE:
                active = event.turn_id
            elif event.event_type in _TURN_TERMINALS:
                if event.event_type == ConversationEventKind.TURN_COMPLETED:
                    completed += 1
                active = None
        return completed, active

    @staticmethod
    def _context_state(
        events: tuple[ConversationEvent, ...],
        *,
        runtime: str,
        model_override: str | None,
    ) -> tuple[str, str | None]:
        for event in events:
            if event.event_type == ConversationEventKind.CONTEXT_CHANGED:
                assert event.runtime is not None
                runtime = event.runtime
                model_override = event.model_override
        return runtime, model_override

    def _load_sync(
        self, conversation_id: str, *, allow_log_ahead: bool = False
    ) -> ConversationSnapshot:
        self._ensure_root()
        directory = self._directory(conversation_id)
        if directory.is_symlink() or not directory.is_dir():
            raise ConversationValidationError("conversation directory is missing or unsafe")
        payload = self._read_json(directory / "conversation.json")
        try:
            manifest = ConversationManifest.model_validate(payload)
        except ValueError as exc:
            raise ConversationValidationError(
                "conversation manifest does not match the supported schema"
            ) from exc
        if manifest.conversation_id != directory.name:
            raise ConversationValidationError("conversation manifest id does not match directory")
        events = self._read_events(directory, manifest.conversation_id)
        actual_last = events[-1].sequence if events else -1
        log_ahead = actual_last == manifest.last_event_sequence + 1
        if manifest.last_event_sequence != actual_last and not (allow_log_ahead and log_ahead):
            raise ConversationValidationError("manifest event sequence does not match event log")
        state_events = events[: manifest.last_event_sequence + 1] if log_ahead else events
        completed, active = self._turn_state(state_events)
        if manifest.turn_count != completed or manifest.active_turn_id != active:
            raise ConversationValidationError("manifest turn state does not match event log")
        runtime, model_override = self._context_state(
            state_events,
            runtime=manifest.runtime,
            model_override=manifest.model_override,
        )
        if manifest.runtime != runtime or manifest.model_override != model_override:
            raise ConversationValidationError("manifest context does not match event log")
        return ConversationSnapshot(manifest=manifest, events=events)

    async def load(self, conversation_id: str) -> ConversationSnapshot:
        return await asyncio.to_thread(self._load_sync, conversation_id)

    async def create(
        self,
        *,
        runtime: str,
        model_override: str | None = None,
        title: str = "",
        conversation_id: str | None = None,
    ) -> ConversationSnapshot:
        conversation_id = conversation_id or uuid4().hex
        lease = self.acquire_writer(conversation_id, create=True)
        try:
            manifest = ConversationManifest(
                conversation_id=conversation_id,
                runtime=runtime,
                model_override=model_override,
                title=title,
                active_writer_token=lease.token,
            )
            events_path = lease.conversation_dir / "events.jsonl"
            descriptor = os.open(events_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            await self._write_manifest(manifest)
            event = ConversationEvent(
                conversation_id=conversation_id,
                sequence=0,
                event_type=ConversationEventKind.CREATED,
            )
            await asyncio.to_thread(self._append_line, events_path, event)
            await self._write_manifest(manifest.model_copy(update={"last_event_sequence": 0}))
            return await self.load(conversation_id)
        finally:
            lease.release()

    async def new(
        self,
        *,
        runtime: str,
        model_override: str | None = None,
        title: str = "",
        conversation_id: str | None = None,
    ) -> ConversationSnapshot:
        return await self.create(
            runtime=runtime,
            model_override=model_override,
            title=title,
            conversation_id=conversation_id,
        )

    async def claim(self, lease: ConversationWriterLease) -> ConversationSnapshot:
        self._require_lease(lease)
        snapshot = await self.load(lease.conversation_dir.name)
        manifest = snapshot.manifest.model_copy(update={"active_writer_token": lease.token})
        await self._write_manifest(manifest)
        return snapshot.model_copy(update={"manifest": manifest})

    def _append_line(self, path: Path, event: ConversationEvent) -> None:
        self._require_regular(path)
        payload = (
            event.model_dump_json(
                exclude_computed_fields=True,
                exclude_none=True,
            ).encode("utf-8")
            + b"\n"
        )
        if len(payload) > MAX_EVENT_BYTES:
            raise ConversationValidationError("conversation event exceeds the record limit")
        if path.stat().st_size + len(payload) > MAX_CONVERSATION_BYTES:
            raise ConversationValidationError("conversation event log exceeds its size limit")
        flags = os.O_APPEND | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(descriptor, view) :]
            if self.durable:
                os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def append(
        self,
        lease: ConversationWriterLease,
        event_type: ConversationEventKind,
        *,
        turn_id: str | None = None,
        text: str | None = None,
        reason: str | None = None,
        error: str | None = None,
    ) -> ConversationEvent:
        self._require_lease(lease)
        snapshot = await self.load(lease.conversation_dir.name)
        if snapshot.manifest.active_writer_token != lease.token:
            raise ConversationValidationError("conversation is fenced by another writer token")
        event = ConversationEvent(
            conversation_id=snapshot.manifest.conversation_id,
            sequence=snapshot.manifest.last_event_sequence + 1,
            event_type=event_type,
            turn_id=turn_id,
            text=text,
            reason=reason,
            error=error,
        )
        candidate_events = (*snapshot.events, event)
        self._validate_lifecycle(candidate_events)
        await asyncio.to_thread(self._append_line, lease.conversation_dir / "events.jsonl", event)
        active_turn = snapshot.manifest.active_turn_id
        turn_count = snapshot.manifest.turn_count
        if event_type == ConversationEventKind.USER_MESSAGE:
            active_turn = event.turn_id
        elif event_type in _TURN_TERMINALS:
            active_turn = None
            if event_type == ConversationEventKind.TURN_COMPLETED:
                turn_count += 1
        manifest = snapshot.manifest.model_copy(
            update={
                "last_event_sequence": event.sequence,
                "active_turn_id": active_turn,
                "turn_count": turn_count,
                "updated_at": datetime.now(UTC),
            }
        )
        await self._write_manifest(manifest)
        return event

    async def change_context(
        self,
        lease: ConversationWriterLease,
        *,
        runtime: str,
        model_override: str | None = None,
    ) -> ConversationEvent:
        """Persist a provider-neutral runtime/model switch between completed turns."""

        self._require_lease(lease)
        snapshot = await self.load(lease.conversation_dir.name)
        if snapshot.manifest.active_writer_token != lease.token:
            raise ConversationValidationError("conversation is fenced by another writer token")
        if snapshot.manifest.active_turn_id is not None:
            raise ConversationValidationError(
                "conversation context cannot change during an active turn"
            )
        event = ConversationEvent(
            conversation_id=snapshot.manifest.conversation_id,
            sequence=snapshot.manifest.last_event_sequence + 1,
            event_type=ConversationEventKind.CONTEXT_CHANGED,
            runtime=runtime,
            model_override=model_override,
        )
        candidate_events = (*snapshot.events, event)
        self._validate_lifecycle(candidate_events)
        await asyncio.to_thread(self._append_line, lease.conversation_dir / "events.jsonl", event)
        manifest = snapshot.manifest.model_copy(
            update={
                "runtime": event.runtime,
                "model_override": event.model_override,
                "last_event_sequence": event.sequence,
                "updated_at": datetime.now(UTC),
            }
        )
        await self._write_manifest(manifest)
        return event

    async def append_context_checkpoint(
        self,
        lease: ConversationWriterLease,
        checkpoint: object,
    ) -> ConversationEvent:
        """Persist one compacted-context checkpoint between completed turns."""

        self._require_lease(lease)
        snapshot = await self.load(lease.conversation_dir.name)
        if snapshot.manifest.active_writer_token != lease.token:
            raise ConversationValidationError("conversation is fenced by another writer token")
        if snapshot.manifest.active_turn_id is not None:
            raise ConversationValidationError("conversation context cannot compact during a turn")
        if hasattr(checkpoint, "model_dump"):
            payload = checkpoint.model_dump(mode="json")  # type: ignore[attr-defined]
        else:
            payload = checkpoint
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if len(text) > MAX_MESSAGE_CHARS:
            raise ConversationValidationError("context checkpoint exceeds the event text limit")
        event = ConversationEvent(
            conversation_id=snapshot.manifest.conversation_id,
            sequence=snapshot.manifest.last_event_sequence + 1,
            event_type=ConversationEventKind.CONTEXT_COMPACTED,
            text=text,
        )
        candidate_events = (*snapshot.events, event)
        self._validate_lifecycle(candidate_events)
        await asyncio.to_thread(self._append_line, lease.conversation_dir / "events.jsonl", event)
        manifest = snapshot.manifest.model_copy(
            update={"last_event_sequence": event.sequence, "updated_at": datetime.now(UTC)}
        )
        await self._write_manifest(manifest)
        return event

    async def list(self) -> tuple[ConversationManifest, ...]:
        await asyncio.to_thread(self._ensure_root)
        manifests: list[ConversationManifest] = []
        for directory in await asyncio.to_thread(lambda: tuple(self.root.iterdir())):
            if directory.name.startswith(".") or directory.is_symlink() or not directory.is_dir():
                continue
            try:
                snapshot = await self.load(directory.name)
            except ConversationError:
                continue
            manifests.append(snapshot.manifest)
        return tuple(sorted(manifests, key=lambda item: item.updated_at, reverse=True))

    async def resume(
        self, conversation_id: str = "last"
    ) -> tuple[ConversationSnapshot, ConversationWriterLease]:
        if conversation_id == "last":
            await asyncio.to_thread(self._ensure_root)
            candidates: list[ConversationManifest] = []
            for directory in await asyncio.to_thread(lambda: tuple(self.root.iterdir())):
                if (
                    directory.name.startswith(".")
                    or directory.is_symlink()
                    or not directory.is_dir()
                ):
                    continue
                try:
                    candidate = await asyncio.to_thread(
                        self._load_sync, directory.name, allow_log_ahead=True
                    )
                except ConversationError:
                    continue
                candidates.append(candidate.manifest)
            if not candidates:
                raise ConversationValidationError("no persisted conversations were found")
            conversation_id = max(candidates, key=lambda item: item.updated_at).conversation_id
        lease = self.acquire_writer(conversation_id)
        try:
            snapshot = await asyncio.to_thread(
                self._load_sync, conversation_id, allow_log_ahead=True
            )
            actual_last = snapshot.events[-1].sequence
            log_ahead = actual_last == snapshot.manifest.last_event_sequence + 1
            completed, active = self._turn_state(snapshot.events)
            runtime, model_override = self._context_state(
                snapshot.events,
                runtime=snapshot.manifest.runtime,
                model_override=snapshot.manifest.model_override,
            )
            manifest = snapshot.manifest.model_copy(
                update={
                    "active_writer_token": lease.token,
                    "last_event_sequence": (
                        actual_last if log_ahead else snapshot.manifest.last_event_sequence
                    ),
                    "turn_count": completed if log_ahead else snapshot.manifest.turn_count,
                    "active_turn_id": active if log_ahead else snapshot.manifest.active_turn_id,
                    "runtime": runtime if log_ahead else snapshot.manifest.runtime,
                    "model_override": (
                        model_override if log_ahead else snapshot.manifest.model_override
                    ),
                    "updated_at": datetime.now(UTC),
                }
            )
            await self._write_manifest(manifest)
            snapshot = snapshot.model_copy(update={"manifest": manifest})
            if manifest.active_turn_id is not None:
                await self.append(
                    lease,
                    ConversationEventKind.TURN_INTERRUPTED,
                    turn_id=manifest.active_turn_id,
                    reason="resume_after_incomplete_turn",
                )
                snapshot = await self.load(conversation_id)
        except Exception:
            lease.release()
            raise
        return snapshot, lease

    async def completed_turns(
        self,
        conversation_id: str,
        *,
        max_messages: int = MAX_REPLAY_MESSAGES,
        max_chars: int = MAX_REPLAY_CHARS,
    ) -> tuple[ConversationMessage, ...]:
        if max_messages <= 0 or max_chars <= 0:
            raise ValueError("replay limits must be positive")
        snapshot = await self.load(conversation_id)
        turns: list[tuple[ConversationMessage, ConversationMessage]] = []
        user: ConversationMessage | None = None
        assistant_parts: list[str] = []
        for event in snapshot.events:
            if event.event_type == ConversationEventKind.USER_MESSAGE:
                assert event.turn_id is not None and event.text is not None
                user = ConversationMessage(role="user", content=event.text, turn_id=event.turn_id)
                assistant_parts = []
            elif event.event_type == ConversationEventKind.ASSISTANT_CHUNK:
                assert event.text is not None
                assistant_parts.append(event.text)
            elif event.event_type == ConversationEventKind.TURN_COMPLETED:
                if user is None:
                    raise ConversationValidationError("completed turn is missing its user message")
                assistant_text = "".join(assistant_parts)
                if len(assistant_text) > MAX_MESSAGE_CHARS:
                    raise ConversationValidationError(
                        "completed assistant message exceeds replay limit"
                    )
                turns.append(
                    (
                        user,
                        ConversationMessage(
                            role="assistant", content=assistant_text, turn_id=user.turn_id
                        ),
                    )
                )
                user = None
                assistant_parts = []
            elif event.event_type in _TURN_TERMINALS:
                user = None
                assistant_parts = []
            elif event.event_type == ConversationEventKind.CONTEXT_COMPACTED:
                continue
        selected: list[ConversationMessage] = []
        used_chars = 0
        for pair in reversed(turns):
            pair_chars = sum(len(message.content) for message in pair)
            if len(selected) + 2 > max_messages or used_chars + pair_chars > max_chars:
                break
            selected[0:0] = pair
            used_chars += pair_chars
        return tuple(selected)

    async def fork_before_turn(
        self,
        source_conversation_id: str,
        turn_id: str,
        *,
        title: str = "",
    ) -> tuple[ConversationSnapshot, ConversationWriterLease]:
        """Fork a conversation immediately before the turn with ``turn_id``.

        The new branch contains every event preceding the selected user message
        (rewritten for the new conversation id) and excludes the selected turn
        and everything after it.  The source conversation is never modified.
        """

        source = await self.load(source_conversation_id)
        if source.manifest.active_turn_id is not None:
            raise ConversationValidationError("cannot rewind during an active turn")
        split_index: int | None = None
        for index, event in enumerate(source.events):
            if event.event_type == ConversationEventKind.USER_MESSAGE and event.turn_id == turn_id:
                split_index = index
                break
        if split_index is None:
            raise ConversationValidationError("rewind turn was not found in the conversation")
        prefix = source.events[:split_index]
        self._validate_lifecycle(prefix)
        branch_id = uuid4().hex
        lease = self.acquire_writer(branch_id, create=True)
        try:
            runtime, model_override = self._context_state(
                prefix,
                runtime=source.manifest.runtime,
                model_override=source.manifest.model_override,
            )
            completed, active = self._turn_state(prefix)
            events_path = lease.conversation_dir / "events.jsonl"
            descriptor = os.open(events_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            for event in prefix:
                rewritten = event.model_copy(update={"conversation_id": branch_id})
                await asyncio.to_thread(self._append_line, events_path, rewritten)
            manifest = ConversationManifest(
                conversation_id=branch_id,
                runtime=runtime,
                model_override=model_override,
                title=title,
                last_event_sequence=len(prefix) - 1,
                turn_count=completed,
                active_turn_id=active,
                active_writer_token=lease.token,
            )
            await self._write_manifest(manifest)
        except Exception:
            lease.release()
            raise
        snapshot = await self.load(branch_id)
        return snapshot, lease

    async def clear(self, conversation_id: str) -> Path:
        self._ensure_root()
        directory = self._directory(conversation_id)
        lease = self.acquire_writer(conversation_id)
        try:
            trash = self.root / ".trash"
            self._ensure_directory(trash)
            destination = trash / f"{conversation_id}.{uuid4().hex}"
            await asyncio.to_thread(os.replace, directory, destination)
        finally:
            lease.release()
        if self.durable:
            for path in (self.root, trash):
                descriptor = os.open(path, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        return destination

    async def delete(self, conversation_id: str) -> Path:
        """Recoverably remove a conversation from normal listings."""

        return await self.clear(conversation_id)
