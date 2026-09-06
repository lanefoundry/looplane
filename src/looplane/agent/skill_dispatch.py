"""Skill invocation dispatch for the native agent loop.

Parallel to ``subagent_dispatch``: the runner delegates here via a protocol
callback, keeping tool-handler logic out of the loop coordinator.
"""

from __future__ import annotations

from collections.abc import Sequence

from looplane.contracts import ToolCall, ToolDefinition, ToolObservation
from looplane.execution.capture import bounded_text
from looplane.skills import ProjectSkill

MAX_SKILL_RESPONSE_CHARS = 64_000


def invoke_skill_definition(
    skills: Sequence[tuple[str, str]],
) -> ToolDefinition:
    """Build the invoke_skill tool definition from structured (name, description) pairs."""

    description = (
        "Load one project skill by name. Skills provide specialized instructions and "
        "workflows for specific tasks. Use this when a task matches a skill's description; "
        "the full skill body is returned as the tool result. Do not guess skill names — "
        "only use names listed below."
    )
    if skills:
        lines = ["", "Available project skills:"]
        for name, desc in skills:
            entry = f"  - {name}"
            if desc:
                entry += f" - {desc}"
            lines.append(entry)
        description += "\n".join(lines)
    return ToolDefinition(
        name="invoke_skill",
        description=description,
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Exact name of the skill to load.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        read_only=True,
        concurrency_safe=True,
    )


def execute_invoke_skill(
    call: ToolCall,
    skills: Sequence[ProjectSkill],
) -> ToolObservation:
    """Synchronous skill lookup and body expansion."""

    name = call.arguments.get("name", "")
    if not isinstance(name, str) or not name:
        return ToolObservation(
            tool_call_id=call.tool_call_id,
            name=call.name,
            ok=False,
            content="",
            error="invoke_skill requires a non-empty 'name' argument.",
        )
    skill = next((s for s in skills if s.name == name), None)
    if skill is None:
        available = ", ".join(s.name for s in skills)
        return ToolObservation(
            tool_call_id=call.tool_call_id,
            name=call.name,
            ok=False,
            content="",
            error=f"Unknown skill: {name!r}. Available: {available}",
        )
    header = f"Skill: {skill.name}"
    if skill.description:
        header += f" — {skill.description}"
    content = f"{header}\n\n{skill.body.strip()}"
    return ToolObservation(
        tool_call_id=call.tool_call_id,
        name=call.name,
        ok=True,
        content=bounded_text(content, MAX_SKILL_RESPONSE_CHARS),
        error=None,
    )
