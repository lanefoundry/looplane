"""Atomic file writes and explicit rollback snapshots, separate from orchestration."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from looplane.policy import SafePathPolicy
from looplane.tooling.read_versions import ReadVersionStore
from looplane.tooling.types import ToolExecutionError, _PathSnapshot


class AtomicWrite(Protocol):
    def __call__(self, target: Path, payload: bytes, mode: int) -> None: ...


class ResetSnapshotIndex(Protocol):
    def __call__(self, paths: Sequence[str], *, timeout_seconds: float) -> object: ...


class AtomicFileWriter:
    def __init__(self, *, new_id: Callable[[], str] | None = None) -> None:
        self.new_id = new_id or (lambda: uuid4().hex)

    def replace(self, target: Path, payload: bytes, mode: int) -> None:
        temporary = target.with_name(f".{target.name}.looplane-replace-{self.new_id()}")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode, follow_symlinks=False)
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


class WorkspaceSnapshots:
    def __init__(
        self, *, policy: SafePathPolicy, versions: ReadVersionStore,
        atomic_write: AtomicWrite, reset_index: ResetSnapshotIndex,
    ) -> None:
        self.policy = policy
        self.versions = versions
        self.atomic_write = atomic_write
        self.reset_index = reset_index

    def capture(self, paths: Sequence[str]) -> dict[str, _PathSnapshot]:
        snapshots: dict[str, _PathSnapshot] = {}
        for path in paths:
            target = self.policy.resolve(path)
            if target.exists():
                if not target.is_file():
                    raise ToolExecutionError(f"transaction path is not a regular file: {path}")
                snapshots[path] = _PathSnapshot(
                    existed=True,
                    data=target.read_bytes(),
                    mode=stat.S_IMODE(target.stat().st_mode),
                )
            else:
                snapshots[path] = _PathSnapshot(existed=False, data=b"", mode=None)
        return snapshots


    def restore(self, snapshots: Mapping[str, _PathSnapshot]) -> None:
        if snapshots:
            self.reset_index(
                tuple(sorted(snapshots)),
                timeout_seconds=5.0,
            )
        for path, snapshot in snapshots.items():
            target = self.policy.resolve(path)
            if snapshot.existed:
                target.parent.mkdir(parents=True, exist_ok=True)
                assert snapshot.mode is not None
                self.atomic_write(target, snapshot.data, snapshot.mode)
                self.versions.record(path, snapshot.data)
            else:
                target.unlink(missing_ok=True)
                self.versions.forget(path)


