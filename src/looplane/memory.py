"""Small explicit memory store for prompt context."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MemoryType = Literal["user_preference", "project_fact", "project_preference"]


class MemoryEntry(BaseModel):
    """One user-approved fact that may be injected into future prompts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: MemoryType
    name: str = Field(min_length=1)
    description: str = Field(min_length=1, max_length=2_000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    project: str | None = None

    @field_validator("name", "description", "project")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.strip().split())
        if not normalized:
            raise ValueError("memory text cannot be blank")
        return normalized


def default_memory_path() -> Path:
    configured = os.environ.get("LOOPLANE_MEMORY_PATH")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".looplane" / "memory.jsonl"


def parse_remember_argument(argument: str) -> tuple[MemoryType, str, str]:
    """Parse `/remember [user|project|preference]: text`."""

    text = argument.strip()
    if not text:
        raise ValueError("/remember requires a memory description")
    prefix, separator, rest = text.partition(":")
    if separator:
        normalized = prefix.strip().casefold().replace("-", "_").replace(" ", "_")
        body = rest.strip()
        if normalized in {"user", "user_preference"}:
            return "user_preference", "user preference", body
        if normalized in {"project", "project_fact"}:
            return "project_fact", "project fact", body
        if normalized in {"preference", "project_preference"}:
            return "project_preference", "project preference", body
    return "project_fact", "project fact", text


def remember(
    argument: str,
    *,
    project: Path | None = None,
    memory_path: Path | None = None,
) -> MemoryEntry:
    memory_type, name, description = parse_remember_argument(argument)
    project_value = None
    if memory_type != "user_preference" and project is not None:
        project_value = str(project.resolve(strict=False))
    entry = MemoryEntry(
        type=memory_type,
        name=name,
        description=description,
        project=project_value,
    )
    path = memory_path or default_memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry.model_dump_json() + "\n")
    return entry


def load_memory_entries(memory_path: Path | None = None) -> tuple[MemoryEntry, ...]:
    path = memory_path or default_memory_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ()
    entries: list[MemoryEntry] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entries.append(MemoryEntry.model_validate(json.loads(line)))
        except (json.JSONDecodeError, ValueError):
            continue
    return tuple(entries)


def relevant_memory_entries(
    *,
    project: Path,
    memory_path: Path | None = None,
    limit: int = 20,
) -> tuple[MemoryEntry, ...]:
    project_key = str(project.resolve(strict=False))
    selected = [
        entry
        for entry in load_memory_entries(memory_path)
        if entry.type == "user_preference" or entry.project == project_key
    ]
    return tuple(selected[-limit:])


def render_known_context(entries: tuple[MemoryEntry, ...]) -> str:
    if not entries:
        return ""
    lines = ["Known context from explicit /remember entries:"]
    for entry in entries:
        scope = "user" if entry.type == "user_preference" else "project"
        lines.append(f"- [{scope}] {entry.description}")
    return "\n".join(lines)
