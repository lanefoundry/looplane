"""Pure Codex tool descriptions and completion values."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from looplane.approvals import ToolEffect
from looplane.conversation_runtime import (
    ConversationProtocolError,
    RuntimeToolKind,
    RuntimeToolStatus,
)


def tool_description(
    item_type: str, item: dict[str, Any]
) -> tuple[
    RuntimeToolKind,
    str,
    ToolEffect,
    str,
    str | None,
    tuple[str, ...],
]:
    if item_type == "commandExecution":
        command = item.get("command")
        if not isinstance(command, str):
            raise ConversationProtocolError("command item has no command")
        cwd = item.get("cwd")
        return (
            RuntimeToolKind.COMMAND,
            "shell",
            ToolEffect.EXECUTE,
            command[:16000],
            cwd[:4096] if isinstance(cwd, str) else None,
            (),
        )
    if item_type == "fileChange":
        changes = item.get("changes")
        if not isinstance(changes, list):
            raise ConversationProtocolError("file change item is malformed")
        paths = [change.get("path") for change in changes if isinstance(change, dict)]
        safe_paths = tuple(
            dict.fromkeys(path[:4096] for path in paths if isinstance(path, str) and path)
        )
        path = safe_paths[0][:4096] if safe_paths else None
        summary = f"{len(changes)} file change(s)"
        return (
            RuntimeToolKind.FILE_CHANGE,
            "file_change",
            ToolEffect.MODIFY,
            summary,
            path,
            safe_paths,
        )
    if item_type == "mcpToolCall":
        server, tool = item.get("server"), item.get("tool")
        if not isinstance(server, str) or not isinstance(tool, str):
            raise ConversationProtocolError("MCP item is malformed")
        return (
            RuntimeToolKind.MCP,
            f"{server}/{tool}"[:256],
            ToolEffect.EXECUTE,
            "",
            None,
            (),
        )
    if item_type == "dynamicToolCall":
        tool = item.get("tool")
        if not isinstance(tool, str):
            raise ConversationProtocolError("dynamic tool item is malformed")
        return RuntimeToolKind.MCP, tool[:256], ToolEffect.EXECUTE, "", None, ()
    if item_type == "collabAgentToolCall":
        return RuntimeToolKind.AGENT, "agent", ToolEffect.EXECUTE, "", None, ()
    return RuntimeToolKind.WEB, "web_search", ToolEffect.EXECUTE, "", None, ()


def tool_status(raw: object) -> RuntimeToolStatus:
    mapping = {
        "completed": RuntimeToolStatus.COMPLETED,
        "failed": RuntimeToolStatus.FAILED,
        "declined": RuntimeToolStatus.DECLINED,
        "interrupted": RuntimeToolStatus.INTERRUPTED,
    }
    if raw not in mapping:
        raise ConversationProtocolError("tool completed with an invalid status")
    return mapping[raw]


def tool_completion_summary(
    item_type: str, item: dict[str, Any], *, bounded: Callable[[str], str]
) -> str:
    if item_type == "commandExecution":
        exit_code = item.get("exitCode")
        return f"exit {exit_code}" if isinstance(exit_code, int) else "command finished"
    status = item.get("status")
    return bounded(status)[:16000] if isinstance(status, str) else "tool finished"


def tool_completion_output(
    item_type: str, item: dict[str, Any], *, bounded: Callable[[str], str]
) -> str | None:
    if item_type != "commandExecution":
        return None
    output = item.get("aggregatedOutput")
    return bounded(output)[:64000] if isinstance(output, str) else None


def tool_completion_diff(
    item_type: str, item: dict[str, Any], *, bounded: Callable[[str], str]
) -> str | None:
    if item_type != "fileChange":
        return None
    changes = item.get("changes")
    if not isinstance(changes, list):
        return None
    diffs = [change.get("diff") for change in changes if isinstance(change, dict)]
    rendered = "\n".join(diff for diff in diffs if isinstance(diff, str))
    if not rendered:
        return None
    return bounded(rendered)[:64000]
