"""Memory store for prompt context — manual /remember entries and auto-extracted memories."""

from __future__ import annotations

import json
import os
import re
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MemoryType = Literal["user_preference", "project_fact", "project_preference"]

MemoryFileType = Literal[
    "session_summary",
    "user_preference",
    "project_fact",
    "project_preference",
]


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


class MemoryFile(BaseModel):
    """A durable memory stored as a Markdown file with YAML frontmatter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str = Field(min_length=1)
    type: MemoryFileType
    description: str = Field(min_length=1, max_length=2_000)
    body: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    project: str | None = None


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower().strip()).strip("-")[:64]
    return slug or "memory"


def _parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}, content
    raw = match.group(1)
    meta: dict[str, str] = {}
    for line in raw.splitlines():
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key and value:
            meta[key] = value
    return meta, content[match.end() :]


def _render_frontmatter(mem: MemoryFile) -> str:
    lines = [
        "---",
        f"type: {mem.type}",
        f"description: {mem.description}",
        f"created_at: {mem.created_at.isoformat()}",
    ]
    if mem.project:
        lines.append(f"project: {mem.project}")
    lines.append("---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def default_memory_path() -> Path:
    configured = os.environ.get("LOOPLANE_MEMORY_PATH")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".looplane" / "memory.jsonl"


def default_memory_dir() -> Path:
    configured = os.environ.get("LOOPLANE_MEMORY_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".looplane" / "memory"


# ---------------------------------------------------------------------------
# Legacy JSONL /remember
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Markdown memory files (memdir)
# ---------------------------------------------------------------------------


def save_memory_file(
    *,
    memory_type: MemoryFileType,
    description: str,
    body: str,
    slug: str | None = None,
    project: Path | None = None,
    memory_dir: Path | None = None,
) -> MemoryFile:
    """Write a Markdown memory file with YAML frontmatter."""
    if not slug:
        slug = _slugify(description)
    if not _SLUG_RE.match(slug):
        slug = _slugify(slug)
    project_value = str(project.resolve(strict=False)) if project else None
    mem = MemoryFile(
        slug=slug,
        type=memory_type,
        description=description,
        body=body.strip(),
        project=project_value,
    )
    directory = memory_dir or default_memory_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{mem.slug}.md"
    content = _render_frontmatter(mem) + "\n\n" + mem.body + "\n"
    path.write_text(content, encoding="utf-8")
    return mem


def load_memory_files(
    memory_dir: Path | None = None,
    *,
    max_files: int = 200,
) -> tuple[MemoryFile, ...]:
    """Scan the memory directory for Markdown files with YAML frontmatter."""
    directory = memory_dir or default_memory_dir()
    if not directory.is_dir():
        return ()
    files = sorted(directory.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    results: list[MemoryFile] = []
    for path in files[:max_files]:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, body = _parse_frontmatter(content)
        if not meta.get("type") or not meta.get("description"):
            continue
        body = body.strip()
        if not body:
            continue
        try:
            results.append(
                MemoryFile(
                    slug=path.stem,
                    type=meta["type"],  # type: ignore[arg-type]
                    description=meta["description"],
                    body=body,
                    project=meta.get("project"),
                    created_at=datetime.fromisoformat(meta["created_at"])
                    if "created_at" in meta
                    else datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
                )
            )
        except (ValueError, KeyError, OSError):
            continue
    return tuple(results)


# ---------------------------------------------------------------------------
# Unified retrieval and rendering
# ---------------------------------------------------------------------------


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


def relevant_memory_files(
    *,
    project: Path,
    memory_dir: Path | None = None,
    limit: int = 20,
) -> tuple[MemoryFile, ...]:
    """Return memory files relevant to the given project."""
    project_key = str(project.resolve(strict=False))
    selected = [
        mem
        for mem in load_memory_files(memory_dir)
        if mem.project is None or mem.project == project_key
    ]
    return tuple(selected[:limit])


def render_known_context(
    entries: tuple[MemoryEntry, ...],
    memory_files: tuple[MemoryFile, ...] = (),
) -> str:
    parts: list[str] = []
    if entries:
        lines = ["Known context from /remember entries:"]
        for entry in entries:
            scope = "user" if entry.type == "user_preference" else "project"
            lines.append(f"- [{scope}] {entry.description}")
        parts.append("\n".join(lines))
    if memory_files:
        lines = ["Recalled memories from prior sessions:"]
        for mem in memory_files:
            body_preview = textwrap.shorten(mem.body, width=500, placeholder="…")
            lines.append(f"### [{mem.type}] {mem.description}")
            lines.append(body_preview)
            lines.append("")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
