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
    )
