from __future__ import annotations

from dataclasses import dataclass


class ToolExecutionError(RuntimeError):
    """A bounded, user-visible tool failure."""


@dataclass(frozen=True)
class ReviewablePatch:
    content: str
    changed_paths: tuple[str, ...]


@dataclass(frozen=True)
class _PathSnapshot:
    existed: bool
    data: bytes
    mode: int | None
