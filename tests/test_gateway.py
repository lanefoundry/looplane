from __future__ import annotations

from collections.abc import Sequence

import httpx
import pytest

from rivumi.contracts import (
    ConversationItem,
    Message,
    ModelCapabilities,
    ModelProtocol,
    ModelTurn,
    ToolCall,
    ToolDefinition,
    ToolObservation,
    Usage,
)
from rivumi.gateway import ModelGateway
from rivumi.models import ProviderError, ProviderErrorKind


class CapturingProvider:
    provider_name = "test-provider"
    model_id = "test-model"
    protocol = ModelProtocol.OPENAI_CHAT
    capabilities = ModelCapabilities(
        tool_calling=True,
        streaming=False,
        structured_output=False,
    )

    def __init__(self, result: ModelTurn | Exception) -> None:
        self.result = result
        self.calls: list[tuple[tuple[ConversationItem, ...], tuple[ToolDefinition, ...]]] = []
        self.closed = False

    async def complete(
        self,
        messages: Sequence[ConversationItem],
        tools: Sequence[ToolDefinition] = (),
    ) -> ModelTurn:
        self.calls.append((tuple(messages), tuple(tools)))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def aclose(self) -> None:
        self.closed = True


def gateway_client(
    result: ModelTurn | Exception,
    **gateway_kwargs: object,
) -> tuple[CapturingProvider, httpx.AsyncClient]:
    provider = CapturingProvider(result)
    app = ModelGateway(provider, **gateway_kwargs)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway.local",
    )
    return provider, client


@pytest.mark.asyncio
async def test_health_models_and_text_completion() -> None:
    provider, client = gateway_client(
        ModelTurn(
            content="Hello from the canonical model.",
            usage=Usage(input_tokens=4, output_tokens=6),
            finish_reason="stop",
        )
    )
    async with client:
        health = await client.get("/healthz")
        models = await client.get("/v1/models")
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    assert health.json() == {"status": "ok"}
    assert models.json()["data"] == [
        {"id": "test-model", "object": "model", "owned_by": "test-provider"}
    ]
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"] == {
        "role": "assistant",
        "content": "Hello from the canonical model.",
    }
    assert body["usage"] == {
        "prompt_tokens": 4,
        "completion_tokens": 6,
        "total_tokens": 10,
    }
    assert provider.calls[0][0] == (Message(role="user", content="Hello"),)


@pytest.mark.asyncio
async def test_lifespan_closes_provider_on_the_server_event_loop() -> None:
    provider = CapturingProvider(ModelTurn(content="unused"))
    app = ModelGateway(provider)
    incoming = iter(
        [
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ]
    )
    outgoing: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return next(incoming)

    async def send(message: dict[str, object]) -> None:
        outgoing.append(message)

    await app({"type": "lifespan"}, receive, send)

    assert provider.closed is True
    assert outgoing == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]


@pytest.mark.asyncio
async def test_tools_are_decoded_and_tool_calls_are_encoded() -> None:
    call = ToolCall(tool_call_id="call-1", name="read_file", arguments={"path": "README.md"})
    provider, client = gateway_client(
        ModelTurn(tool_calls=(call,), finish_reason="tool_calls")
    )
    async with client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Read the readme"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "description": "Read a file",
                            "parameters": {
                                "type": "object",
                                "properties": {"path": {"type": "string"}},
                            },
                        },
                    }
                ],
            },
        )

    assert response.status_code == 200
    assert provider.calls[0][1][0].name == "read_file"
    assert provider.calls[0][1][0].input_schema["properties"]["path"] == {"type": "string"}
    assert response.json()["choices"][0]["message"]["tool_calls"] == [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
        }
    ]


@pytest.mark.asyncio
async def test_tool_result_roundtrips_through_canonical_observation() -> None:
    provider, client = gateway_client(ModelTurn(content="The file is present."))
    async with client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [
                    {"role": "user", "content": "Read it"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"README.md"}',
                                },
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call-1", "content": "contents"},
                ],
            },
        )

    assert response.status_code == 200
    observation = provider.calls[0][0][2]
    assert observation == ToolObservation(
        tool_call_id="call-1",
        name="read_file",
        ok=True,
        content="contents",
    )


@pytest.mark.asyncio
async def test_invalid_and_streaming_requests_return_safe_openai_errors() -> None:
    provider, client = gateway_client(ModelTurn(content="unused"))
    async with client:
        malformed = await client.post(
            "/v1/chat/completions",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        streaming = await client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
        )

    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "invalid_request"
    assert streaming.status_code == 400
    assert streaming.json()["error"]["code"] == "unsupported_streaming"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_provider_errors_do_not_leak_exception_details() -> None:
    secret = "sk-super-secret"
    _provider, client = gateway_client(
        ProviderError(
            f"upstream rejected Authorization: Bearer {secret}",
            kind=ProviderErrorKind.AUTH,
            provider_name="test-provider",
            status_code=401,
        )
    )
    async with client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    assert response.status_code == 502
    assert response.json()["error"]["message"] == "upstream model request failed"
    assert secret not in response.text


@pytest.mark.asyncio
async def test_request_size_limit_is_enforced_before_provider_dispatch() -> None:
    provider, client = gateway_client(ModelTurn(content="unused"), max_request_bytes=64)
    async with client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "x" * 100}],
            },
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_gateway_bearer_protects_v1_routes_but_not_health() -> None:
    provider, client = gateway_client(ModelTurn(content="Authorized"), bearer_token="gateway-key")
    async with client:
        health = await client.get("/healthz")
        missing = await client.get("/v1/models")
        wrong = await client.get(
            "/v1/models", headers={"authorization": "Bearer gateway-key-wrong"}
        )
        allowed = await client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer gateway-key"},
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    assert health.status_code == 200
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert wrong.status_code == 401
    assert allowed.status_code == 200
    assert len(provider.calls) == 1
