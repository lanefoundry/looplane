"""Minimal native MCP stdio client for tool discovery and calls."""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .contracts import ToolDefinition
from .runtime import bounded_text, sanitized_subprocess_env

MCP_CONFIG_FILE = ".mcp.json"
MCP_TOOL_PREFIX = "mcp__"
MCP_RESOURCE_PREFIX = "mcp_resource__"
MCP_PROMPT_PREFIX = "mcp_prompt__"
MCP_PROTOCOL_VERSION = "2026-07-28"
_SAFE_SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class McpError(RuntimeError):
    """User-visible MCP client failure."""


class NativeMcpServerConfig(BaseModel):
    """One allowlisted stdio MCP server from project config."""

    name: str = Field(min_length=1)
    command: str = Field(min_length=1)
    args: tuple[str, ...] = ()
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not _SAFE_SERVER_NAME.fullmatch(value):
            raise ValueError("MCP server names may contain only letters, digits, _ and -")
        return value

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: str) -> str:
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("MCP command must be non-empty and NUL-free")
        return value

    @field_validator("args")
    @classmethod
    def validate_args(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not isinstance(arg, str) or "\x00" in arg for arg in value):
            raise ValueError("MCP args must be strings without NUL")
        return value

    @field_validator("env")
    @classmethod
    def validate_env(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if not key or "\x00" in key or "\x00" in item:
                raise ValueError("MCP env keys and values must be NUL-free")
        return value


def allowlist_from_env(value: str | None = None) -> frozenset[str]:
    """Parse the native MCP server allowlist from ``RIVUMI_MCP_ALLOWLIST``."""

    raw = os.environ.get("RIVUMI_MCP_ALLOWLIST", "") if value is None else value
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def load_native_mcp_server_configs(
    repository: Path,
    *,
    allowlist: Iterable[str] | None = None,
) -> tuple[NativeMcpServerConfig, ...]:
    """Load allowlisted stdio MCP server configs from ``.mcp.json``.

    The default allowlist is empty, so project config never spawns a process
    unless the operator explicitly opts into named servers.
    """

    allowed = frozenset(allowlist_from_env() if allowlist is None else allowlist)
    if not allowed:
        return ()
    config_path = repository / MCP_CONFIG_FILE
    if not config_path.is_file():
        return ()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise McpError(f"could not read {MCP_CONFIG_FILE}: {exc}") from exc
    servers = payload.get("mcpServers") if isinstance(payload, Mapping) else None
    if not isinstance(servers, Mapping):
        raise McpError(f"{MCP_CONFIG_FILE} must contain an object mcpServers field")
    configs: list[NativeMcpServerConfig] = []
    for name, server_payload in servers.items():
        if name not in allowed:
            continue
        if not isinstance(server_payload, Mapping):
            raise McpError(f"MCP server {name!r} must be an object")
        config = NativeMcpServerConfig(name=str(name), **dict(server_payload))
        if config.enabled:
            configs.append(config)
    return tuple(configs)


def native_mcp_tool_name(server_name: str, tool_name: str) -> str:
    return f"{MCP_TOOL_PREFIX}{server_name}__{tool_name}"


def split_native_mcp_tool_name(tool_name: str) -> tuple[str, str] | None:
    if not tool_name.startswith(MCP_TOOL_PREFIX):
        return None
    rest = tool_name[len(MCP_TOOL_PREFIX) :]
    server_name, separator, remote_tool = rest.partition("__")
    if not separator or not server_name or not remote_tool:
        return None
    return server_name, remote_tool


def native_mcp_resource_tool_name(server_name: str, operation: str) -> str:
    return f"{MCP_RESOURCE_PREFIX}{server_name}__{operation}"


def native_mcp_prompt_tool_name(server_name: str, operation: str) -> str:
    return f"{MCP_PROMPT_PREFIX}{server_name}__{operation}"


class StdioMcpClient:
    """Small line-delimited JSON-RPC client for MCP tool calls."""

    def __init__(
        self,
        config: NativeMcpServerConfig,
        *,
        cwd: Path,
        task_home: Path,
        max_output_chars: int = 200_000,
    ) -> None:
        self.config = config
        self.cwd = cwd
        self.task_home = task_home
        self.max_output_chars = max_output_chars
        self._process: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._lock = threading.RLock()
        self._initialized = False

    def close(self) -> None:
        process = self._process
        self._process = None
        self._initialized = False
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    def tool_definitions(self, *, timeout_seconds: float = 10.0) -> tuple[ToolDefinition, ...]:
        tools = self._request_paginated_tools(timeout_seconds=timeout_seconds)
        definitions: list[ToolDefinition] = []
        for tool in tools:
            if not isinstance(tool, Mapping):
                continue
            remote_name = str(tool.get("name") or "")
            if not remote_name:
                continue
            input_schema = tool.get("inputSchema")
            if not isinstance(input_schema, dict):
                input_schema = {"type": "object", "properties": {}, "additionalProperties": True}
            definitions.append(
                ToolDefinition(
                    name=native_mcp_tool_name(self.config.name, remote_name),
                    description=(
                        f"MCP tool {remote_name!r} from server {self.config.name!r}. "
                        f"{str(tool.get('description') or '').strip()}"
                    ).strip(),
                    input_schema=input_schema,
                )
            )
        return tuple(definitions)

    def call_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float = 30.0,
    ) -> tuple[bool, str, str | None]:
        payload = self._request(
            "tools/call",
            {"name": tool_name, "arguments": dict(arguments)},
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(payload, Mapping):
            raise McpError("MCP tools/call returned a non-object result")
        content = self._render_tool_result(payload)
        is_error = bool(payload.get("isError", False))
        return (not is_error, content, "MCP tool returned isError=true" if is_error else None)

    def list_resources(self, *, timeout_seconds: float = 10.0) -> str:
        resources = self._request_paginated_items(
            method="resources/list",
            key="resources",
            timeout_seconds=timeout_seconds,
        )
        return bounded_text(
            json.dumps(resources, ensure_ascii=False, indent=2, sort_keys=True),
            self.max_output_chars,
        )

    def read_resource(self, uri: str, *, timeout_seconds: float = 30.0) -> str:
        if not isinstance(uri, str) or not uri:
            raise McpError("MCP resource uri must be a non-empty string")
        payload = self._request(
            "resources/read",
            {"uri": uri},
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(payload, Mapping):
            raise McpError("MCP resources/read returned a non-object result")
        return self._render_resource_result(payload)

    def list_prompts(self, *, timeout_seconds: float = 10.0) -> str:
        prompts = self._request_paginated_items(
            method="prompts/list",
            key="prompts",
            timeout_seconds=timeout_seconds,
        )
        return bounded_text(
            json.dumps(prompts, ensure_ascii=False, indent=2, sort_keys=True),
            self.max_output_chars,
        )

    def get_prompt(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float = 30.0,
    ) -> str:
        if not isinstance(name, str) or not name:
            raise McpError("MCP prompt name must be a non-empty string")
        if arguments is not None and not isinstance(arguments, Mapping):
            raise McpError("MCP prompt arguments must be an object")
        payload = self._request(
            "prompts/get",
            {"name": name, "arguments": dict(arguments or {})},
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(payload, Mapping):
            raise McpError("MCP prompts/get returned a non-object result")
        return self._render_prompt_result(payload)

    def _request_paginated_tools(self, *, timeout_seconds: float) -> list[Mapping[str, Any]]:
        return self._request_paginated_items(
            method="tools/list",
            key="tools",
            timeout_seconds=timeout_seconds,
        )

    def _request_paginated_items(
        self,
        *,
        method: str,
        key: str,
        timeout_seconds: float,
    ) -> list[Mapping[str, Any]]:
        cursor: str | None = None
        items: list[Mapping[str, Any]] = []
        while True:
            params = {"cursor": cursor} if cursor else None
            payload = self._request(method, params, timeout_seconds=timeout_seconds)
            if not isinstance(payload, Mapping):
                raise McpError(f"MCP {method} returned a non-object result")
            page_items = payload.get(key)
            if not isinstance(page_items, Sequence) or isinstance(page_items, (str, bytes)):
                raise McpError(f"MCP {method} result must contain a {key} array")
            items.extend(item for item in page_items if isinstance(item, Mapping))
            next_cursor = payload.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return items
            cursor = next_cursor

    def _start(self) -> None:
        if self._process is not None:
            return
        env = sanitized_subprocess_env(task_home=self.task_home)
        env.update(self.config.env)
        try:
            self._process = subprocess.Popen(
                (self.config.command, *self.config.args),
                cwd=self.cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
        except OSError as exc:
            raise McpError(f"could not start MCP server {self.config.name!r}: {exc}") from exc

    def _initialize(self, *, timeout_seconds: float) -> None:
        if self._initialized:
            return
        self._start()
        self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "rivumi", "version": "0"},
            },
            timeout_seconds=timeout_seconds,
            require_initialized=False,
        )
        self._notify("notifications/initialized")
        self._initialized = True

    def _request(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        *,
        timeout_seconds: float,
        require_initialized: bool = True,
    ) -> Any:
        with self._lock:
            if require_initialized:
                self._initialize(timeout_seconds=timeout_seconds)
            self._start()
            process = self._process
            if process is None or process.stdin is None or process.stdout is None:
                raise McpError(f"MCP server {self.config.name!r} has no stdio pipes")
            request_id = self._next_id
            self._next_id += 1
            request: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
            }
            if params is not None:
                request["params"] = dict(params)
            self._write_message(request)
            deadline = time.monotonic() + timeout_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.close()
                    raise McpError(f"MCP {method} timed out after {timeout_seconds:g} seconds")
                selector = selectors.DefaultSelector()
                try:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    if not selector.select(timeout=remaining):
                        self.close()
                        raise McpError(
                            f"MCP {method} timed out after {timeout_seconds:g} seconds"
                        )
                finally:
                    selector.close()
                line = process.stdout.readline()
                if line == "":
                    stderr = ""
                    if process.stderr is not None:
                        stderr = process.stderr.read()
                    raise McpError(
                        f"MCP server {self.config.name!r} closed stdout"
                        + (f": {bounded_text(stderr, 1_000)}" if stderr else "")
                    )
                try:
                    response = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    raise McpError(f"MCP {method} failed: {response['error']}")
                return response.get("result")

    def _notify(self, method: str) -> None:
        self._start()
        self._write_message({"jsonrpc": "2.0", "method": method})

    def _write_message(self, payload: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise McpError(f"MCP server {self.config.name!r} has no stdin")
        process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def _render_tool_result(self, payload: Mapping[str, Any]) -> str:
        rendered: list[str] = []
        content = payload.get("content", ())
        if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    rendered.append(block["text"])
                else:
                    rendered.append(json.dumps(block, ensure_ascii=False, sort_keys=True))
        structured = payload.get("structuredContent")
        if structured is not None:
            rendered.append(json.dumps(structured, ensure_ascii=False, sort_keys=True))
        return bounded_text("\n".join(rendered), self.max_output_chars)

    def _render_resource_result(self, payload: Mapping[str, Any]) -> str:
        rendered: list[str] = []
        contents = payload.get("contents", ())
        if not isinstance(contents, Sequence) or isinstance(contents, (str, bytes)):
            raise McpError("MCP resources/read result must contain a contents array")
        for block in contents:
            if not isinstance(block, Mapping):
                continue
            uri = str(block.get("uri") or "")
            mime_type = str(block.get("mimeType") or "application/octet-stream")
            header = f"# {uri}" if uri else "# resource"
            if isinstance(block.get("text"), str):
                rendered.append(f"{header} ({mime_type})\n{block['text']}")
            elif isinstance(block.get("blob"), str):
                rendered.append(f"{header} ({mime_type})\n[blob bytes={len(block['blob'])}]")
            else:
                rendered.append(json.dumps(block, ensure_ascii=False, sort_keys=True))
        return bounded_text("\n\n".join(rendered), self.max_output_chars)

    def _render_prompt_result(self, payload: Mapping[str, Any]) -> str:
        rendered: list[str] = []
        description = payload.get("description")
        if isinstance(description, str) and description:
            rendered.append(description)
        messages = payload.get("messages", ())
        if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
            raise McpError("MCP prompts/get result must contain a messages array")
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            rendered.append(json.dumps(message, ensure_ascii=False, sort_keys=True))
        return bounded_text("\n".join(rendered), self.max_output_chars)
