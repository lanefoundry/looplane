from __future__ import annotations

import json
import stat
import sys
import time
from pathlib import Path

import httpx
import pytest

from rivumi.approvals import ToolEffect, effect_for_tool, effect_for_tool_definition
from rivumi.contracts import ToolCall, ToolDefinition, VerificationCommand
from rivumi.mcp_client import (
    McpError,
    McpOAuthClient,
    McpOAuthCredential,
    McpOAuthCredentialStore,
    NativeMcpOAuthConfig,
    discover_http_auth_metadata,
    load_native_mcp_server_configs,
    mcp_oauth_credential_path,
    parse_mcp_oauth_callback,
)
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
                    "annotations": {"readOnlyHint": True, "destructiveHint": False},
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


def _write_refreshing_mcp_server(path: Path) -> None:
    path.write_text(
        """
from __future__ import annotations

import json
import sys
from pathlib import Path

counter_path = Path(sys.argv[1])

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if "id" not in request:
        continue
    if method == "initialize":
        result = {
            "protocolVersion": request["params"]["protocolVersion"],
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "refreshing", "version": "1"},
        }
    elif method == "tools/list":
        count = int(counter_path.read_text(encoding="utf-8")) if counter_path.exists() else 0
        counter_path.write_text(str(count + 1), encoding="utf-8")
        name = "first" if count == 0 else "second"
        result = {
            "tools": [
                {
                    "name": name,
                    "inputSchema": {"type": "object", "properties": {}},
                    "annotations": {"readOnlyHint": True},
                }
            ]
        }
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": request["params"]["name"]}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
""".lstrip(),
        encoding="utf-8",
    )


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


def test_mcp_config_loads_allowlisted_http_server(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {
                        "url": "https://mcp.example.test/mcp",
                        "headers": {"x-client": "rivumi"},
                        "bearerTokenEnvVar": "REMOTE_MCP_TOKEN",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    configs = load_native_mcp_server_configs(tmp_path, allowlist=("remote",))

    assert len(configs) == 1
    assert configs[0].url == "https://mcp.example.test/mcp"
    assert configs[0].headers == {"x-client": "rivumi"}
    assert configs[0].bearer_token_env_var == "REMOTE_MCP_TOKEN"


def test_mcp_config_loads_authorization_code_oauth_metadata(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {
                        "url": "https://mcp.example.test/mcp",
                        "oauth": {
                            "grantType": "authorization_code",
                            "issuer": "https://auth.example.test",
                            "authorizationEndpoint": "https://auth.example.test/authorize",
                            "tokenEndpoint": "https://auth.example.test/token",
                            "clientId": "rivumi",
                            "redirectUri": "https://client.example.test/callback",
                            "scopes": ["mcp:tools", "mcp:tools"],
                            "accessTokenEnvVar": "REMOTE_MCP_ACCESS_TOKEN",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    configs = load_native_mcp_server_configs(tmp_path, allowlist=("remote",))

    assert configs[0].oauth is not None
    assert configs[0].oauth.grant_type == "authorization_code"
    assert configs[0].oauth.scopes == ("mcp:tools",)


def test_mcp_config_rejects_remote_plain_http(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"remote": {"url": "http://mcp.example.test/mcp"}}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="HTTP url is only allowed"):
        load_native_mcp_server_configs(tmp_path, allowlist=("remote",))


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
        assert definitions["mcp__local__echo"].read_only is True
        assert definitions["mcp__local__echo"].concurrency_safe is True

        observation = executor.execute(
            ToolCall(name="mcp__local__echo", arguments={"message": "hello"})
        )

        assert observation.ok is True
        assert observation.content == "echo:hello"
    finally:
        executor.close()


def test_tool_executor_exposes_and_calls_allowlisted_http_mcp_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(
            {
                "method": body.get("method"),
                "id": body.get("id"),
                "authorization": request.headers.get("authorization"),
                "session": request.headers.get("mcp-session-id"),
                "protocol": request.headers.get("mcp-protocol-version"),
                "client": request.headers.get("x-client"),
            }
        )
        request_id = body.get("id")
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "mcp-session-id": "session-1",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2026-07-28",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            payload = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "tools": [
                            {
                                "name": "lookup",
                                "description": "Lookup a value.",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"key": {"type": "string"}},
                                    "required": ["key"],
                                    "additionalProperties": False,
                                },
                                "annotations": {"readOnlyHint": True},
                            }
                        ]
                    },
                }
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=f"event: message\ndata: {payload}\n\n",
            )
        if method == "tools/call":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"content": [{"type": "text", "text": "value:42"}]},
                },
            )
        raise AssertionError(f"unexpected MCP method: {method}")

    real_client = httpx.Client

    def client_factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr("rivumi.mcp_client.httpx.Client", client_factory)
    monkeypatch.setenv("REMOTE_MCP_TOKEN", "secret-token")
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {
                        "url": "https://mcp.example.test/mcp",
                        "headers": {"x-client": "rivumi"},
                        "bearerTokenEnvVar": "REMOTE_MCP_TOKEN",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    configs = load_native_mcp_server_configs(tmp_path, allowlist=("remote",))
    executor = ToolExecutor(
        workspace=tmp_path,
        policy=SafePathPolicy(tmp_path, allowed_paths=("**",)),
        verification_commands=(VerificationCommand(name="noop", argv=("true",)),),
        mcp_servers=configs,
    )
    try:
        definitions = {definition.name: definition for definition in executor.definitions}
        observation = executor.execute(
            ToolCall(name="mcp__remote__lookup", arguments={"key": "answer"})
        )

        assert definitions["mcp__remote__lookup"].read_only is True
        assert observation.ok is True
        assert observation.content == "value:42"
        assert [request["method"] for request in requests] == [
            "initialize",
            "notifications/initialized",
            "tools/list",
            "tools/call",
        ]
        assert all(request["authorization"] == "Bearer secret-token" for request in requests)
        assert requests[0]["session"] is None
        assert requests[1]["session"] == "session-1"
        assert requests[2]["session"] == "session-1"
        assert requests[2]["protocol"] == "2026-07-28"
        assert requests[2]["client"] == "rivumi"
    finally:
        executor.close()


def test_tool_executor_uses_oauth_access_token_for_http_mcp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorizations: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        authorizations.append(request.headers.get("authorization"))
        request_id = body.get("id")
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"jsonrpc": "2.0", "id": request_id, "result": {"capabilities": {}}},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"tools": []},
                },
            )
        raise AssertionError(f"unexpected MCP method: {method}")

    real_client = httpx.Client

    def client_factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr("rivumi.mcp_client.httpx.Client", client_factory)
    monkeypatch.setenv("REMOTE_MCP_ACCESS_TOKEN", "oauth-access-token")
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {
                        "url": "https://mcp.example.test/mcp",
                        "oauth": {
                            "authorizationEndpoint": "https://auth.example.test/authorize",
                            "tokenEndpoint": "https://auth.example.test/token",
                            "clientId": "rivumi",
                            "redirectUri": "https://client.example.test/callback",
                            "accessTokenEnvVar": "REMOTE_MCP_ACCESS_TOKEN",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    executor = ToolExecutor(
        workspace=tmp_path,
        policy=SafePathPolicy(tmp_path, allowed_paths=("**",)),
        verification_commands=(VerificationCommand(name="noop", argv=("true",)),),
        mcp_servers=load_native_mcp_server_configs(tmp_path, allowlist=("remote",)),
    )
    try:
        assert executor.definitions
        assert authorizations == [
            "Bearer oauth-access-token",
            "Bearer oauth-access-token",
            "Bearer oauth-access-token",
        ]
    finally:
        executor.close()


def test_http_mcp_oauth_header_uses_rivumi_store_when_env_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorizations: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        authorizations.append(request.headers.get("authorization"))
        request_id = body.get("id")
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"jsonrpc": "2.0", "id": request_id, "result": {"capabilities": {}}},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"jsonrpc": "2.0", "id": request_id, "result": {"tools": []}},
            )
        raise AssertionError(method)

    real_client = httpx.Client

    def client_factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr("rivumi.mcp_client.httpx.Client", client_factory)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    McpOAuthCredentialStore(mcp_oauth_credential_path("remote")).save(
        McpOAuthCredential(accessToken="stored-access-token")
    )
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {
                        "url": "https://mcp.example.test/mcp",
                        "oauth": {
                            "authorizationEndpoint": "https://auth.example.test/authorize",
                            "tokenEndpoint": "https://auth.example.test/token",
                            "clientId": "rivumi",
                            "redirectUri": "http://localhost:1455/callback",
                            "accessTokenEnvVar": "REMOTE_MCP_ACCESS_TOKEN",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    executor = ToolExecutor(
        workspace=tmp_path,
        policy=SafePathPolicy(tmp_path, allowed_paths=("**",)),
        verification_commands=(VerificationCommand(name="noop", argv=("true",)),),
        mcp_servers=load_native_mcp_server_configs(tmp_path, allowlist=("remote",)),
    )
    try:
        assert executor.definitions
        assert all(value == "Bearer stored-access-token" for value in authorizations)
    finally:
        executor.close()


def test_mcp_oauth_client_exchanges_authorization_code_and_parses_callback() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
            request=request,
        )

    config = NativeMcpOAuthConfig(
        authorizationEndpoint="https://auth.example.test/authorize",
        tokenEndpoint="https://auth.example.test/token",
        clientId="rivumi",
        redirectUri="http://localhost:1455/callback",
        scopes=("mcp:tools",),
        accessTokenEnvVar="REMOTE_MCP_ACCESS_TOKEN",
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    oauth = McpOAuthClient(client=client)

    try:
        authorization = oauth.begin_login(config)
        assert authorization.url.startswith("https://auth.example.test/authorize?")
        code = parse_mcp_oauth_callback(
            f"http://localhost:1455/callback?code=abc&state={authorization.state}",
            expected_state=authorization.state,
        )

        credential = oauth.exchange_code(config, code=code, verifier=authorization.verifier)

        assert code == "abc"
        assert credential.access_token == "access-token"
        assert credential.refresh_token == "refresh-token"
        assert credential.expires_at is not None
        assert credential.expires_at > time.time()
        assert requests[0].url == httpx.URL("https://auth.example.test/token")
        assert b"grant_type=authorization_code" in requests[0].content
        assert b"code=abc" in requests[0].content
        assert b"code_verifier=" in requests[0].content
    finally:
        oauth.close()
        client.close()


def test_mcp_oauth_credential_store_round_trips_private_file(tmp_path: Path) -> None:
    path = tmp_path / "state" / "rivumi" / "auth" / "mcp-remote.json"
    store = McpOAuthCredentialStore(path)

    store.save(
        McpOAuthCredential(
            accessToken="access-token",
            refreshToken="refresh-token",
            expiresAt=1234.5,
        )
    )

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token == "access-token"
    assert loaded.refresh_token == "refresh-token"
    assert loaded.expires_at == 1234.5


def test_discover_http_auth_metadata_from_www_authenticate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://mcp.example.test/mcp":
            return httpx.Response(
                401,
                headers={
                    "www-authenticate": (
                        'Bearer resource_metadata="https://mcp.example.test/.well-known/oauth"'
                    )
                },
                request=request,
            )
        if str(request.url) == "https://mcp.example.test/.well-known/oauth":
            return httpx.Response(
                200,
                json={
                    "resource": "https://mcp.example.test/mcp",
                    "authorization_servers": ["https://auth.example.test"],
                },
                request=request,
            )
        raise AssertionError(str(request.url))

    client = httpx.Client(transport=httpx.MockTransport(handler))

    metadata = discover_http_auth_metadata("https://mcp.example.test/mcp", client=client)

    assert metadata == {
        "resource": "https://mcp.example.test/mcp",
        "authorization_servers": ["https://auth.example.test"],
    }


def test_http_mcp_requires_configured_bearer_token(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {
                        "url": "https://mcp.example.test/mcp",
                        "bearerTokenEnvVar": "MISSING_MCP_TOKEN",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(McpError, match="bearer token env var"):
        ToolExecutor(
            workspace=tmp_path,
            policy=SafePathPolicy(tmp_path, allowed_paths=("**",)),
            verification_commands=(VerificationCommand(name="noop", argv=("true",)),),
            mcp_servers=load_native_mcp_server_configs(tmp_path, allowlist=("remote",)),
        )


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


def test_native_mcp_tool_definition_metadata_can_lower_effect_to_read() -> None:
    definition = ToolDefinition(
        name="mcp__local__echo",
        read_only=True,
        concurrency_safe=True,
    )

    assert effect_for_tool_definition("mcp__local__echo", definition) is ToolEffect.READ
    assert effect_for_tool_definition("mcp__local__echo", None) is ToolEffect.EXECUTE


def test_tool_executor_refreshes_mcp_tool_list_and_call_mapping(tmp_path: Path) -> None:
    server = tmp_path / "refreshing_mcp_server.py"
    counter = tmp_path / "tools-list-count.txt"
    _write_refreshing_mcp_server(server)
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "local": {
                        "command": sys.executable,
                        "args": [str(server), str(counter)],
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    configs = load_native_mcp_server_configs(tmp_path, allowlist=("local",))
    executor = ToolExecutor(
        workspace=tmp_path,
        policy=SafePathPolicy(tmp_path, allowed_paths=("**",)),
        verification_commands=(VerificationCommand(name="noop", argv=("true",)),),
        mcp_servers=configs,
    )
    try:
        assert "mcp__local__first" in {definition.name for definition in executor.definitions}

        assert executor.refresh_mcp_tool_definitions() is True
        definitions = {definition.name: definition for definition in executor.definitions}
        assert "mcp__local__first" not in definitions
        assert definitions["mcp__local__second"].read_only is True

        observation = executor.execute(ToolCall(name="mcp__local__second"))

        assert observation.ok is True
        assert observation.content == "second"
    finally:
        executor.close()


def test_rivumi_agent_advertises_native_mcp_capability() -> None:
    assert RuntimeCapability.MCP in RUNTIME_REGISTRY["rivumi-agent"].capabilities
