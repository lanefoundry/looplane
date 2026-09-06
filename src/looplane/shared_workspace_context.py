"""Read-only snapshot of a source repository for multi-session shared context."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

from looplane.runtime import (
    WorkspacePreparationError,
    run_bounded_command,
    sanitized_subprocess_env,
)


@dataclass(frozen=True)
class SharedWorkspaceContext:
    """Immutable, lightweight snapshot of the source repo state.

    Computed once at server startup without cloning the repository.
    Multiple conversation sessions can reference the same snapshot to
    share baseline information (HEAD commit, dirty status) while each
    maintaining an independent writable workspace.
    """

    source_repository: Path
    base_sha: str
    source_was_dirty: bool
    source_snapshot_warning: str | None
    version: str
    created_at: float

    @classmethod
    async def create(cls, source_repository: Path) -> SharedWorkspaceContext:
        source = Path(source_repository).resolve(strict=True)
        return await asyncio.to_thread(cls._create_sync, source)

    @classmethod
    def _create_sync(cls, source: Path) -> SharedWorkspaceContext:
        if not source.is_dir():
            raise WorkspacePreparationError(f"source repository is not a directory: {source}")

        root_result = _source_git(source, ("rev-parse", "--show-toplevel"))
        if not root_result.ok or Path(root_result.stdout.strip()).resolve() != source:
            raise WorkspacePreparationError("source_repository must be the Git worktree root")

        head = _source_git(source, ("rev-parse", "--verify", "HEAD^{commit}"))
        base_sha = head.stdout.strip().lower()
        if not head.ok or len(base_sha) != 40:
            raise WorkspacePreparationError("could not resolve a full source HEAD commit")

        status_result = _source_git(
            source,
            ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
            max_output_chars=2_000_000,
        )
        if not status_result.ok or status_result.stdout_truncated:
            raise WorkspacePreparationError("could not inspect source repository status")

        source_was_dirty = bool(status_result.stdout)
        now = time.monotonic()
        version = hashlib.sha256(f"{base_sha}:{source_was_dirty}:{now}".encode()).hexdigest()

        return cls(
            source_repository=source,
            base_sha=base_sha,
            source_was_dirty=source_was_dirty,
            source_snapshot_warning=(
                "The source repository had uncommitted changes when this session started. "
                "The disposable workspace contains committed HEAD only; staged, unstaged, "
                "and untracked source changes are not included."
                if source_was_dirty
                else None
            ),
            version=version,
            created_at=now,
        )

    def is_stale(self, max_age_seconds: float = 300.0) -> bool:
        return (time.monotonic() - self.created_at) > max_age_seconds

    async def refresh(self) -> SharedWorkspaceContext:
        return await self.create(self.source_repository)


def _source_git(
    source: Path,
    argv: tuple[str, ...],
    *,
    max_output_chars: int = 20_000,
):
    env = sanitized_subprocess_env()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return run_bounded_command(
        (
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            *argv,
        ),
        cwd=source,
        timeout_seconds=30.0,
        max_output_chars=max_output_chars,
        env=env,
    )
