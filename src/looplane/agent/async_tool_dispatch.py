"""Async tool dispatch for background processes, PTY sessions, and LSP queries.

These tools are inherently async or stateful and cannot run inside the
synchronous ToolExecutor.execute() path. The tool_scheduler routes them
here before reaching the executor, following the same pattern as
invoke_skill and dispatch_subagents.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from looplane.contracts import ToolCall, ToolDefinition, ToolObservation
from looplane.execution.capture import bounded_text
from looplane.tooling.background import BackgroundProcessManager
from looplane.tooling.lsp_tools import (
    lsp_definition,
    lsp_diagnostics,
    lsp_references,
    lsp_symbols,
)
from looplane.tooling.pty_session import PtySessionManager
from looplane.tooling.types import ToolExecutionError
from looplane.tooling.validation import (
    ValidationError,
    build_schema_index,
    validate_and_sanitize,
)

_ASYNC_TOOL_NAMES = frozenset(
    {
        "start_process",
        "read_process",
        "stop_process",
        "list_processes",
        "wait_for_output",
        "start_session",
        "send_input",
        "read_session",
        "end_session",
        "lsp_symbols",
        "lsp_references",
        "lsp_definition",
        "lsp_diagnostics",
    }
)


def is_async_tool(name: str) -> bool:
    return name in _ASYNC_TOOL_NAMES


class AsyncToolDispatch:
    """Dispatch async tools to their respective managers."""

    def __init__(
        self,
        *,
        background: BackgroundProcessManager | None = None,
        pty: PtySessionManager | None = None,
        lsp_server: Any | None = None,
        max_output_chars: int = 200_000,
        definitions: Sequence[ToolDefinition] = (),
    ) -> None:
        self._background = background
        self._pty = pty
        self._lsp_server = lsp_server
        self._max_output_chars = max_output_chars
        self._schema_index = build_schema_index(definitions)

    async def execute(self, call: ToolCall) -> ToolObservation:
        name = call.name
        schema = self._schema_index.get(name)
        try:
            if schema:
                args = validate_and_sanitize(name, call.arguments, schema)
            else:
                args = dict(call.arguments)
            result = await self._dispatch(name, args)
            content = bounded_text(result, self._max_output_chars)
            return ToolObservation(
                tool_call_id=call.tool_call_id,
                name=name,
                ok=True,
                content=content,
                error=None,
            )
        except (ToolExecutionError, ValidationError, OSError, TypeError) as exc:
            error = bounded_text(f"{type(exc).__name__}: {exc}", self._max_output_chars)
            return ToolObservation(
                tool_call_id=call.tool_call_id,
                name=name,
                ok=False,
                content="",
                error=error,
            )

    async def _dispatch(self, name: str, args: dict[str, Any]) -> str:
        if name in {
            "start_process",
            "read_process",
            "stop_process",
            "list_processes",
            "wait_for_output",
        }:
            return await self._dispatch_background(name, args)
        if name in {"start_session", "send_input", "read_session", "end_session"}:
            return await self._dispatch_pty(name, args)
        if name in {"lsp_symbols", "lsp_references", "lsp_definition", "lsp_diagnostics"}:
            return await self._dispatch_lsp(name, args)
        raise ToolExecutionError(f"unknown async tool: {name}")

    async def _dispatch_background(self, name: str, args: dict[str, Any]) -> str:
        if self._background is None:
            raise ToolExecutionError("background process manager is not available")
        bg = self._background
        if name == "start_process":
            return await bg.start_process(args["command"], args["label"])
        if name == "read_process":
            return bg.read_process(
                args["process_id"],
                lines=args.get("lines", 50),
                pattern=args.get("pattern"),
            )
        if name == "stop_process":
            return await bg.stop_process(args["process_id"])
        if name == "list_processes":
            return bg.list_processes()
        if name == "wait_for_output":
            return await bg.wait_for_output(
                args["process_id"],
                args["pattern"],
                timeout_seconds=args.get("timeout_seconds", 60),
            )
        raise ToolExecutionError(f"unknown background tool: {name}")

    async def _dispatch_pty(self, name: str, args: dict[str, Any]) -> str:
        if self._pty is None:
            raise ToolExecutionError("PTY session manager is not available")
        pty = self._pty
        if name == "start_session":
            return await pty.start_session(args["command"], args["label"])
        if name == "send_input":
            return await pty.send_input(
                args["session_id"],
                args["text"],
                wait_ms=args.get("wait_ms", 2000),
            )
        if name == "read_session":
            return pty.read_session(
                args["session_id"],
                lines=args.get("lines", 50),
            )
        if name == "end_session":
            return await pty.end_session(args["session_id"])
        raise ToolExecutionError(f"unknown PTY tool: {name}")

    async def _dispatch_lsp(self, name: str, args: dict[str, Any]) -> str:
        if self._lsp_server is None:
            raise ToolExecutionError("LSP server is not configured")
        server = self._lsp_server
        if name == "lsp_symbols":
            return await lsp_symbols(server, args["query"], path=args.get("path"))
        if name == "lsp_references":
            return await lsp_references(server, args["path"], args["line"], args["character"])
        if name == "lsp_definition":
            return await lsp_definition(server, args["path"], args["line"], args["character"])
        if name == "lsp_diagnostics":
            return await lsp_diagnostics(server, path=args.get("path"))
        raise ToolExecutionError(f"unknown LSP tool: {name}")

    async def cleanup(self) -> None:
        if self._background is not None:
            await self._background.cleanup()
        if self._pty is not None:
            await self._pty.cleanup()
