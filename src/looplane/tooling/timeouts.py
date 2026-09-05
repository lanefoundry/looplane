"""Harness timeout caps shared by bounded tool operations."""

from __future__ import annotations

from looplane.tooling.types import ToolExecutionError


def effective_timeout(default: float, override: float | None) -> float:
    if override is None:
        return default
    if override <= 0:
        raise ToolExecutionError("harness timeout budget is exhausted")
    return min(default, override)
