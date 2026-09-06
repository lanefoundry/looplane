from __future__ import annotations

from ..contracts import ToolDefinition


def tool_definitions() -> tuple[ToolDefinition, ...]:
    path = {"type": "string", "description": "Workspace-relative path."}
    return (
        ToolDefinition(
            name="list_files",
            description=(
                "List allowed files below a workspace-relative path. Use this to discover "
                "file names before reading. It is read-only and bounded; do not use it when "
                "you already know the exact file and can call read_file directly."
            ),
            input_schema={
                "type": "object",
                "properties": {"path": {**path, "default": "."}},
                "additionalProperties": False,
            },
            read_only=True,
            concurrency_safe=True,
        ),
        ToolDefinition(
            name="read_file",
            description=(
                "Read one allowed UTF-8 text file with a bounded result. Returns numbered "
                "lines in 'LINE\\tCONTENT' format. Use offset and limit to read a specific "
                "line range instead of the whole file — first search_text to find the line "
                "number, then read_file with offset and limit to read just that region. "
                "Use this before replace_text and whenever exact source text matters."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": path,
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "0-based starting line. Omit to start at the top.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Max lines to return. Omit to read the whole file.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            read_only=True,
            concurrency_safe=True,
        ),
        ToolDefinition(
            name="search_text",
            description=(
                "Search allowed files for a text pattern, respecting .gitignore when "
                "ripgrep is available. Returns bounded path:line:text matches. "
                "Default is literal search; set regex to true for pattern matching."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "path": {**path, "default": "."},
                    "glob": {"type": ["string", "null"]},
                    "case_sensitive": {"type": "boolean", "default": True},
                    "regex": {"type": "boolean", "default": False},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            read_only=True,
            concurrency_safe=True,
        ),
        ToolDefinition(
            name="replace_text",
            description=(
                "Replace an exact text fragment in one existing UTF-8 file. Read the file "
                "first. Prefer this for small edits; old_text must occur exactly once. "
                "Correct example: copy old_text directly from read_file, preserving spaces "
                "and newlines. Do not use it for new files, deletions, multi-hunk edits, or "
                "guessed text."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": path,
                    "old_text": {"type": "string", "minLength": 1},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="create_file",
            description=(
                "Create one new UTF-8 text file from structured path and content arguments. "
                "The path must not already exist. Prefer this over hand-writing a unified "
                "diff for a new file; the harness generates and validates the diff."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": path,
                    "content": {"type": "string", "minLength": 1},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="apply_patch",
            description=(
                "Apply one bounded unified text diff after path and git checks. Use this "
                "for multi-hunk edits, new files, and deletions. The patch must include "
                "diff --git, ---/+++ file headers, and @@ hunks; do not use it for a small "
                "single exact replacement where replace_text is safer."
            ),
            input_schema={
                "type": "object",
                "properties": {"patch": {"type": "string", "minLength": 1}},
                "required": ["patch"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="shell",
            description=(
                "Run a shell command in the workspace. Use it for exploration "
                "(grep, find, git log, wc) and verification (pytest, linters). "
                "Dangerous commands are denied; suspicious ones need approval. "
                "Output is bounded and secrets are redacted."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Shell command to execute.",
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="git_diff",
            description=(
                "Return the bounded uncommitted workspace patch for review. Use it after "
                "edits when you need to inspect the cumulative diff; it is read-only."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            read_only=True,
            concurrency_safe=True,
        ),
        ToolDefinition(
            name="tool_program",
            description=(
                "Execute a bounded read-only tool program in one model tool call. Each step "
                "must use op list_files, read_file, search_text, git_diff, repeat, or "
                "if_contains with normal tool arguments. Use this for small planned "
                "inspection batches; it cannot edit files, run checks, or call MCP tools."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "properties": {
                                "op": {
                                    "type": "string",
                                    "enum": [
                                        "list_files",
                                        "read_file",
                                        "search_text",
                                        "git_diff",
                                        "repeat",
                                        "if_contains",
                                    ],
                                },
                                "args": {
                                    "type": "object",
                                    "default": {},
                                    "additionalProperties": True,
                                },
                                "count": {"type": "integer", "minimum": 1, "maximum": 8},
                                "contains": {"type": "string"},
                                "steps": {"type": "array", "items": {"type": "object"}},
                                "then_steps": {"type": "array", "items": {"type": "object"}},
                                "else_steps": {"type": "array", "items": {"type": "object"}},
                            },
                            "required": ["op"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["steps"],
                "additionalProperties": False,
            },
            read_only=True,
        ),
        ToolDefinition(
            name="tool_transaction",
            description=(
                "Execute a bounded modify/check transaction. Steps may read files, create one "
                "new file, apply an exact replacement or unified diff, run a check, or inspect "
                "git_diff. repeat and if_contains provide bounded control flow. If any step "
                "fails, files touched by create_file/replace_text/apply_patch are restored to "
                "their "
                "pre-transaction state. Use this when an edit and its check must succeed or "
                "fail as one unit; it requires modify+execute approval."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "properties": {
                                "op": {
                                    "type": "string",
                                    "enum": [
                                        "read_file",
                                        "create_file",
                                        "replace_text",
                                        "apply_patch",
                                        "shell",
                                        "git_diff",
                                        "repeat",
                                        "if_contains",
                                    ],
                                },
                                "args": {
                                    "type": "object",
                                    "default": {},
                                    "additionalProperties": True,
                                },
                                "count": {"type": "integer", "minimum": 1, "maximum": 8},
                                "contains": {"type": "string"},
                                "steps": {"type": "array", "items": {"type": "object"}},
                                "then_steps": {"type": "array", "items": {"type": "object"}},
                                "else_steps": {"type": "array", "items": {"type": "object"}},
                            },
                            "required": ["op"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["steps"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="web_fetch",
            description=(
                "Fetch a web page and extract its text content as markdown. Use this to "
                "read online documentation, API references, changelogs, or any public URL. "
                "Returns cleaned text; use selector to extract a specific part of the page. "
                "Cannot access localhost or private networks."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "minLength": 1, "description": "Target URL."},
                    "selector": {
                        "type": ["string", "null"],
                        "description": "CSS selector to extract a specific section.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "default": 16000,
                        "minimum": 1000,
                        "maximum": 32000,
                        "description": "Max characters to return.",
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            read_only=True,
            concurrency_safe=True,
        ),
        ToolDefinition(
            name="web_search",
            description=(
                "Search the web and return structured results. Use this when you need to "
                "find documentation, examples, or answers but do not know the exact URL."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "max_results": {
                        "type": "integer",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 10,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            read_only=True,
            concurrency_safe=True,
        ),
        ToolDefinition(
            name="http_request",
            description=(
                "Send an HTTP request and return the response. Use this to call REST APIs, "
                "webhooks, or GraphQL endpoints. Supports GET, POST, PUT, PATCH, DELETE. "
                "Cannot access localhost or private networks."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                    },
                    "url": {"type": "string", "minLength": 1},
                    "headers": {
                        "type": ["object", "null"],
                        "additionalProperties": {"type": "string"},
                        "description": "Custom request headers.",
                    },
                    "body": {
                        "type": ["string", "null"],
                        "description": "Request body.",
                    },
                    "timeout": {
                        "type": "integer",
                        "default": 30,
                        "minimum": 1,
                        "maximum": 60,
                        "description": "Request timeout in seconds.",
                    },
                },
                "required": ["method", "url"],
                "additionalProperties": False,
            },
        ),
    )


def lsp_tool_definitions() -> tuple[ToolDefinition, ...]:
    """LSP semantic tools — only registered when an LSP server is configured."""
    position_params = {
        "path": {"type": "string", "minLength": 1, "description": "Workspace-relative path."},
        "line": {"type": "integer", "minimum": 1, "description": "1-based line number."},
        "character": {
            "type": "integer",
            "minimum": 1,
            "description": "1-based column number.",
        },
    }
    return (
        ToolDefinition(
            name="lsp_symbols",
            description=(
                "Search for code symbols (functions, classes, variables) using the LSP server. "
                "Provide a query to search workspace-wide, or additionally a path to search "
                "within one file. Returns symbol names, kinds, and locations."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "path": {
                        "type": ["string", "null"],
                        "description": "Workspace-relative path to search within one file.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            read_only=True,
            concurrency_safe=True,
        ),
        ToolDefinition(
            name="lsp_references",
            description=(
                "Find all references to the symbol at the given position. Returns a list "
                "of locations where the symbol is used across the workspace."
            ),
            input_schema={
                "type": "object",
                "properties": position_params,
                "required": ["path", "line", "character"],
                "additionalProperties": False,
            },
            read_only=True,
            concurrency_safe=True,
        ),
        ToolDefinition(
            name="lsp_definition",
            description=(
                "Go to the definition of the symbol at the given position. Returns "
                "the location where the symbol is defined."
            ),
            input_schema={
                "type": "object",
                "properties": position_params,
                "required": ["path", "line", "character"],
                "additionalProperties": False,
            },
            read_only=True,
            concurrency_safe=True,
        ),
        ToolDefinition(
            name="lsp_diagnostics",
            description=(
                "Get LSP diagnostics (errors, warnings) for the workspace or a specific file. "
                "Returns the latest diagnostics reported by the language server."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": ["string", "null"],
                        "description": "Workspace-relative path to get diagnostics for.",
                    },
                },
                "additionalProperties": False,
            },
            read_only=True,
            concurrency_safe=True,
        ),
    )


def background_tool_definitions() -> tuple[ToolDefinition, ...]:
    """Background process tools."""
    return (
        ToolDefinition(
            name="start_process",
            description=(
                "Start a background process (e.g. dev server, watch mode). "
                "Max 3 concurrent processes. Returns a process_id for later interaction."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "minLength": 1},
                    "label": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Human-readable label (e.g. 'dev-server').",
                    },
                },
                "required": ["command", "label"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="read_process",
            description=(
                "Read recent output from a background process. "
                "Use pattern to grep-filter the output."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "process_id": {"type": "string", "minLength": 1},
                    "lines": {
                        "type": "integer",
                        "default": 50,
                        "minimum": 1,
                        "maximum": 500,
                    },
                    "pattern": {
                        "type": ["string", "null"],
                        "description": "Regex pattern to filter output lines.",
                    },
                },
                "required": ["process_id"],
                "additionalProperties": False,
            },
            read_only=True,
        ),
        ToolDefinition(
            name="stop_process",
            description="Stop a running background process by its process_id.",
            input_schema={
                "type": "object",
                "properties": {
                    "process_id": {"type": "string", "minLength": 1},
                },
                "required": ["process_id"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="list_processes",
            description="List all background processes and their status.",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            read_only=True,
            concurrency_safe=True,
        ),
        ToolDefinition(
            name="wait_for_output",
            description=(
                "Wait until a background process outputs a line matching a pattern. "
                "Useful for waiting until a dev server is ready. "
                "Times out after timeout_seconds (max 120s)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "process_id": {"type": "string", "minLength": 1},
                    "pattern": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Regex pattern to wait for.",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "default": 60,
                        "minimum": 1,
                        "maximum": 120,
                    },
                },
                "required": ["process_id", "pattern"],
                "additionalProperties": False,
            },
            read_only=True,
        ),
    )


def media_tool_definitions(*, has_playwright: bool = False) -> tuple[ToolDefinition, ...]:
    """Image viewing and screenshot tools."""
    defs: list[ToolDefinition] = [
        ToolDefinition(
            name="view_image",
            description=(
                "View an image file in the workspace. Returns the image for visual "
                "inspection (if the model supports vision) or metadata otherwise. "
                "Supports PNG, JPEG, GIF, WebP, SVG, BMP."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Workspace-relative path to the image.",
                    },
                    "max_dimension": {
                        "type": "integer",
                        "default": 1024,
                        "minimum": 64,
                        "maximum": 2048,
                        "description": "Max width/height to scale to.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            read_only=True,
            concurrency_safe=True,
        ),
    ]
    if has_playwright:
        defs.append(
            ToolDefinition(
                name="take_screenshot",
                description=(
                    "Take a screenshot of a web page. Use with background dev servers "
                    "to visually verify UI changes. Requires Playwright."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "minLength": 1},
                        "selector": {
                            "type": ["string", "null"],
                            "description": "CSS selector to screenshot a specific element.",
                        },
                        "width": {
                            "type": "integer",
                            "default": 1280,
                            "minimum": 320,
                            "maximum": 3840,
                        },
                        "height": {
                            "type": "integer",
                            "default": 720,
                            "minimum": 240,
                            "maximum": 2160,
                        },
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
                read_only=True,
            ),
        )
    return tuple(defs)


def pty_tool_definitions() -> tuple[ToolDefinition, ...]:
    """Interactive PTY session tools."""
    return (
        ToolDefinition(
            name="start_session",
            description=(
                "Start an interactive terminal session (e.g. Python REPL, psql, redis-cli). "
                "Max 2 concurrent sessions. Returns a session_id for sending input."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Command to launch (e.g. 'python3', 'psql').",
                    },
                    "label": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Human-readable label.",
                    },
                },
                "required": ["command", "label"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="send_input",
            description=(
                "Send text input to an interactive session and return new output. "
                "Newline is appended automatically. wait_ms controls how long to wait "
                "for output to stabilize."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "minLength": 1},
                    "text": {"type": "string", "description": "Text to send."},
                    "wait_ms": {
                        "type": "integer",
                        "default": 2000,
                        "minimum": 100,
                        "maximum": 10000,
                        "description": "Milliseconds to wait for output.",
                    },
                },
                "required": ["session_id", "text"],
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="read_session",
            description="Read the output buffer of an interactive session.",
            input_schema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "minLength": 1},
                    "lines": {
                        "type": "integer",
                        "default": 50,
                        "minimum": 1,
                        "maximum": 500,
                    },
                },
                "required": ["session_id"],
                "additionalProperties": False,
            },
            read_only=True,
        ),
        ToolDefinition(
            name="end_session",
            description="End an interactive session and clean up its PTY.",
            input_schema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "minLength": 1},
                },
                "required": ["session_id"],
                "additionalProperties": False,
            },
        ),
    )
