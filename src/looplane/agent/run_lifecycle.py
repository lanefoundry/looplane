"""Bounded-run lifecycle around an explicit low-level turn-engine callback."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath

from looplane.agent.checkpoints import RunPersistence
from looplane.agent.state import ActiveRunClock
from looplane.contracts import RunResult
from looplane.workspace.local_git import WorkspacePreparationError


def validate_run_id(run_id: str) -> None:
    run_id_path = Path(run_id)
    windows_path = PureWindowsPath(run_id)
    if (
        not run_id
        or "\x00" in run_id
        or run_id in {".", ".."}
        or run_id_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or run_id_path.name != run_id
    ):
        raise ValueError("run_id must be one safe relative path segment")


def validate_run_location(repository: Path, run_dir: Path) -> None:
    source = repository.resolve(strict=True)
    candidate = run_dir.resolve(strict=False)
    try:
        candidate.relative_to(source)
    except ValueError:
        return
    raise WorkspacePreparationError("run directory must not be inside the source repository")


class BoundedRunLifecycle:
    """Charge active time and settle persistence around one bounded engine invocation.

    The callback owns turn transitions, retry, tools, verification, and completion.
    This facade owns no runner reference or conversation-runtime session.
    """

    def __init__(self, persistence: RunPersistence) -> None:
        self.persistence = persistence
        self.clock = ActiveRunClock()

    def current_active_wall_time(self) -> float:
        elapsed = 0.0
        if self.clock.run_started_monotonic is not None:
            elapsed = max(0.0, time.monotonic() - self.clock.run_started_monotonic)
        return self.clock.active_wall_time_base + elapsed

    async def save_clock(self) -> None:
        if self.persistence.manifest is not None:
            self.persistence.manifest = self.persistence.manifest.model_copy(
                update={
                    "active_wall_time_seconds": self.clock.active_wall_time_base,
                    "active_started_at": self.clock.active_started_at,
                }
            )
            await self.persistence.save()

    async def pause_active_wall_time(self) -> None:
        if self.clock.run_started_monotonic is None:
            return
        self.clock.active_wall_time_base = self.current_active_wall_time()
        self.clock.run_started_monotonic = None
        self.clock.active_started_at = None
        await self.save_clock()

    async def resume_active_wall_time(self) -> None:
        if self.clock.run_started_monotonic is not None:
            return
        self.clock.run_started_monotonic = time.monotonic()
        self.clock.active_started_at = datetime.now(UTC)
        await self.save_clock()

    async def run(self, turn_engine: Callable[[], Awaitable[RunResult]]) -> RunResult:
        manifest = self.persistence.manifest
        self.clock.active_wall_time_base = (
            manifest.active_wall_time_seconds if manifest is not None else 0.0
        )
        now = datetime.now(UTC)
        if manifest is not None and manifest.active_started_at is not None:
            self.clock.active_wall_time_base += max(
                0.0, (now - manifest.active_started_at).total_seconds()
            )
        self.clock.run_started_monotonic = time.monotonic()
        self.clock.active_started_at = now
        await self.save_clock()
        try:
            return await turn_engine()
        finally:
            if (
                self.persistence.manifest is not None
                and self.persistence.lease is not None
                and self.persistence.lease.active
                and self.clock.run_started_monotonic is not None
            ):
                self.clock.active_wall_time_base = self.current_active_wall_time()
                self.clock.run_started_monotonic = None
                self.clock.active_started_at = None
                await asyncio.shield(self.save_clock())
            if self.persistence.lease is not None:
                self.persistence.lease.release()
