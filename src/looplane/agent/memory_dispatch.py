"""Agent-callable memory tools — save_memory and recall_memory."""

from __future__ import annotations

import textwrap
from pathlib import Path

from looplane.contracts import ToolCall, ToolDefinition, ToolObservation
from looplane.memory import (
    MemoryFileType,
    relevant_memory_files,
    save_memory_file,
)


def save_memory_definition() -> ToolDefinition:
    return ToolDefinition(
        name="save_memory",
        description=(
            "Persist a durable fact to cross-session memory. Use when you discover "
            "something worth remembering for future sessions: project conventions, "
            "user preferences, important file locations, recurring patterns, or "
            "lessons learned. Each memory is a short Markdown note stored in "
            "~/.looplane/memory/."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": "One-line summary used to decide relevance in future sessions.",
                },
                "body": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1500,
                    "description": "The memory content — what to remember and why.",
                },
                "type": {
                    "type": "string",
                    "enum": ["project_fact", "user_preference", "project_preference"],
                    "default": "project_fact",
                    "description": (
                        "project_fact: a fact about this project. "
                        "user_preference: a preference that applies to all projects. "
                        "project_preference: a preference specific to this project."
                    ),
                },
            },
            "required": ["description", "body"],
            "additionalProperties": False,
        },
        read_only=False,
        concurrency_safe=False,
    )


def recall_memory_definition() -> ToolDefinition:
    return ToolDefinition(
        name="recall_memory",
        description=(
            "Search cross-session memories for facts, preferences, or session summaries "
            "relevant to the current project. Returns the most recent memories. Use when "
            "you need context about prior work, user preferences, or project conventions."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 10,
                    "description": "Maximum number of memories to return.",
                },
            },
            "additionalProperties": False,
        },
        read_only=True,
        concurrency_safe=True,
    )


def memory_tool_definitions() -> tuple[ToolDefinition, ...]:
    return (save_memory_definition(), recall_memory_definition())


def execute_save_memory(call: ToolCall, *, project: Path) -> ToolObservation:
    description = call.arguments.get("description", "")
    body = call.arguments.get("body", "")
    memory_type: MemoryFileType = call.arguments.get("type", "project_fact")

    if not isinstance(description, str) or not description.strip():
        return ToolObservation(
            tool_call_id=call.tool_call_id,
            name=call.name,
            ok=False,
            content="",
            error="save_memory requires a non-empty 'description'.",
        )
    if not isinstance(body, str) or not body.strip():
        return ToolObservation(
            tool_call_id=call.tool_call_id,
            name=call.name,
            ok=False,
            content="",
            error="save_memory requires a non-empty 'body'.",
        )
    if memory_type not in ("project_fact", "user_preference", "project_preference"):
        memory_type = "project_fact"

    try:
        mem = save_memory_file(
            memory_type=memory_type,
            description=description.strip(),
            body=body.strip(),
            project=project if memory_type != "user_preference" else None,
        )
    except (OSError, ValueError) as exc:
        return ToolObservation(
            tool_call_id=call.tool_call_id,
            name=call.name,
            ok=False,
            content="",
            error=f"Failed to save memory: {exc}",
        )

    return ToolObservation(
        tool_call_id=call.tool_call_id,
        name=call.name,
        ok=True,
        content=f"Memory saved as {mem.slug}.md ({mem.type}: {mem.description})",
    )


def execute_recall_memory(call: ToolCall, *, project: Path) -> ToolObservation:
    limit = call.arguments.get("limit", 10)
    if not isinstance(limit, int) or limit < 1:
        limit = 10
    limit = min(limit, 20)

    memories = relevant_memory_files(project=project, limit=limit)
    if not memories:
        return ToolObservation(
            tool_call_id=call.tool_call_id,
            name=call.name,
            ok=True,
            content="No memories found for this project.",
        )

    lines: list[str] = [f"Found {len(memories)} memories:\n"]
    for mem in memories:
        body_preview = textwrap.shorten(mem.body, width=400, placeholder="…")
        lines.append(f"## [{mem.type}] {mem.description}")
        lines.append(body_preview)
        lines.append(f"_slug: {mem.slug}, created: {mem.created_at.isoformat()}_")
        lines.append("")

    return ToolObservation(
        tool_call_id=call.tool_call_id,
        name=call.name,
        ok=True,
        content="\n".join(lines),
    )
