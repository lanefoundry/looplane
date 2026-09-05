"""MCP discovery ownership independent of tool dispatch and process execution."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from looplane.contracts import ToolDefinition

if TYPE_CHECKING:
    from looplane.mcp_client import NativeMcpServerConfig


class McpClient(Protocol):
    """Client operations consumed by discovery and the executor's MCP dispatch."""

    @property
    def config(self) -> NativeMcpServerConfig: ...

    def tool_definitions(self, *, timeout_seconds: float = 10.0) -> tuple[ToolDefinition, ...]: ...

    def close(self) -> None: ...

    def call_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float = 30.0,
    ) -> tuple[bool, str, str | None]: ...

    def list_resources(self, *, timeout_seconds: float = 10.0) -> str: ...

    def read_resource(self, uri: str, *, timeout_seconds: float = 30.0) -> str: ...

    def list_prompts(self, *, timeout_seconds: float = 10.0) -> str: ...

    def get_prompt(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float = 30.0,
    ) -> str: ...


@dataclass(frozen=True)
class McpToolNames:
    """Existing namespace functions supplied without importing concrete clients."""

    resource: Callable[[str, str], str]
    prompt: Callable[[str, str], str]
    split_tool: Callable[[str], tuple[str, str] | None]


class McpBridge:
    """Own configured clients, discovery definitions and live dispatch mappings.

    Construction consumes the client factory without retaining it, so a bound
    executor construction method does not make this owner retain its executor.
    Discovery intentionally rebuilds mappings in place and lets errors propagate;
    a failed discovery retains the established partial-mapping behavior.
    """

    def __init__(
        self,
        configs: Sequence[NativeMcpServerConfig],
        *,
        client_factory: Callable[[NativeMcpServerConfig], McpClient],
        names: McpToolNames,
    ) -> None:
        self.configs = tuple(configs)
        self.names = names
        self.clients: dict[str, McpClient] = {
            config.name: client_factory(config) for config in self.configs
        }
        self.tools: dict[str, tuple[McpClient, str]] = {}
        self.resource_tools: dict[str, tuple[McpClient, str]] = {}
        self.prompt_tools: dict[str, tuple[McpClient, str]] = {}
        self.definitions: tuple[ToolDefinition, ...] = ()

    def clear_routes(self) -> None:
        self.tools.clear()
        self.resource_tools.clear()
        self.prompt_tools.clear()

    def discover(self) -> tuple[ToolDefinition, ...]:
        self.clear_routes()
        definitions: list[ToolDefinition] = []
        for client in self.clients.values():
            definitions.extend(self.bridge_definitions(client))
            for definition in client.tool_definitions():
                split = self.names.split_tool(definition.name)
                if split is None:
                    continue
                _server, remote_tool = split
                self.tools[definition.name] = (client, remote_tool)
                definitions.append(definition)
        self.definitions = tuple(definitions)
        return self.definitions

    def close(self) -> None:
        for client in self.clients.values():
            client.close()

    def bridge_definitions(self, client: McpClient) -> tuple[ToolDefinition, ...]:
        server_name = client.config.name
        resource_list = self.names.resource(server_name, "list")
        resource_read = self.names.resource(server_name, "read")
        prompt_list = self.names.prompt(server_name, "list")
        prompt_get = self.names.prompt(server_name, "get")
        self.resource_tools[resource_list] = (client, "list")
        self.resource_tools[resource_read] = (client, "read")
        self.prompt_tools[prompt_list] = (client, "list")
        self.prompt_tools[prompt_get] = (client, "get")
        return (
            ToolDefinition(
                name=resource_list,
                description=f"List MCP resources exposed by server {server_name!r}.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                read_only=True,
                concurrency_safe=True,
            ),
            ToolDefinition(
                name=resource_read,
                description=f"Read one MCP resource URI from server {server_name!r}.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "uri": {
                            "type": "string",
                            "minLength": 1,
                            "description": "MCP resource URI returned by the server.",
                        }
                    },
                    "required": ["uri"],
                    "additionalProperties": False,
                },
                read_only=True,
                concurrency_safe=True,
            ),
            ToolDefinition(
                name=prompt_list,
                description=f"List MCP prompts exposed by server {server_name!r}.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                read_only=True,
                concurrency_safe=True,
            ),
            ToolDefinition(
                name=prompt_get,
                description=f"Get one MCP prompt from server {server_name!r}.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "minLength": 1},
                        "arguments": {
                            "type": "object",
                            "additionalProperties": True,
                            "default": {},
                        },
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
                read_only=True,
                concurrency_safe=True,
            ),
        )
