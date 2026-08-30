from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import APIStatusError

from looplane.cache_strategy import provider_cache_trace
from looplane.contracts import (
    InjectedContext,
    Message,
    ModelTurn,
    ToolCall,
    ToolDefinition,
    ToolObservation,
    Usage,
)
from looplane.models import (
    AnthropicModel,
    GeminiModel,
    OpenAICompatibleModel,
    ProviderError,
    ProviderErrorKind,
    ResponsesModel,
    ScriptedModel,
    WorkersAIModel,
    _http_error,
    _retry_after,
)
from looplane.prompts import PromptSection, render_prompt_sections

TOOL = ToolDefinition(
    name="read_file",
    description="Read one file",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
)
MESSAGES = (Message(role="system", content="Be precise."), Message(role="user", content="Read it."))
ATTACHMENT_MESSAGE = Message(
    role="user",
    content="Inspect attachments.",
    provider_metadata={
        "attachments": [
            {
                "name": "screenshot.png",
                "media_type": "image/png",
                "data_base64": "aW1hZ2U=",
            },
            {
                "name": "notes.md",
                "media_type": "text/markdown",
                "content": "# Notes",
            },
        ]
    },
)


def assert_common_turn(turn: ModelTurn) -> None:
    assert turn.content == "I will inspect the file."
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].name == "read_file"
    assert turn.tool_calls[0].arguments == {"path": "src/example.py"}
    assert turn.usage.input_tokens == 11
    assert turn.usage.output_tokens == 7
    assert turn.usage.total_tokens == 18


@pytest.mark.asyncio
async def test_scripted_model_preserves_common_contract_and_records_request() -> None:
    expected = ModelTurn(
        content="I will inspect the file.",
        tool_calls=(ToolCall(name="read_file", arguments={"path": "src/example.py"}),),
        usage=Usage(input_tokens=11, output_tokens=7),
        finish_reason="tool_calls",
    )
    model = ScriptedModel([expected])

    actual = await model.complete(MESSAGES, (TOOL,))

    assert actual is expected
    assert model.calls == [(MESSAGES, (TOOL,))]


class FakeCompletions:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.mark.asyncio
async def test_openai_compatible_text_tool_and_usage_roundtrip() -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="I will inspect the file.",
                    tool_calls=[
                        SimpleNamespace(
                            id="call-1",
                            function=SimpleNamespace(
                                name="read_file",
                                arguments=json.dumps({"path": "src/example.py"}),
                            ),
                        )
                    ],
                ),
                finish_reason="tool_calls",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=11,
            completion_tokens=7,
            prompt_tokens_details=SimpleNamespace(cached_tokens=3),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
        ),
    )
    completions = FakeCompletions(response)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    model = OpenAICompatibleModel(model="fake-model", client=client, supports_tool_calling=True)

    turn = await model.complete(MESSAGES, (TOOL,))

    assert_common_turn(turn)
    assert turn.tool_calls[0].tool_call_id == "call-1"
    assert turn.usage.cached_input_tokens == 3
    assert turn.usage.reasoning_tokens == 2
    assert completions.requests[0]["tools"][0]["function"]["name"] == "read_file"


@pytest.mark.asyncio
async def test_openai_compatible_renders_injected_context_as_marked_user_message() -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="done", tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=None,
    )
    completions = FakeCompletions(response)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    model = OpenAICompatibleModel(model="fake-model", client=client, supports_tool_calling=True)

    await model.complete(
        [
            Message(role="system", content="Use tools."),
            InjectedContext(source="workspace", content="Changed files: src/app.py"),
        ],
        (),
    )

    assert completions.requests[0]["messages"][1] == {
        "role": "user",
        "content": "[injected_context:workspace]\nChanged files: src/app.py",
    }


@pytest.mark.asyncio
async def test_openai_compatible_maps_message_attachments_to_native_content() -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="done", tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=None,
    )
    completions = FakeCompletions(response)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    model = OpenAICompatibleModel(model="fake-model", client=client, supports_tool_calling=True)

    await model.complete((Message(role="system", content="Use tools."), ATTACHMENT_MESSAGE))

    content = completions.requests[0]["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "Inspect attachments."}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
    }
    assert content[2] == {
        "type": "text",
        "text": "[attachment:notes.md; media_type=text/markdown]\n# Notes",
    }


def make_json_client(payload: dict[str, Any], status_code: int = 200) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload, request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def make_capturing_json_client(
    payload: dict[str, Any], status_code: int = 200
) -> tuple[httpx.AsyncClient, list[dict[str, Any]]]:
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(status_code, json=payload, request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), requests


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["anthropic", "gemini", "workers-ai"])
async def test_http_adapters_text_tool_and_usage_roundtrip(provider: str) -> None:
    if provider == "anthropic":
        payload = {
            "content": [
                {"type": "text", "text": "I will inspect the file."},
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "read_file",
                    "input": {"path": "src/example.py"},
                },
            ],
            "usage": {"input_tokens": 11, "output_tokens": 7},
            "stop_reason": "tool_use",
        }
        client = make_json_client(payload)
        model = AnthropicModel(
            model="fake-model", api_key="test", client=client, supports_tool_calling=True
        )
    elif provider == "gemini":
        payload = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "I will inspect the file."},
                            {
                                "functionCall": {
                                    "id": "call-1",
                                    "name": "read_file",
                                    "args": {"path": "src/example.py"},
                                }
                            },
                        ]
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 7},
        }
        client = make_json_client(payload)
        model = GeminiModel(
            model="fake-model", api_key="test", client=client, supports_tool_calling=True
        )
    else:
        payload = {
            "success": True,
            "result": {
                "response": "I will inspect the file.",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "read_file",
                            "arguments": {"path": "src/example.py"},
                        },
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                "finish_reason": "tool_calls",
            },
        }
        client = make_json_client(payload)
        model = WorkersAIModel(
            account_id="account",
            api_token="test",
            model="fake-model",
            supports_tool_calling=True,
            client=client,
        )

    try:
        turn = await model.complete(MESSAGES, (TOOL,))
    finally:
        await client.aclose()

    assert_common_turn(turn)
    assert turn.tool_calls[0].tool_call_id == "call-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["anthropic", "gemini"])
async def test_http_adapters_map_message_attachments_to_native_content(provider: str) -> None:
    if provider == "anthropic":
        payload = {
            "content": [{"type": "text", "text": "Done."}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        client, requests = make_capturing_json_client(payload)
        model = AnthropicModel(
            model="fake-model",
            api_key="test",
            client=client,
            supports_tool_calling=True,
        )
    else:
        payload = {
            "candidates": [{"content": {"parts": [{"text": "Done."}]}}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
        }
        client, requests = make_capturing_json_client(payload)
        model = GeminiModel(
            model="fake-model",
            api_key="test",
            client=client,
            supports_tool_calling=True,
        )

    try:
        await model.complete((Message(role="system", content="Use tools."), ATTACHMENT_MESSAGE))
    finally:
        await client.aclose()

    if provider == "anthropic":
        content = requests[0]["messages"][0]["content"]
        assert content[0] == {"type": "text", "text": "Inspect attachments."}
        assert content[1] == {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "aW1hZ2U=",
            },
        }
        assert content[2] == {
            "type": "text",
            "text": "[attachment:notes.md; media_type=text/markdown]\n# Notes",
        }
    else:
        parts = requests[0]["contents"][0]["parts"]
        assert parts[0] == {"text": "Inspect attachments."}
        assert parts[1] == {"inline_data": {"mime_type": "image/png", "data": "aW1hZ2U="}}
        assert parts[2] == {"text": "[attachment:notes.md; media_type=text/markdown]\n# Notes"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_kind"),
    [
        (401, ProviderErrorKind.AUTH),
        (429, ProviderErrorKind.RATE_LIMIT),
        (503, ProviderErrorKind.RETRYABLE),
    ],
)
async def test_http_statuses_map_to_stable_provider_errors(
    status_code: int, expected_kind: ProviderErrorKind
) -> None:
    client = make_json_client({"error": "injected failure"}, status_code=status_code)
    model = AnthropicModel(
        model="fake-model", api_key="test", client=client, supports_tool_calling=True
    )

    try:
        with pytest.raises(ProviderError) as caught:
            await model.complete(MESSAGES, (TOOL,))
    finally:
        await client.aclose()

    assert caught.value.kind == expected_kind
    assert caught.value.status_code == status_code
    assert caught.value.provider_name == "anthropic"


SECOND_TURN_MESSAGES = (
    Message(role="system", content="Use tools."),
    Message(role="user", content="Inspect both files."),
    Message(
        role="assistant",
        content="Checking now.",
        tool_calls=(
            ToolCall(
                tool_call_id="call-ok",
                name="read_file",
                arguments={"path": "src/good.py"},
            ),
            ToolCall(
                tool_call_id="call-failed",
                name="read_file",
                arguments={"path": "src/missing.py"},
            ),
        ),
    ),
    ToolObservation(
        tool_call_id="call-ok",
        name="read_file",
        ok=True,
        content="GOOD = True",
    ),
    ToolObservation(
        tool_call_id="call-failed",
        name="read_file",
        ok=False,
        error="PathPolicyError: denied",
    ),
)


@pytest.mark.asyncio
async def test_openai_second_turn_preserves_tool_call_ids_and_failed_observation() -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="Done.", tool_calls=[]),
                finish_reason="stop",
            )
        ],
        usage=None,
    )
    completions = FakeCompletions(response)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    model = OpenAICompatibleModel(model="fake-model", client=client, supports_tool_calling=True)

    await model.complete(SECOND_TURN_MESSAGES, (TOOL,))

    messages = completions.requests[0]["messages"]
    assistant = next(message for message in messages if message["role"] == "assistant")
    observations = [message for message in messages if message["role"] == "tool"]
    assert [call["id"] for call in assistant["tool_calls"]] == ["call-ok", "call-failed"]
    assert [message["tool_call_id"] for message in observations] == [
        "call-ok",
        "call-failed",
    ]
    assert observations[0]["content"] == "GOOD = True"
    assert "PathPolicyError: denied" in observations[1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["anthropic", "gemini", "workers-ai"])
async def test_http_provider_second_turn_preserves_success_and_failure_observations(
    provider: str,
) -> None:
    if provider == "anthropic":
        response = {
            "content": [{"type": "text", "text": "Done."}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        client, requests = make_capturing_json_client(response)
        model = AnthropicModel(
            model="fake-model", api_key="test", client=client, supports_tool_calling=True
        )
    elif provider == "gemini":
        response = {
            "candidates": [{"content": {"parts": [{"text": "Done."}]}}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
        }
        client, requests = make_capturing_json_client(response)
        model = GeminiModel(
            model="fake-model", api_key="test", client=client, supports_tool_calling=True
        )
    else:
        response = {"success": True, "result": {"response": "Done."}}
        client, requests = make_capturing_json_client(response)
        model = WorkersAIModel(
            account_id="account",
            api_token="test",
            model="fake-model",
            client=client,
            supports_tool_calling=True,
        )

    try:
        await model.complete(SECOND_TURN_MESSAGES, (TOOL,))
    finally:
        await client.aclose()

    serialized = json.dumps(requests[0], ensure_ascii=False)
    assert "call-ok" in serialized
    assert "call-failed" in serialized
    assert "GOOD = True" in serialized
    assert "PathPolicyError: denied" in serialized
    if provider == "anthropic":
        tool_results = [
            block
            for message in requests[0]["messages"]
            for block in message.get("content", [])
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        assert [block["is_error"] for block in tool_results] == [False, True]
    elif provider == "gemini":
        responses = [
            part["functionResponse"]
            for content in requests[0]["contents"]
            for part in content["parts"]
            if "functionResponse" in part
        ]
        assert [response["response"]["ok"] for response in responses] == [True, False]
    else:
        tool_messages = [
            message for message in requests[0]["messages"] if message["role"] == "tool"
        ]
        assert [message["tool_call_id"] for message in tool_messages] == [
            "call-ok",
            "call-failed",
        ]


@pytest.mark.asyncio
async def test_anthropic_model_marks_stable_prompt_prefix_for_cache_control() -> None:
    response = {
        "content": [{"type": "text", "text": "Done."}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    client, requests = make_capturing_json_client(response)
    model = AnthropicModel(
        model="fake-model", api_key="test", client=client, supports_tool_calling=True
    )
    prompt = render_prompt_sections(
        (
            PromptSection("core", "Stable rules", cache_stable=True),
            PromptSection("workspace", "Dynamic state"),
        )
    )

    try:
        await model.complete((Message(role="system", content=prompt),))
    finally:
        await client.aclose()

    system = requests[0]["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "Stable rules" in system[0]["text"]
    assert "cache_control" not in system[1]
    trace = provider_cache_trace("anthropic", requests[0])
    assert trace.cache_ready is True
    assert trace.cache_control_blocks == 1
    assert model.last_cache_trace == trace


@pytest.mark.asyncio
async def test_real_provider_adapters_default_to_tool_calling_disabled() -> None:
    fake_openai = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(None)))
    http_client = make_json_client({})
    models = (
        OpenAICompatibleModel(model="fake", client=fake_openai),
        AnthropicModel(model="fake", api_key="test", client=http_client),
        GeminiModel(model="fake", api_key="test", client=http_client),
        WorkersAIModel(account_id="account", api_token="test", model="fake", client=http_client),
    )

    try:
        assert all(model.capabilities.tool_calling is False for model in models)
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_injected_http_client_remains_caller_owned() -> None:
    client = make_json_client({})
    model = AnthropicModel(model="fake", api_key="test", client=client)

    await model.aclose()

    assert client.is_closed is False
    await client.aclose()


@pytest.mark.parametrize("model_class", [AnthropicModel, GeminiModel, WorkersAIModel])
def test_native_provider_rejects_custom_endpoint_without_explicit_opt_in(
    model_class: type[Any],
) -> None:
    common: dict[str, Any] = {
        "model": "fake",
        "base_url": "https://proxy.example.invalid",
    }
    if model_class is WorkersAIModel:
        common.update(account_id="account", api_token="test")
    else:
        common["api_key"] = "test"

    with pytest.raises(ValueError, match="custom provider endpoint"):
        model_class(**common)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    [
        "https://gateway.example.test/openai/v1",
        "http://localhost:11434/v1",
        "http://127.0.0.1:11434/v1/",
        "http://[::1]:11434/v1",
    ],
)
async def test_openai_compatible_accepts_https_and_loopback_http_urls(
    base_url: str,
) -> None:
    model = OpenAICompatibleModel(
        model="fake",
        api_key="test",
        base_url=base_url,
    )

    try:
        assert str(model._client.base_url).rstrip("/") == base_url.rstrip("/")
    finally:
        await model.aclose()


@pytest.mark.asyncio
async def test_openai_compatible_uses_dummy_key_for_keyless_loopback_ollama() -> None:
    model = OpenAICompatibleModel(
        model="qwen3-coder",
        base_url="http://127.0.0.1:11434/v1",
    )

    try:
        assert model._client.api_key == "local-openai-compatible"
    finally:
        await model.aclose()


@pytest.mark.asyncio
async def test_remote_ollama_compatible_endpoint_uses_explicit_api_key() -> None:
    model = OpenAICompatibleModel(
        model="remote-tool-model",
        api_key="fake-ollama-api-key",
        base_url="https://ollama-gateway.example.test/v1",
        provider_name="ollama",
        supports_tool_calling=True,
    )

    try:
        assert model.provider_name == "ollama"
        assert model.capabilities.tool_calling is True
        assert model._client.api_key == "fake-ollama-api-key"
        assert str(model._client.base_url).rstrip("/") == ("https://ollama-gateway.example.test/v1")
    finally:
        await model.aclose()


@pytest.mark.parametrize(
    ("base_url", "message"),
    [
        ("http://models.example.test/v1", "only allowed for a loopback host"),
        ("http://127.0.0.1.example.test/v1", "only allowed for a loopback host"),
        ("https://user:secret@models.example.test/v1", "must not contain credentials"),
        ("https://models.example.test/v1?tenant=one", "query or fragment"),
        ("https://models.example.test/v1?", "query or fragment"),
        ("https://models.example.test/v1#route", "query or fragment"),
        ("models.example.test/v1", "absolute HTTP\\(S\\) URL"),
        ("https://models.example.test:invalid/v1", "invalid port"),
    ],
)
def test_openai_compatible_rejects_unsafe_or_malformed_urls(
    base_url: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        OpenAICompatibleModel(
            model="fake",
            api_key="test",
            base_url=base_url,
        )


def test_openai_compatible_still_requires_key_for_remote_endpoint() -> None:
    with pytest.raises(ValueError, match="key or api_key is required"):
        OpenAICompatibleModel(
            model="fake",
            base_url="https://gateway.example.test/v1",
        )


@pytest.mark.parametrize(
    "credentials",
    [
        {"api_key": ""},
        {"api_key": "   "},
        {"key": "legacy", "api_key": "canonical"},
    ],
)
def test_openai_compatible_rejects_ambiguous_or_blank_credentials(
    credentials: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="not both|cannot be blank"):
        OpenAICompatibleModel(
            model="fake",
            base_url="https://gateway.example.test/v1",
            **credentials,
        )


def test_openai_compatible_validates_url_even_with_injected_client() -> None:
    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(None)))

    with pytest.raises(ValueError, match="only allowed for a loopback host"):
        OpenAICompatibleModel(
            model="fake",
            client=client,
            base_url="http://models.example.test/v1",
        )


@pytest.mark.asyncio
async def test_openai_compatible_passes_bounded_provider_options() -> None:
    completions = FakeCompletions(
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="done", tool_calls=[]),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )
    )
    fake_openai = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    model = OpenAICompatibleModel(
        model="qwen3:4b",
        client=fake_openai,
        supports_tool_calling=True,
        extra_body={"think": False},
        max_tokens=1_024,
        user_message_prefix="/no_think\n",
    )

    await model.complete([Message(role="user", content="test")])

    assert completions.requests[0]["extra_body"]["think"] is False
    assert completions.requests[0]["extra_body"]["prompt_cache_key"].startswith("looplane-openai:")
    trace = provider_cache_trace("openai-compatible", completions.requests[0])
    assert trace.cache_ready is True
    assert trace.prompt_cache_key is not None
    assert model.last_cache_trace == trace
    assert completions.requests[0]["max_tokens"] == 1_024
    assert completions.requests[0]["messages"][0]["content"].startswith("/no_think\n")


@pytest.mark.asyncio
async def test_openai_compatible_preserves_caller_prompt_cache_key() -> None:
    completions = FakeCompletions(
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="done", tool_calls=[]),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )
    )
    fake_openai = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    model = OpenAICompatibleModel(
        model="qwen3:4b",
        client=fake_openai,
        supports_tool_calling=True,
        extra_body={"prompt_cache_key": "caller:key"},
    )

    await model.complete([Message(role="user", content="test")])

    assert completions.requests[0]["extra_body"]["prompt_cache_key"] == "caller:key"
    trace = provider_cache_trace("openai-compatible", completions.requests[0])
    assert trace.prompt_cache_key == "caller:key"
    assert model.last_cache_trace == trace


def test_openai_compatible_provider_options_cannot_override_core_fields() -> None:
    with pytest.raises(ValueError, match="cannot override canonical"):
        OpenAICompatibleModel(
            model="fake",
            client=SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(None))),
            extra_body={"model": "different"},
        )


def test_openai_compatible_rejects_nonpositive_output_limit() -> None:
    with pytest.raises(ValueError, match="max_tokens must be positive"):
        OpenAICompatibleModel(
            model="fake",
            client=SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(None))),
            max_tokens=0,
        )


def test_openai_compatible_rejects_blank_user_prefix() -> None:
    with pytest.raises(ValueError, match="user_message_prefix cannot be blank"):
        OpenAICompatibleModel(
            model="fake",
            client=SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(None))),
            user_message_prefix="   ",
        )


@pytest.mark.asyncio
async def test_workers_ai_7505_is_classified_with_provider_diagnostics() -> None:
    client = make_json_client(
        {
            "success": False,
            "errors": [{"code": 7505, "message": "rate limited"}],
            "request_id": "request-7505",
        }
    )
    model = WorkersAIModel(
        account_id="account",
        api_token="test",
        model="fake",
        client=client,
        supports_tool_calling=True,
    )

    try:
        with pytest.raises(ProviderError) as caught:
            await model.complete(MESSAGES)
    finally:
        await client.aclose()

    assert caught.value.kind == ProviderErrorKind.RATE_LIMIT
    assert caught.value.provider_code == 7505
    assert caught.value.request_id == "request-7505"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_kind"),
    [
        (401, ProviderErrorKind.AUTH),
        (429, ProviderErrorKind.RATE_LIMIT),
        (503, ProviderErrorKind.RETRYABLE),
    ],
)
async def test_openai_status_errors_are_normalized(
    status_code: int, expected_kind: ProviderErrorKind
) -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status_code, request=request, headers={"retry-after": "2"})
    error = APIStatusError("injected failure", response=response, body={"error": "failure"})
    completions = FakeCompletions(error)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    model = OpenAICompatibleModel(model="fake", client=client, supports_tool_calling=True)

    with pytest.raises(ProviderError) as caught:
        await model.complete(MESSAGES)

    assert caught.value.kind == expected_kind
    assert caught.value.status_code == status_code
    assert caught.value.retry_after_seconds == 2


RESPONSES_PAYLOAD = {
    "id": "resp-1",
    "status": "completed",
    "output": [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "I will inspect the file."}],
        },
        {
            "type": "function_call",
            "call_id": "call-1",
            "name": "read_file",
            "arguments": json.dumps({"path": "src/example.py"}),
        },
    ],
    "usage": {
        "input_tokens": 11,
        "output_tokens": 7,
        "input_tokens_details": {"cached_tokens": 3},
        "output_tokens_details": {"reasoning_tokens": 2},
        "total_tokens": 18,
    },
}


@pytest.mark.asyncio
async def test_responses_model_text_tool_and_usage_roundtrip() -> None:
    client = make_json_client(RESPONSES_PAYLOAD)
    model = ResponsesModel(
        model="muse-spark-1.2-contributor-free",
        api_key="test",
        base_url="https://opencode.ai/zen/v1",
        client=client,
        allow_custom_endpoint=True,
        supports_tool_calling=True,
    )

    try:
        turn = await model.complete(MESSAGES, (TOOL,))
    finally:
        await client.aclose()

    assert_common_turn(turn)
    assert turn.tool_calls[0].tool_call_id == "call-1"
    assert turn.finish_reason == "tool_calls"
    assert turn.usage.cached_input_tokens == 3
    assert turn.usage.reasoning_tokens == 2


@pytest.mark.asyncio
async def test_responses_model_maps_message_attachments_to_native_content() -> None:
    client, requests = make_capturing_json_client(RESPONSES_PAYLOAD)
    model = ResponsesModel(
        model="gpt-5",
        api_key="test",
        client=client,
        supports_tool_calling=True,
    )

    try:
        await model.complete(
            (
                Message(role="system", content="Use tools."),
                ATTACHMENT_MESSAGE.model_copy(
                    update={
                        "provider_metadata": {
                            "attachments": [
                                {
                                    "name": "screenshot.png",
                                    "media_type": "image/png",
                                    "uri": "https://example.test/screenshot.png",
                                },
                                {
                                    "name": "design.pdf",
                                    "media_type": "application/pdf",
                                    "uri": "https://example.test/design.pdf",
                                },
                            ]
                        }
                    }
                ),
            )
        )
    finally:
        await client.aclose()

    content = requests[0]["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "Inspect attachments."}
    assert content[1] == {
        "type": "input_image",
        "image_url": "https://example.test/screenshot.png",
    }
    assert content[2] == {
        "type": "input_file",
        "file_url": "https://example.test/design.pdf",
    }


@pytest.mark.asyncio
async def test_responses_model_translates_canonical_request_shape() -> None:
    client, requests = make_capturing_json_client(RESPONSES_PAYLOAD)
    model = ResponsesModel(
        model="muse-spark-1.2-contributor-free",
        api_key="test",
        base_url="https://opencode.ai/zen/v1",
        client=client,
        allow_custom_endpoint=True,
        supports_tool_calling=True,
    )

    try:
        await model.complete(SECOND_TURN_MESSAGES, (TOOL,))
    finally:
        await client.aclose()

    request = requests[0]
    assert request["prompt_cache_key"].startswith("looplane-responses:")
    trace = provider_cache_trace("openai-responses", request)
    assert trace.cache_ready is True
    assert trace.tool_schema_fingerprint is not None
    assert model.last_cache_trace == trace
    # system messages hoist into top-level instructions
    assert request["instructions"] == "Use tools."
    # tools flatten one level versus Chat Completions
    assert request["tools"] == [
        {
            "type": "function",
            "name": "read_file",
            "description": TOOL.description,
            "parameters": TOOL.input_schema,
        }
    ]
    # assistant tool_calls and observations survive as paired items
    function_calls = [item for item in request["input"] if item.get("type") == "function_call"]
    outputs = [item for item in request["input"] if item.get("type") == "function_call_output"]
    assert [call["call_id"] for call in function_calls] == ["call-ok", "call-failed"]
    assert [item["call_id"] for item in outputs] == ["call-ok", "call-failed"]
    assert outputs[0]["output"] == "GOOD = True"
    assert "PathPolicyError: denied" in outputs[1]["output"]


@pytest.mark.asyncio
async def test_responses_model_incomplete_maps_to_length_finish_reason() -> None:
    payload = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "partial"}],
            }
        ],
        "usage": {"input_tokens": 5, "output_tokens": 10},
    }
    client = make_json_client(payload)
    model = ResponsesModel(
        model="fake",
        api_key="test",
        base_url="https://opencode.ai/zen/v1",
        client=client,
        allow_custom_endpoint=True,
        supports_tool_calling=True,
    )

    try:
        turn = await model.complete(MESSAGES)
    finally:
        await client.aclose()

    assert turn.finish_reason == "length"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_kind"),
    [
        (401, ProviderErrorKind.AUTH),
        (429, ProviderErrorKind.RATE_LIMIT),
        (408, ProviderErrorKind.RETRYABLE),
        (409, ProviderErrorKind.RETRYABLE),
        (503, ProviderErrorKind.RETRYABLE),
        (529, ProviderErrorKind.RETRYABLE),
    ],
)
async def test_responses_model_status_errors_are_normalized(
    status_code: int, expected_kind: ProviderErrorKind
) -> None:
    client = make_json_client({"error": "injected failure"}, status_code=status_code)
    model = ResponsesModel(
        model="fake",
        api_key="test",
        base_url="https://opencode.ai/zen/v1",
        client=client,
        allow_custom_endpoint=True,
        supports_tool_calling=True,
    )

    try:
        with pytest.raises(ProviderError) as caught:
            await model.complete(MESSAGES)
    finally:
        await client.aclose()

    assert caught.value.kind == expected_kind
    assert caught.value.status_code == status_code
    assert caught.value.provider_name == "openai-responses"


def _response(status_code: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status_code,
        json={"error": "injected"},
        headers=headers or {},
        request=httpx.Request("POST", "https://provider.test/v1"),
    )


@pytest.mark.parametrize(
    ("headers", "expected"),
    (
        ({"retry-after": "7"}, 7.0),
        ({"retry-after-ms": "250"}, 0.25),
        ({"retry-after-ms": "250", "retry-after": "7"}, 0.25),
        ({}, None),
        ({"retry-after": "soon"}, None),
    ),
)
def test_retry_after_prefers_millisecond_header(headers: dict[str, str], expected: float | None):
    assert _retry_after(headers) == expected


def test_x_should_retry_false_downgrades_5xx_to_non_retryable() -> None:
    error = _http_error("test", _response(503, {"x-should-retry": "false"}))
    assert error.kind is ProviderErrorKind.PROVIDER
    assert error.retryable is False


def test_x_should_retry_true_upgrades_invalid_request_to_retryable() -> None:
    error = _http_error("test", _response(400, {"x-should-retry": "true"}))
    assert error.kind is ProviderErrorKind.RETRYABLE
    assert error.retryable is True


def test_x_should_retry_does_not_override_auth() -> None:
    error = _http_error("test", _response(401, {"x-should-retry": "true"}))
    assert error.kind is ProviderErrorKind.AUTH
    assert error.retryable is False
