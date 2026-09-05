"""Append-only event logging and crash-safe JSON persistence."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from pydantic_core import to_jsonable_python


class RunEvent(BaseModel):
    """One immutable event in a run's ordered audit stream."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    task_id: str | None = None
    sequence: int = Field(ge=0)
    data: dict[str, Any] = Field(default_factory=dict)
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


@runtime_checkable
class EventSink(Protocol):
    """Consumer of canonical native-run events, shared by console and SDK."""

    async def emit(self, event: RunEvent) -> None: ...


JsonValue = BaseModel | Mapping[str, Any] | list[Any] | tuple[Any, ...]


def _json_bytes(value: Any, *, newline: bool = False) -> bytes:
    payload = json.dumps(
        to_jsonable_python(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return payload + (b"\n" if newline else b"")


def _append_bytes(path: Path, payload: bytes, *, durable: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        if durable:
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes, *, durable: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            file.write(payload)
            file.flush()
            if durable:
                os.fsync(file.fileno())
        os.replace(temporary_path, path)
        if durable:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


async def atomic_write_json(path: str | Path, value: JsonValue, *, durable: bool = True) -> None:
    """Atomically replace a JSON document using a temp file in the same directory."""

    await asyncio.to_thread(_atomic_write, Path(path), _json_bytes(value), durable=durable)


async def write_json_atomic(path: str | Path, value: JsonValue, *, durable: bool = True) -> None:
    """Readable alias for :func:`atomic_write_json`."""

    await atomic_write_json(path, value, durable=durable)


class EventWriter:
    """Serialize events as append-only JSONL without rewriting prior records."""

    def __init__(self, path: str | Path, *, durable: bool = True) -> None:
        self.path = Path(path)
        self.durable = durable
        self._lock = asyncio.Lock()

    async def append(self, event: RunEvent) -> None:
        async with self._lock:
            await asyncio.to_thread(
                _append_bytes,
                self.path,
                _json_bytes(event, newline=True),
                durable=self.durable,
            )

    async def write(self, event: RunEvent) -> None:
        """Alias matching common event-sink terminology."""

        await self.append(event)
