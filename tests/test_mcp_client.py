from __future__ import annotations

import json
import sys
from pathlib import Path

from rivumi.approvals import ToolEffect, effect_for_tool
from rivumi.contracts import ToolCall, VerificationCommand
from rivumi.mcp_client import load_native_mcp_server_configs
from rivumi.policy import SafePathPolicy
from rivumi.runtime_registry import RUNTIME_REGISTRY, RuntimeCapability
from rivumi.tools import ToolExecutor


def _write_fake_mcp_server(path: Path) -> None:
    path.write_text(
        """
from __future__ import annotations

import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if "id" not in request:
        continue
    if method == "initialize":
        result = {
            "protocolVersion": request["params"]["protocolVersion"],
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
            "serverInfo": {"name": "fake", "version": "1"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo one message.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"message": {"type": "string"}},
                        "required": ["message"],
                        "additionalProperties": False,
                    },
                }
            ]
        }
    elif method == "tools/call":
        result = {
            "content": [
                {"type": "text", "text": "echo:" + request["params"]["arguments"]["message"]}
            ]
        }
    elif method == "resources/list":
        result = {
            "resources": [
                {
                    "uri": "file:///notes.md",
                    "name": "notes",
                    "mimeType": "text/plain",
                }
            ]
        }
    elif method == "resources/read":
        result = {
            "contents": [
                {
                    "uri": request["params"]["uri"],
                    "mimeType": "text/plain",
                    "text": "resource body",
                }
            ]
        }
    elif method == "prompts/list":
        result = {
            "prompts": [
                {
                    "name": "review",
                    "description": "Review a topic.",
                    "arguments": [{"name": "topic", "required": True}],
                }
            ]
        }
    elif method == "prompts/get":
        topic = request["params"]["arguments"].get("topic", "unknown")
        result = {
            "description": "Review prompt",
            "messages": [
                {
                    "role": "user",
                    "content": {"type": "text", "text": "review " + topic},
                }
            ],
        }
    else:
        print(
            json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601}}),
            flush=True,
        )
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
""".lstrip(),
        encoding="utf-8",
    )


def _write_mcp_config(tmp_path: Path, server: Path) -> Path:
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "local": {"command": sys.executable, "args": [str(server)]},
                }
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_mcp_config_requires_explicit_allowlist(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "allowed": {"command": sys.executable, "args": ["server.py"]},
                    "blocked": {"command": sys.executable, "args": ["other.py"]},
                }
            }
        ),
        encoding="utf-8",
    )

    assert load_native_mcp_server_configs(tmp_path, allowlist=()) == ()

    configs = load_native_mcp_server_configs(tmp_path, allowlist=("allowed",))

    assert len(configs) == 1
    assert configs[0].name == "allowed"
    assert configs[0].command == sys.executable


def test_tool_executor_exposes_and_calls_allowlisted_mcp_tool(tmp_path: Path) -> None:
    server = tmp_path / "fake_mcp_server.py"
    _write_fake_mcp_server(server)
    configs = load_native_mcp_server_configs(
        _write_mcp_config(tmp_path, server),
        allowlist=("local",),
    )
    executor = ToolExecutor(
        workspace=tmp_path,
        policy=SafePathPolicy(tmp_path, allowed_paths=("**",)),
        verification_commands=(VerificationCommand(name="noop", argv=("true",)),),
        mcp_servers=configs,
    )
    try:
        definitions = {definition.name: definition for definition in executor.definitions}

        assert "mcp__local__echo" in definitions
        assert definitions["mcp__local__echo"].input_schema["required"] == ["message"]

        observation = executor.execute(
            ToolCall(name="mcp__local__echo", arguments={"message": "hello"})
        )

        assert observation.ok is True
        assert observation.content == "echo:hello"
    finally:
        executor.close()


def test_tool_executor_exposes_mcp_resource_and_prompt_read_only_bridges(tmp_path: Path) -> None:
    server = tmp_path / "fake_mcp_server.py"
    _write_fake_mcp_server(server)
    configs = (
        load_native_mcp_server_configs(
            _write_mcp_config(tmp_path, server),
            allowlist=("local",),
        )
    )
    executor = ToolExecutor(
        workspace=tmp_path,
        policy=SafePathPolicy(tmp_path, allowed_paths=("**",)),
        verification_commands=(VerificationCommand(name="noop", argv=("true",)),),
        mcp_servers=configs,
    )
    try:
        definitions = {definition.name: definition for definition in executor.definitions}

        bridge_names = (
            "mcp_resource__local__list",
            "mcp_resource__local__read",
            "mcp_prompt__local__list",
            "mcp_prompt__local__get",
        )
        for name in bridge_names:
            assert definitions[name].read_only is True
            assert definitions[name].concurrency_safe is True
    finally:
        executor.close()


def test_mcp_resource_bridge_lists_and_reads_resources(tmp_path: Path) -> None:
    server = tmp_path / "fake_mcp_server.py"
    _write_fake_mcp_server(server)
    configs = load_native_mcp_server_configs(
        _write_mcp_config(tmp_path, server),
        allowlist=("local",),
    )
    executor = ToolExecutor(
        workspace=tmp_path,
        policy=SafePathPolicy(tmp_path, allowed_paths=("**",)),
        verification_commands=(VerificationCommand(name="noop", argv=("true",)),),
        mcp_servers=configs,
    )
    try:
        listed = executor.execute(ToolCall(name="mcp_resource__local__list"))
        read = executor.execute(
            ToolCall(name="mcp_resource__local__read", arguments={"uri": "file:///notes.md"})
        )

        assert listed.ok is True
        assert "file:///notes.md" in listed.content
        assert read.ok is True
        assert "resource body" in read.content
    finally:
        executor.close()


def test_mcp_prompt_bridge_lists_and_gets_prompt(tmp_path: Path) -> None:
    server = tmp_path / "fake_mcp_server.py"
    _write_fake_mcp_server(server)
    configs = load_native_mcp_server_configs(
        _write_mcp_config(tmp_path, server),
        allowlist=("local",),
    )
    executor = ToolExecutor(
        workspace=tmp_path,
        policy=SafePathPolicy(tmp_path, allowed_paths=("**",)),
        verification_commands=(VerificationCommand(name="noop", argv=("true",)),),
        mcp_servers=configs,
    )
    try:
        listed = executor.execute(ToolCall(name="mcp_prompt__local__list"))
        got = executor.execute(
            ToolCall(
                name="mcp_prompt__local__get",
                arguments={"name": "review", "arguments": {"topic": "diff"}},
            )
        )

        assert listed.ok is True
        assert '"name": "review"' in listed.content
        assert got.ok is True
        assert "review diff" in got.content
    finally:
        executor.close()


def test_native_mcp_tools_are_execute_effect() -> None:
    assert effect_for_tool("mcp__local__echo") is ToolEffect.EXECUTE
    assert effect_for_tool("mcp_resource__local__read") is ToolEffect.READ
    assert effect_for_tool("mcp_prompt__local__get") is ToolEffect.READ


def test_rivumi_agent_advertises_native_mcp_capability() -> None:
    assert RuntimeCapability.MCP in RUNTIME_REGISTRY["rivumi-agent"].capabilities
