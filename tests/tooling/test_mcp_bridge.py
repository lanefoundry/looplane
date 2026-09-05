"""MCP owner characterization and unchanged executor dispatch contracts."""

from __future__ import annotations

import gc
import subprocess
import sys
import weakref
from dataclasses import replace

import pytest

from looplane import tools
from looplane.contracts import ToolCall, ToolDefinition, VerificationCommand
from looplane.mcp_client import (
    McpError,
    NativeMcpServerConfig,
    native_mcp_prompt_tool_name,
    native_mcp_resource_tool_name,
    split_native_mcp_tool_name,
)
from looplane.policy import SafePathPolicy
from looplane.tooling.mcp_bridge import McpBridge, McpToolNames

NAMES = McpToolNames(
    resource=native_mcp_resource_tool_name,
    prompt=native_mcp_prompt_tool_name,
    split_tool=split_native_mcp_tool_name,
)


class FakeClient:
    def __init__(self, config, **options):
        self.config = config
        self.options = options
        self.definitions = (
            ToolDefinition(
                name=f"mcp__{config.name}__echo",
                description="Exact remote description.",
                input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
                read_only=True,
                concurrency_safe=False,
            ),
        )
        self.discovery_error = None
        self.call_error = None
        self.close_error = None
        self.calls = []
        self.close_count = 0
        self.content = "output"
        self.result_error = None

    def tool_definitions(self):
        if self.discovery_error:
            raise self.discovery_error
        return self.definitions

    def close(self):
        self.close_count += 1
        if self.close_error:
            raise self.close_error

    def _call(self, operation, args, timeout_seconds):
        self.calls.append((operation, args, timeout_seconds))
        if self.call_error:
            raise self.call_error
        return self.content

    def list_resources(self, *, timeout_seconds):
        return self._call("resources/list", (), timeout_seconds)

    def read_resource(self, uri, *, timeout_seconds):
        return self._call("resources/read", (uri,), timeout_seconds)

    def list_prompts(self, *, timeout_seconds):
        return self._call("prompts/list", (), timeout_seconds)

    def get_prompt(self, name, arguments, *, timeout_seconds):
        return self._call("prompts/get", (name, arguments), timeout_seconds)

    def call_tool(self, name, arguments, *, timeout_seconds):
        content = self._call("tools/call", (name, arguments), timeout_seconds)
        return self.result_error is None, content, self.result_error


def config(name="local", **kwargs):
    return NativeMcpServerConfig(name=name, command="unused", **kwargs)


def make_bridge(configs=None, factory=FakeClient):
    return McpBridge(configs or (config(),), client_factory=factory, names=NAMES)


@pytest.fixture
def executor(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "StdioMcpClient", FakeClient)
    executor = tools.ToolExecutor(
        tmp_path,
        SafePathPolicy(tmp_path),
        (VerificationCommand(name="check", argv=("unused",)),),
        limits={"max_tool_output_bytes": 64},
        mcp_servers=(config(),),
    )
    yield executor
    executor.close()


def test_definitions_keep_exact_schema_description_metadata_and_order():
    bridge = make_bridge()
    client = bridge.clients["local"]
    remote = client.definitions[0]
    definitions = bridge.discover()
    assert definitions is bridge.definitions
    expected = [
        ToolDefinition(
            name="mcp_resource__local__list",
            description="List MCP resources exposed by server 'local'.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            read_only=True,
            concurrency_safe=True,
        ),
        ToolDefinition(
            name="mcp_resource__local__read",
            description="Read one MCP resource URI from server 'local'.",
            input_schema={
                "type": "object",
                "properties": {
                    "uri": {
                        "type": "string",
                        "minLength": 1,
                        "description": "MCP resource URI returned by the server.",
                    },
                },
                "required": ["uri"],
                "additionalProperties": False,
            },
            read_only=True,
            concurrency_safe=True,
        ),
        ToolDefinition(
            name="mcp_prompt__local__list",
            description="List MCP prompts exposed by server 'local'.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            read_only=True,
            concurrency_safe=True,
        ),
        ToolDefinition(
            name="mcp_prompt__local__get",
            description="Get one MCP prompt from server 'local'.",
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
        remote,
    ]
    assert [item.model_dump(mode="json") for item in definitions] == [
        item.model_dump(mode="json") for item in expected
    ]
    assert definitions[-1] is remote
    assert bridge.tools == {remote.name: (client, "echo")}


def test_discovery_preserves_client_order_and_namespace_filtering():
    bridge = make_bridge((config("zeta"), config("alpha")))
    first = bridge.clients["zeta"]
    foreign = first.definitions[0].model_copy(update={"name": "mcp__foreign__remote"})
    first.definitions = (*first.definitions, ToolDefinition(name="invalid"), foreign)
    definitions = bridge.discover()
    assert [item.name for item in definitions][:2] == [
        "mcp_resource__zeta__list",
        "mcp_resource__zeta__read",
    ]
    assert "invalid" not in {item.name for item in definitions}
    assert bridge.tools[foreign.name] == (first, "remote")
    assert definitions[-1].name == "mcp__alpha__echo"


def test_factory_is_consumed_without_retaining_its_owner():
    class Factory:
        def create(self, config):
            return FakeClient(config)

    factory = Factory()
    reference = weakref.ref(factory)
    bridge = make_bridge(factory=factory.create)
    del factory
    gc.collect()
    assert reference() is None
    assert bridge.discover()


def test_duplicate_config_names_keep_last_client_and_creation_order():
    created = []

    def factory(config):
        created.append(FakeClient(config))
        return created[-1]

    configs = (config("same"), config("other"), config("same"))
    bridge = make_bridge(configs, factory)
    assert bridge.configs == configs
    assert list(bridge.clients) == ["same", "other"]
    assert bridge.clients["same"] is created[2]
    bridge.close()
    assert [client.close_count for client in created] == [0, 1, 1]


def test_failed_refresh_keeps_previous_definitions_and_partial_routes(executor):
    client = executor.mcp_bridge.clients["local"]
    definitions = executor.definitions
    mcp_definitions = executor.mcp_bridge.definitions
    client.discovery_error = McpError("discovery failed")
    with pytest.raises(McpError, match="discovery failed"):
        executor.refresh_mcp_tool_definitions()
    assert executor.definitions is definitions
    assert executor.mcp_bridge.definitions is mcp_definitions
    assert executor.mcp_bridge.tools == {}
    assert len(executor.mcp_bridge.resource_tools) == len(executor.mcp_bridge.prompt_tools) == 2
    client.discovery_error = None
    assert executor.refresh_mcp_tool_definitions() is False
    assert executor.execute(ToolCall(name="mcp__local__echo")).ok


def test_refresh_detects_metadata_changes_removes_stale_routes_and_preserves_builtins(executor):
    bridge = executor.mcp_bridge
    client = bridge.clients["local"]
    tools_map = bridge.tools
    assert executor.refresh_mcp_tool_definitions() is False
    client.definitions = (
        client.definitions[0].model_copy(
            update={"description": "New metadata", "read_only": False}
        ),
    )
    assert executor.refresh_mcp_tool_definitions() is True
    assert executor.refresh_mcp_tool_definitions() is False
    client.definitions = (client.definitions[0].model_copy(update={"name": "mcp__local__second"}),)
    assert executor.refresh_mcp_tool_definitions() is True
    assert bridge.tools is tools_map
    assert "mcp__local__echo" not in tools_map
    assert not executor.execute(ToolCall(name="mcp__local__echo")).ok
    assert executor.execute(ToolCall(name="mcp__local__second")).ok
    run_check = next(item for item in executor.definitions if item.name == "run_check")
    assert run_check.input_schema["properties"]["name"]["enum"] == ["check"]
    assert executor._mcp_clients is bridge.clients
    assert executor._mcp_tools is bridge.tools
    assert executor._mcp_resource_tools is bridge.resource_tools
    assert executor._mcp_prompt_tools is bridge.prompt_tools


def test_close_error_stops_iteration_and_repeated_close_is_forwarded():
    bridge = make_bridge((config("first"), config("second")))
    first, second = bridge.clients.values()
    first.close_error = OSError("close failed")
    with pytest.raises(OSError, match="close failed"):
        bridge.close()
    assert (first.close_count, second.close_count) == (1, 0)
    first.close_error = None
    bridge.close()
    bridge.close()
    assert (first.close_count, second.close_count) == (3, 2)


def test_factory_error_still_propagates_without_implicit_close():
    created = []

    def factory(config):
        if config.name == "second":
            raise McpError("construction failed")
        created.append(FakeClient(config))
        return created[-1]

    with pytest.raises(McpError, match="construction failed"):
        make_bridge((config("first"), config("second")), factory)
    assert created[0].close_count == 0


def test_legacy_client_factories_receive_exact_workspace_and_limit_options(tmp_path, monkeypatch):
    created = []

    def factory(config, **options):
        created.append(FakeClient(config, **options))
        return created[-1]

    monkeypatch.setattr(tools, "StdioMcpClient", factory)
    monkeypatch.setattr(tools, "HttpMcpClient", factory)
    home = tmp_path / "home"
    executor = tools.ToolExecutor(
        tmp_path,
        SafePathPolicy(tmp_path),
        (),
        limits={"max_tool_output_bytes": 123, "max_output_chars": 456},
        task_home=home,
        mcp_servers=(config(), NativeMcpServerConfig(name="http", url="https://example.test/mcp")),
    )
    assert created[0].options == {
        "cwd": tmp_path.resolve(),
        "task_home": home.resolve(),
        "max_output_chars": 123,
    }
    assert created[1].options == {"max_output_chars": 123}
    executor.close()
    assert [client.close_count for client in created] == [1, 1]


@pytest.mark.parametrize(
    "name,arguments,operation,default",
    [
        ("mcp_resource__local__list", {}, "resources/list", 10.0),
        ("mcp_resource__local__read", {"uri": "file:///notes"}, "resources/read", 30.0),
        ("mcp_prompt__local__list", {}, "prompts/list", 10.0),
        ("mcp_prompt__local__get", {"name": "review"}, "prompts/get", 30.0),
        ("mcp__local__echo", {"text": "hello"}, "tools/call", 30.0),
    ],
)
@pytest.mark.parametrize("budget", [None, 0.25, 50.0, 0.0])
def test_dispatch_preserves_harness_timeouts(executor, name, arguments, operation, default, budget):
    client = executor.mcp_bridge.clients["local"]
    result = executor.execute(ToolCall(name=name, arguments=arguments), timeout_seconds=budget)
    if budget == 0.0:
        assert not result.ok
        assert "budget is exhausted" in result.error
        assert client.calls == []
    else:
        assert result.ok
        assert client.calls[-1][0] == operation
        assert client.calls[-1][2] == (default if budget is None else min(default, budget))
    client.calls.clear()
    result = executor.execute(ToolCall(name=name, arguments={**arguments, "timeout_seconds": 99}))
    assert not result.ok
    assert "controlled by the harness" in result.error
    assert client.calls == []


@pytest.mark.parametrize(
    "name,args,message",
    [
        ("mcp_resource__local__read", {}, "uri must be a non-empty string"),
        ("mcp_resource__local__read", {"uri": 3}, "uri must be a non-empty string"),
        ("mcp_prompt__local__get", {}, "name must be a non-empty string"),
        ("mcp_prompt__local__get", {"name": "ok", "arguments": []}, "arguments must be an object"),
    ],
)
def test_dispatch_preserves_argument_validation(executor, name, args, message):
    result = executor.execute(ToolCall(name=name, arguments=args))
    assert not result.ok
    assert message in result.error
    assert executor.mcp_bridge.clients["local"].calls == []


@pytest.mark.parametrize("error_type", [McpError, OSError, TypeError])
@pytest.mark.parametrize(
    "name", ["mcp_resource__local__list", "mcp_prompt__local__list", "mcp__local__echo"]
)
def test_dispatch_errors_keep_type_prefix_and_bound(executor, name, error_type):
    client = executor.mcp_bridge.clients["local"]
    client.call_error = error_type("x" * 300)
    result = executor.execute(ToolCall(name=name))
    assert not result.ok
    assert result.content == ""
    assert result.error == tools.bounded_text(f"{error_type.__name__}: " + "x" * 300, 64)


def test_remote_error_is_preserved_while_content_is_bounded(executor):
    client = executor.mcp_bridge.clients["local"]
    client.content = "output" * 100
    client.result_error = "remote failure" * 100
    result = executor.execute(ToolCall(name="mcp__local__echo"))
    assert not result.ok
    assert result.content == tools.bounded_text(client.content, 64)
    assert result.error == client.result_error


def test_naming_callbacks_are_explicit_and_bridge_import_is_independent():
    bridge = McpBridge(
        (config(),),
        client_factory=FakeClient,
        names=replace(NAMES, resource=lambda server, operation: f"resource:{server}:{operation}"),
    )
    assert bridge.discover()[0].name == "resource:local:list"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import looplane.tooling.mcp_bridge; "
            "assert not {'looplane.tools', 'looplane.runtime', 'looplane.mcp_client', "
            "'looplane.loop', 'looplane.cli', 'looplane.tui'}.intersection(sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
