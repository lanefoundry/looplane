"""Extract durable memories from a completed agent run — no LLM call required."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from pathlib import Path

from looplane.contracts import (
    ConversationItem,
    Message,
    RunStatus,
    ToolObservation,
    VerificationOutcome,
)
from looplane.memory import MemoryFile, save_memory_file

logger = logging.getLogger(__name__)

MAX_BODY_CHARS = 1500


def _file_paths_from_messages(messages: Sequence[ConversationItem]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for item in messages:
        if not isinstance(item, Message) or item.role != "assistant":
            continue
        for call in item.tool_calls:
            for key in ("path", "file_path"):
                raw = call.arguments.get(key)
                if isinstance(raw, str) and raw and raw not in seen:
                    paths.append(raw)
                    seen.add(raw)
            raw_paths = call.arguments.get("paths")
            if isinstance(raw_paths, list):
                for p in raw_paths:
                    if isinstance(p, str) and p and p not in seen:
                        paths.append(p)
                        seen.add(p)
    return paths


def _tool_names_used(messages: Sequence[ConversationItem]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for item in messages:
        if not isinstance(item, Message) or item.role != "assistant":
            continue
        for call in item.tool_calls:
            if call.name not in seen:
                names.append(call.name)
                seen.add(call.name)
    return names


def _errors_from_messages(messages: Sequence[ConversationItem]) -> list[str]:
    errors: list[str] = []
    for item in messages:
        if isinstance(item, ToolObservation) and not item.ok and item.error:
            line = item.error.strip().splitlines()[0][:200]
            if line and line not in errors:
                errors.append(line)
    return errors[:8]


def _run_slug(instruction: str, run_id: str) -> str:
    digest = hashlib.sha256(f"{instruction}:{run_id}".encode()).hexdigest()[:8]
    return f"session-{digest}"


def extract_session_memory(
    *,
    run_id: str,
    instruction: str,
    messages: Sequence[ConversationItem],
    status: RunStatus,
    summary: str,
    verification: tuple[VerificationOutcome, ...],
    step_count: int,
    project: Path,
    memory_dir: Path | None = None,
) -> MemoryFile | None:
    """Build and save a structured session memory from a completed run."""
    file_paths = _file_paths_from_messages(messages)
    tool_names = _tool_names_used(messages)
    errors = _errors_from_messages(messages)

    sections: list[str] = []

    task_preview = instruction[:300]
    if len(instruction) > 300:
        task_preview += "…"
    sections.append(f"**Task:** {task_preview}")
    sections.append(f"**Status:** {status.value} ({step_count} steps)")

    if summary:
        sections.append(f"**Summary:** {summary[:400]}")

    if verification:
        v_lines = []
        for v in verification:
            result = "passed" if v.ok else "failed"
            v_lines.append(f"  - {v.name}: {result}")
        sections.append("**Verification:**\n" + "\n".join(v_lines))

    if file_paths:
        shown = file_paths[:15]
        paths_str = ", ".join(f"`{p}`" for p in shown)
        if len(file_paths) > 15:
            paths_str += f" (+{len(file_paths) - 15} more)"
        sections.append(f"**Files touched:** {paths_str}")

    if errors:
        err_lines = [f"  - {e}" for e in errors[:5]]
        sections.append("**Errors encountered:**\n" + "\n".join(err_lines))

    if tool_names:
        sections.append(f"**Tools used:** {', '.join(tool_names[:10])}")

    body = "\n\n".join(sections)
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n\n…(truncated)"

    description = f"Session: {task_preview[:100]}"

    try:
        return save_memory_file(
            memory_type="session_summary",
            description=description,
            body=body,
            slug=_run_slug(instruction, run_id),
            project=project,
            memory_dir=memory_dir,
        )
    except OSError:
        logger.warning("failed to save session memory", exc_info=True)
        return None


def persist_compaction_summary(
    *,
    run_id: str,
    summary_text: str,
    project: Path,
    memory_dir: Path | None = None,
) -> MemoryFile | None:
    """Persist a compaction summary so future sessions can reference it."""
    body = summary_text.strip()
    if not body or len(body) < 50:
        return None
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n\n…(truncated)"
    slug = f"compaction-{hashlib.sha256(run_id.encode()).hexdigest()[:8]}"
    try:
        return save_memory_file(
            memory_type="session_summary",
            description=f"Compaction summary from run {run_id[:12]}",
            body=body,
            slug=slug,
            project=project,
            memory_dir=memory_dir,
        )
    except OSError:
        logger.warning("failed to persist compaction summary", exc_info=True)
        return None
