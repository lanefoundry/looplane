"""LSP-based semantic tools: symbols, references, definition, diagnostics."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from looplane.lsp import LspSupervisorError, ManagedLspServer
from looplane.tooling.types import ToolExecutionError

_MAX_RESULTS = 50


def _uri_to_relpath(uri: str, project_root: Path) -> str:
    parsed = urlparse(uri)
    file_path = Path(unquote(parsed.path))
    try:
        return str(file_path.relative_to(project_root))
    except ValueError:
        return str(file_path)


def _format_location(loc: dict[str, Any], project_root: Path) -> str:
    uri = loc.get("uri", "")
    rng = loc.get("range", {})
    start = rng.get("start", {})
    line = start.get("line", 0) + 1
    col = start.get("character", 0) + 1
    path = _uri_to_relpath(uri, project_root)
    return f"{path}:{line}:{col}"


async def lsp_symbols(
    server: ManagedLspServer,
    query: str,
    path: str | None = None,
) -> str:
    if not server.running:
        raise ToolExecutionError("LSP server is not running")

    await server.initialize()

    if path:
        file_path = server.project_root / path
        if not file_path.is_file():
            raise ToolExecutionError(f"file not found: {path}")
        await server.open_document(file_path)
        result = await server.request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": file_path.resolve().as_uri()}},
        )
    else:
        result = await server.request("workspace/symbol", {"query": query})

    if not result:
        return f"No symbols found for: {query}"

    symbols = result if isinstance(result, list) else []
    lines = [f"# Symbols matching: {query}\n"]
    for sym in symbols[:_MAX_RESULTS]:
        name = sym.get("name", "?")
        kind = _symbol_kind(sym.get("kind", 0))
        loc = sym.get("location", sym)
        location_str = _format_location(loc, server.project_root)
        lines.append(f"  {kind} {name}  ({location_str})")

    if len(symbols) > _MAX_RESULTS:
        lines.append(f"\n  ... and {len(symbols) - _MAX_RESULTS} more")
    return "\n".join(lines)


async def lsp_references(
    server: ManagedLspServer,
    path: str,
    line: int,
    character: int,
) -> str:
    if not server.running:
        raise ToolExecutionError("LSP server is not running")

    await server.initialize()

    file_path = server.project_root / path
    if not file_path.is_file():
        raise ToolExecutionError(f"file not found: {path}")

    await server.open_document(file_path)
    result = await server.request(
        "textDocument/references",
        {
            "textDocument": {"uri": file_path.resolve().as_uri()},
            "position": {"line": line - 1, "character": character - 1},
            "context": {"includeDeclaration": True},
        },
    )

    if not result:
        return f"No references found at {path}:{line}:{character}"

    refs = result if isinstance(result, list) else []
    lines = [f"# References at {path}:{line}:{character}\n"]
    for ref in refs[:_MAX_RESULTS]:
        lines.append(f"  {_format_location(ref, server.project_root)}")

    if len(refs) > _MAX_RESULTS:
        lines.append(f"\n  ... and {len(refs) - _MAX_RESULTS} more")
    return "\n".join(lines)


async def lsp_definition(
    server: ManagedLspServer,
    path: str,
    line: int,
    character: int,
) -> str:
    if not server.running:
        raise ToolExecutionError("LSP server is not running")

    await server.initialize()

    file_path = server.project_root / path
    if not file_path.is_file():
        raise ToolExecutionError(f"file not found: {path}")

    await server.open_document(file_path)
    result = await server.request(
        "textDocument/definition",
        {
            "textDocument": {"uri": file_path.resolve().as_uri()},
            "position": {"line": line - 1, "character": character - 1},
        },
    )

    if not result:
        return f"No definition found at {path}:{line}:{character}"

    defs = result if isinstance(result, list) else [result]
    lines = [f"# Definition of symbol at {path}:{line}:{character}\n"]
    for d in defs:
        lines.append(f"  {_format_location(d, server.project_root)}")
    return "\n".join(lines)


async def lsp_diagnostics(
    server: ManagedLspServer,
    path: str | None = None,
) -> str:
    if not server.running:
        raise ToolExecutionError("LSP server is not running")

    if path:
        file_path = server.project_root / path
        if not file_path.is_file():
            raise ToolExecutionError(f"file not found: {path}")
        await server.initialize()
        await server.open_document(file_path)

    snapshot = server.last_diagnostics
    if snapshot is None:
        try:
            snapshot = await server.wait_for_diagnostics(timeout_seconds=10.0)
        except (TimeoutError, LspSupervisorError):
            return "No diagnostics available (timed out waiting for LSP server)"

    lines = ["# LSP Diagnostics\n"]
    entries = snapshot.entries if hasattr(snapshot, "entries") else []
    if not entries:
        return "No diagnostics reported."

    for entry in entries[:_MAX_RESULTS]:
        severity = getattr(entry, "severity", "info")
        message = getattr(entry, "message", "")
        file_str = getattr(entry, "path", "?")
        line_num = getattr(entry, "line", 0)
        lines.append(f"  [{severity}] {file_str}:{line_num}: {message}")

    return "\n".join(lines)


_SYMBOL_KINDS = {
    1: "File",
    2: "Module",
    3: "Namespace",
    4: "Package",
    5: "Class",
    6: "Method",
    7: "Property",
    8: "Field",
    9: "Constructor",
    10: "Enum",
    11: "Interface",
    12: "Function",
    13: "Variable",
    14: "Constant",
    15: "String",
    16: "Number",
    17: "Boolean",
    18: "Array",
    19: "Object",
    20: "Key",
    21: "Null",
    22: "EnumMember",
    23: "Struct",
    24: "Event",
    25: "Operator",
    26: "TypeParameter",
}


def _symbol_kind(kind: int) -> str:
    return _SYMBOL_KINDS.get(kind, f"Kind({kind})")
