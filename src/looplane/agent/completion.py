"""Patch artifacts and ordered terminal persistence; no terminal policy engine."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from looplane.agent.ports import EventEmitter
from looplane.agent.verification import JsonWriter
from looplane.contracts import (
    CostBreakdown,
    ModelUsageRecord,
    RunResult,
    RunStatus,
    Usage,
    VerificationOutcome,
)
from looplane.events import atomic_write_json
from looplane.tooling.types import ToolExecutionError


class ReviewablePatch(Protocol):
    @property
    def content(self) -> str: ...

    @property
    def changed_paths(self) -> tuple[str, ...]: ...


class PatchSource(Protocol):
    def reviewable_patch(self, *, timeout_seconds: float | None) -> ReviewablePatch: ...


class CheckpointWriter(Protocol):
    async def __call__(self, status: RunStatus, **metadata: Any) -> None: ...


@dataclass(frozen=True)
class ResultAccounting:
    usage: Usage
    model_usage: tuple[ModelUsageRecord, ...]
    cost: CostBreakdown | None


@dataclass(frozen=True)
class CompletionInputs:
    run_id: str
    task_id: str
    run_dir: Path
    verification: tuple[VerificationOutcome, ...]


@dataclass(frozen=True)
class CompletionRequest:
    status: RunStatus
    terminal_reason: str
    summary: str
    error: str | None = None
    verification: tuple[VerificationOutcome, ...] | None = None
    patch_timeout_seconds: float | None = None
    collected_patch: tuple[str, tuple[str, ...]] | None = None


@dataclass(frozen=True)
class CompletionPorts:
    collect_patch: Callable[[float | None], Awaitable[tuple[str, tuple[str, ...]]]]
    accounting: Callable[[], ResultAccounting]
    checkpoint: CheckpointWriter
    emit: EventEmitter
    close_executor: Callable[[], None]
    write_json: JsonWriter = atomic_write_json


async def collect_patch(
    run_dir: Path,
    executor: PatchSource | None,
    timeout_seconds: float | None = None,
) -> tuple[str, tuple[str, ...]]:
    if executor is None:
        patch = ""
        changed: tuple[str, ...] = ()
    else:
        review = await asyncio.to_thread(
            executor.reviewable_patch,
            timeout_seconds=timeout_seconds,
        )
        patch = review.content
        changed = review.changed_paths
    (run_dir / "changes.patch").write_text(patch, encoding="utf-8")
    return patch, changed


async def finish(
    inputs: CompletionInputs,
    request: CompletionRequest,
    ports: CompletionPorts,
) -> RunResult:
    status = request.status
    terminal_reason = request.terminal_reason
    summary = request.summary
    error = request.error
    verification = request.verification
    patch_timeout_seconds = request.patch_timeout_seconds
    collected_patch = request.collected_patch
    if verification is None:
        verification = inputs.verification
    try:
        if collected_patch is None:
            _patch, changed_files = await ports.collect_patch(patch_timeout_seconds)
        else:
            patch, changed_files = collected_patch
            (inputs.run_dir / "changes.patch").write_text(patch, encoding="utf-8")
    except (ToolExecutionError, TimeoutError) as exc:
        status = RunStatus.FAILED
        terminal_reason = "patch_artifact_failed"
        summary = f"{summary}\n\nFinal patch artifact refused: {exc}".strip()
        changed_files = ()
        (inputs.run_dir / "changes.patch").write_text("", encoding="utf-8")
    if not (inputs.run_dir / "test.log").exists():
        (inputs.run_dir / "test.log").write_text("", encoding="utf-8")
    accounting = ports.accounting()
    result = RunResult(
        run_id=inputs.run_id,
        task_id=inputs.task_id,
        status=status,
        summary=summary,
        changed_files=changed_files,
        verification=verification,
        usage=accounting.usage,
        model_usage=accounting.model_usage,
        cost=accounting.cost,
        terminal_reason=terminal_reason,
        error=error,
        artifacts={
            "request": str(inputs.run_dir / "request.json"),
            "events": str(inputs.run_dir / "events.jsonl"),
            "checkpoint": str(inputs.run_dir / "checkpoint.json"),
            "patch": str(inputs.run_dir / "changes.patch"),
            "test_log": str(inputs.run_dir / "test.log"),
            "result": str(inputs.run_dir / "result.json"),
        }
        | (
            {"cache_traces": str(inputs.run_dir / "cache-traces.jsonl")}
            if (inputs.run_dir / "cache-traces.jsonl").is_file()
            else {}
        )
        | (
            {"review": str(inputs.run_dir / "review.md")}
            if (inputs.run_dir / "review.md").is_file()
            else {}
        ),
    )
    await ports.checkpoint(status, terminal_reason=terminal_reason)
    await ports.emit(
        f"run.{status.value}", terminal_reason=terminal_reason, changed_files=changed_files
    )
    await ports.write_json(inputs.run_dir / "result.json", result)
    ports.close_executor()
    return result
