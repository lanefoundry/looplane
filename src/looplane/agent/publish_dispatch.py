"""Native host publication tool; repository identity never comes from model input."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from looplane.contracts import ToolCall, ToolDefinition, ToolObservation, VerificationCommand
from looplane.publishing import publish_changes


def publish_changes_definition() -> ToolDefinition:
    return ToolDefinition(
        name="publish_changes",
        description=(
            "After the user requests publication, verify and push this task's changes to a "
            "branch on a configured source repository remote. Uses an isolated checkout and "
            "the task's configured verification commands. Requires execution approval. "
            "Available in normal local sessions; checks retain the session's sandbox settings. "
            "Does not deploy or force push. Report the actual publication result."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "branch": {"type": "string", "minLength": 1},
                "remote": {"type": "string", "minLength": 1, "default": "origin"},
            },
            "required": ["branch"],
            "additionalProperties": False,
        },
        read_only=False,
        concurrency_safe=False,
    )


def execute_publish_changes(
    call: ToolCall,
    *,
    source_repo: Path,
    workspace: Path,
    base_sha: str,
    verification: tuple[VerificationCommand, ...],
    patch: str,
    timeout_seconds: float,
    sandbox_checks: bool = False,
    sandbox_profile: str | None = None,
    sandbox_backend: str | None = None,
    sandbox_read_roots: tuple[Path, ...] = (),
) -> ToolObservation:
    """Dispatch only trusted host context and tightly scoped branch/remote arguments."""
    try:
        if set(call.arguments) - {"branch", "remote"}:
            raise ValueError("publish_changes accepts only branch and remote arguments")
        branch = call.arguments.get("branch")
        remote = call.arguments.get("remote", "origin")
        if not isinstance(branch, str) or not branch.strip():
            raise ValueError("publish_changes requires a non-empty branch")
        if not isinstance(remote, str) or not remote.strip():
            raise ValueError("publish_changes requires a non-empty configured remote name")
        result: Any = publish_changes(
            source_repo=source_repo,
            workspace=workspace,
            base_sha=base_sha,
            branch=branch,
            remote=remote,
            verification=verification,
            patch=patch,
            timeout_seconds=timeout_seconds,
            sandbox_checks=sandbox_checks,
            sandbox_profile=sandbox_profile,
            sandbox_backend=sandbox_backend,
            sandbox_read_roots=sandbox_read_roots,
        )
        return ToolObservation(
            tool_call_id=call.tool_call_id,
            name=call.name,
            ok=True,
            content=json.dumps(asdict(result), ensure_ascii=False),
        )
    except (ValueError, RuntimeError, OSError) as exc:
        return ToolObservation(
            tool_call_id=call.tool_call_id,
            name=call.name,
            ok=False,
            content="",
            error=str(exc),
        )
