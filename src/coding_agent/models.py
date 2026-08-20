"""Provider adapters for the canonical coding-agent model contract."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from coding_agent.contracts import (
    ConversationItem,
    ModelCapabilities,
    ModelProtocol,
    ModelTurn,
    ToolCall,
    ToolDefinition,
    ToolObservation,
    Usage,
)


class ProviderErrorKind(StrEnum):
    """Stable failure categories understood by retry/orchestration policy."""

    RETRYABLE = "retryable"
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    INVALID_REQUEST = "invalid_request"
    PROVIDER = "provider"


class ProviderError(RuntimeError):
    """A provider failure normalized without exposing an SDK exception."""

    def __init__(
        self,
        message: str,
        *,
        kind: ProviderErrorKind,
        provider_name: str,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        provider_code: str | int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.provider_name = provider_name
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.provider_code = provider_code
        self.request_id = request_id

    @property
    def retryable(self) -> bool:
        return self.kind in {ProviderErrorKind.RETRYABLE, ProviderErrorKind.RATE_LIMIT}


@runtime_checkable
class ModelProvider(Protocol):
    """Canonical, non-streaming provider boundary consumed by the agent loop."""

    provider_name: str
    model_id: str
    protocol: ModelProtocol
    capabilities: ModelCapabilities

    async def complete(
        self,
        messages: Sequence[ConversationItem],
        tools: Sequence[ToolDefinition] = (),
    ) -> ModelTurn: ...

    async def aclose(self) -> None: ...


def _capabilities(
    capabilities: ModelCapabilities | None,
    supports_tool_calling: bool | None,
) -> ModelCapabilities:
    if capabilities is not None and supports_tool_calling is not None:
        raise ValueError("pass capabilities or supports_tool_calling, not both")
    if capabilities is not None:
        return capabilities
    return ModelCapabilities(
        tool_calling=bool(supports_tool_calling),
        streaming=False,
        structured_output=False,
    )


def _validated_native_base_url(
    base_url: str,
    *,
    official_base_url: str,
    allow_custom_endpoint: bool,
) -> str:
    normalized = base_url.rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("provider base_url must be an absolute HTTPS URL")
    if normalized != official_base_url.rstrip("/") and not allow_custom_endpoint:
        raise ValueError(
            "custom provider endpoint requires allow_custom_endpoint=True; "
            "credentials would otherwise be sent to an untrusted host"
        )
    return normalized


def _validated_openai_base_url(base_url: str | None) -> str | None:
    if base_url is None:
        return None
    normalized = base_url.rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ValueError("base_url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url must not contain credentials")
    if "?" in normalized or "#" in normalized:
        raise ValueError("base_url must not contain a query or fragment")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("base_url contains an invalid port") from exc
    if parsed.scheme == "http" and not _is_loopback_base_url(normalized):
        raise ValueError("HTTP base_url is only allowed for a loopback host")
    return normalized


def _is_loopback_base_url(base_url: str) -> bool:
    hostname = urlsplit(base_url).hostname
    return hostname is not None and hostname.lower() in {"localhost", "127.0.0.1", "::1"}


def _error_kind(status_code: int | None) -> ProviderErrorKind:
    if status_code in {401, 403}:
        return ProviderErrorKind.AUTH
    if status_code == 429:
        return ProviderErrorKind.RATE_LIMIT
    if status_code in {400, 404, 405, 409, 415, 422}:
        return ProviderErrorKind.INVALID_REQUEST
    if status_code is not None and status_code >= 500:
        return ProviderErrorKind.RETRYABLE
    return ProviderErrorKind.PROVIDER


def _retry_after(headers: Mapping[str, str]) -> float | None:
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _http_error(provider_name: str, response: httpx.Response) -> ProviderError:
    try:
        detail = response.json()
    except ValueError:
        detail = response.text
    return ProviderError(
        f"{provider_name} request failed ({response.status_code}): {detail}",
        kind=_error_kind(response.status_code),
        provider_name=provider_name,
        status_code=response.status_code,
        retry_after_seconds=_retry_after(response.headers),
        request_id=(
            response.headers.get("x-request-id")
            or response.headers.get("request-id")
            or response.headers.get("cf-ray")
        ),
    )


async def _post_json(
    client: httpx.AsyncClient,
    *,
    provider_name: str,
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        response = await client.post(url, headers=headers, json=payload)
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise ProviderError(
            f"{provider_name} transport failed: {exc}",
            kind=ProviderErrorKind.RETRYABLE,
            provider_name=provider_name,
        ) from exc
    if response.is_error:
        raise _http_error(provider_name, response)
    try:
        body = response.json()
    except ValueError as exc:
        raise ProviderError(
            f"{provider_name} returned invalid JSON",
            kind=ProviderErrorKind.PROVIDER,
            provider_name=provider_name,
            status_code=response.status_code,
        ) from exc
    if not isinstance(body, dict):
        raise ProviderError(
            f"{provider_name} returned a non-object response",
            kind=ProviderErrorKind.PROVIDER,
            provider_name=provider_name,
            status_code=response.status_code,
        )
    return body


def _parse_arguments(value: Any, *, provider_name: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"{provider_name} returned malformed tool arguments",
                kind=ProviderErrorKind.PROVIDER,
                provider_name=provider_name,
            ) from exc
        if isinstance(parsed, dict):
            return parsed
    raise ProviderError(
        f"{provider_name} returned non-object tool arguments",
        kind=ProviderErrorKind.PROVIDER,
        provider_name=provider_name,
    )


def _observation_content(observation: ToolObservation) -> str:
    if observation.ok:
        return observation.content
    return json.dumps(
        {"ok": False, "content": observation.content, "error": observation.error},
        ensure_ascii=False,
    )


def _openai_tools(tools: Sequence[ToolDefinition]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }
        for tool in tools
    ]


def _openai_messages(messages: Sequence[ConversationItem]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in messages:
        if isinstance(item, ToolObservation):
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": item.tool_call_id,
                    "content": _observation_content(item),
                }
            )
            continue
        message: dict[str, Any] = {"role": item.role, "content": item.content}
        if item.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in item.tool_calls
            ]
        result.append(message)
    return result


class ScriptedModel:
    """Deterministic provider for contract tests and offline agent runs."""

    provider_name = "scripted"
    protocol = ModelProtocol.SCRIPTED
    capabilities = ModelCapabilities(
        tool_calling=True,
        streaming=False,
        structured_output=False,
    )

    def __init__(
        self,
        turns: Iterable[ModelTurn | ProviderError],
        *,
        model_id: str = "scripted",
    ) -> None:
        self.model_id = model_id
        self._turns = deque(turns)
        self.calls: list[tuple[tuple[ConversationItem, ...], tuple[ToolDefinition, ...]]] = []

    async def complete(
        self,
        messages: Sequence[ConversationItem],
        tools: Sequence[ToolDefinition] = (),
    ) -> ModelTurn:
        self.calls.append((tuple(messages), tuple(tools)))
        if not self._turns:
            raise ProviderError(
                "scripted model has no remaining turns",
                kind=ProviderErrorKind.PROVIDER,
                provider_name=self.provider_name,
            )
        result = self._turns.popleft()
        if isinstance(result, ProviderError):
            raise result
        return result

    async def aclose(self) -> None:
        """No-op lifecycle hook matching real providers."""


class OpenAICompatibleModel:
    """Adapter for OpenAI and compatible Chat Completions endpoints."""

    provider_name = "openai-compatible"
    protocol = ModelProtocol.OPENAI_CHAT

    def __init__(
        self,
        *,
        model: str,
        key: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        client: Any | None = None,
        capabilities: ModelCapabilities | None = None,
        supports_tool_calling: bool | None = None,
        provider_name: str = "openai-compatible",
        extra_body: Mapping[str, Any] | None = None,
        max_tokens: int | None = None,
        user_message_prefix: str | None = None,
    ) -> None:
        validated_base_url = _validated_openai_base_url(base_url)
        if key is not None and api_key is not None:
            raise ValueError("pass key or api_key, not both")
        supplied_api_key = api_key if api_key is not None else key
        if supplied_api_key is not None and not supplied_api_key.strip():
            raise ValueError("api_key cannot be blank")
        is_loopback = (
            validated_base_url is not None
            and _is_loopback_base_url(validated_base_url)
        )
        if not supplied_api_key and client is None and not is_loopback:
            raise ValueError("key or api_key is required when client is not supplied")
        self.provider_name = provider_name
        self.model_id = model
        self.capabilities = _capabilities(capabilities, supports_tool_calling)
        self._extra_body = dict(extra_body or {})
        if max_tokens is not None and max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        self._max_tokens = max_tokens
        if user_message_prefix is not None and not user_message_prefix.strip():
            raise ValueError("user_message_prefix cannot be blank")
        self._user_message_prefix = user_message_prefix
        reserved = {"model", "messages", "tools"}.intersection(self._extra_body)
        if reserved:
            raise ValueError(
                f"extra_body cannot override canonical request fields: {sorted(reserved)}"
            )
        self._owns_client = client is None
        self._client = client or AsyncOpenAI(
            # OpenAI-compatible local servers such as Ollama do not authenticate,
            # while the SDK requires a non-empty value. This placeholder is only
            # synthesized for an explicit loopback endpoint.
            api_key=supplied_api_key or "local-openai-compatible",
            base_url=validated_base_url,
        )

    async def complete(
        self,
        messages: Sequence[ConversationItem],
        tools: Sequence[ToolDefinition] = (),
    ) -> ModelTurn:
        native_messages = _openai_messages(messages)
        if self._user_message_prefix:
            for message in native_messages:
                if message.get("role") == "user" and isinstance(message.get("content"), str):
                    message["content"] = f"{self._user_message_prefix}{message['content']}"
                    break
        request: dict[str, Any] = {
            "model": self.model_id,
            "messages": native_messages,
        }
        if tools:
            request["tools"] = _openai_tools(tools)
        if self._extra_body:
            request["extra_body"] = self._extra_body
        if self._max_tokens is not None:
            request["max_tokens"] = self._max_tokens
        try:
            response = await self._client.chat.completions.create(**request)
        except (APIConnectionError, APITimeoutError, APIStatusError) as exc:
            status_code = getattr(exc, "status_code", None)
            response_value = getattr(exc, "response", None)
            headers = getattr(response_value, "headers", {})
            kind = _error_kind(status_code)
            if isinstance(exc, (APIConnectionError, APITimeoutError)):
                kind = ProviderErrorKind.RETRYABLE
            raise ProviderError(
                f"{self.provider_name} request failed: {exc}",
                kind=kind,
                provider_name=self.provider_name,
                status_code=status_code,
                retry_after_seconds=_retry_after(headers),
            ) from exc
        choices = getattr(response, "choices", None)
        if not choices:
            raise ProviderError(
                "openai-compatible response contained no choices",
                kind=ProviderErrorKind.PROVIDER,
                provider_name=self.provider_name,
            )
        choice = choices[0]
        message = choice.message
        calls = tuple(
            ToolCall(
                tool_call_id=call.id,
                name=call.function.name,
                arguments=_parse_arguments(
                    call.function.arguments,
                    provider_name=self.provider_name,
                ),
            )
            for call in (message.tool_calls or ())
        )
        raw_usage = getattr(response, "usage", None)
        details = getattr(raw_usage, "prompt_tokens_details", None)
        completion_details = getattr(raw_usage, "completion_tokens_details", None)
        usage = Usage(
            input_tokens=getattr(raw_usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(raw_usage, "completion_tokens", 0) or 0,
            cached_input_tokens=getattr(details, "cached_tokens", 0) or 0,
            reasoning_tokens=getattr(completion_details, "reasoning_tokens", 0) or 0,
            provider_total_tokens=getattr(raw_usage, "total_tokens", None),
        )
        return ModelTurn(
            content=message.content,
            tool_calls=calls,
            usage=usage,
            finish_reason=getattr(choice, "finish_reason", None),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.close()


class _HttpModel:
    provider_name: str

    def __init__(self, client: httpx.AsyncClient | None) -> None:
        self._owns_client = client is None
        self._http = client or httpx.AsyncClient(timeout=60.0)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()


class AnthropicModel(_HttpModel):
    """Native Anthropic Messages API adapter."""

    provider_name = "anthropic"
    protocol = ModelProtocol.ANTHROPIC_MESSAGES

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        max_tokens: int = 4096,
        base_url: str = "https://api.anthropic.com",
        anthropic_version: str = "2023-06-01",
        client: httpx.AsyncClient | None = None,
        supports_tool_calling: bool | None = None,
        capabilities: ModelCapabilities | None = None,
        allow_custom_endpoint: bool = False,
    ) -> None:
        validated_base_url = _validated_native_base_url(
            base_url,
            official_base_url="https://api.anthropic.com",
            allow_custom_endpoint=allow_custom_endpoint,
        )
        super().__init__(client)
        self.model_id = model
        self._api_key = api_key
        self.max_tokens = max_tokens
        self.base_url = validated_base_url
        self.anthropic_version = anthropic_version
        self.capabilities = _capabilities(capabilities, supports_tool_calling)

    async def complete(
        self,
        messages: Sequence[ConversationItem],
        tools: Sequence[ToolDefinition] = (),
    ) -> ModelTurn:
        system, native_messages = _anthropic_messages(messages)
        payload: dict[str, Any] = {
            "model": self.model_id,
            "max_tokens": self.max_tokens,
            "messages": native_messages,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in tools
            ]
        body = await _post_json(
            self._http,
            provider_name=self.provider_name,
            url=f"{self.base_url}/v1/messages",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": self.anthropic_version,
            },
            payload=payload,
        )
        blocks = body.get("content", [])
        text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
        calls = tuple(
            ToolCall(
                tool_call_id=str(block["id"]),
                name=str(block["name"]),
                arguments=_parse_arguments(
                    block.get("input", {}),
                    provider_name=self.provider_name,
                ),
            )
            for block in blocks
            if block.get("type") == "tool_use"
        )
        raw_usage = body.get("usage") or {}
        cached_read = raw_usage.get("cache_read_input_tokens", 0)
        cached_created = raw_usage.get("cache_creation_input_tokens", 0)
        inclusive_input = raw_usage.get("input_tokens", 0) + cached_read + cached_created
        output_tokens = raw_usage.get("output_tokens", 0)
        return ModelTurn(
            content=text or None,
            tool_calls=calls,
            usage=Usage(
                input_tokens=inclusive_input,
                output_tokens=output_tokens,
                cached_input_tokens=cached_read,
                provider_total_tokens=raw_usage.get(
                    "total_tokens", inclusive_input + output_tokens
                ),
            ),
            finish_reason=body.get("stop_reason"),
        )


def _anthropic_messages(
    messages: Sequence[ConversationItem],
) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    result: list[dict[str, Any]] = []
    for item in messages:
        if isinstance(item, ToolObservation):
            result.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": item.tool_call_id,
                            "content": _observation_content(item),
                            "is_error": not item.ok,
                        }
                    ],
                }
            )
        elif item.role == "system":
            system_parts.append(item.content or "")
        elif item.role == "assistant" and item.tool_calls:
            content: list[dict[str, Any]] = []
            if item.content:
                content.append({"type": "text", "text": item.content})
            content.extend(
                {
                    "type": "tool_use",
                    "id": call.tool_call_id,
                    "name": call.name,
                    "input": call.arguments,
                }
                for call in item.tool_calls
            )
            result.append({"role": "assistant", "content": content})
        else:
            result.append({"role": item.role, "content": item.content})
    return "\n\n".join(system_parts) or None, result


class GeminiModel(_HttpModel):
    """Native Google Gemini generateContent adapter."""

    provider_name = "gemini"
    protocol = ModelProtocol.GEMINI_GENERATE_CONTENT

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        client: httpx.AsyncClient | None = None,
        supports_tool_calling: bool | None = None,
        capabilities: ModelCapabilities | None = None,
        allow_custom_endpoint: bool = False,
    ) -> None:
        validated_base_url = _validated_native_base_url(
            base_url,
            official_base_url="https://generativelanguage.googleapis.com/v1beta",
            allow_custom_endpoint=allow_custom_endpoint,
        )
        super().__init__(client)
        self.model_id = model
        self._api_key = api_key
        self.base_url = validated_base_url
        self.capabilities = _capabilities(capabilities, supports_tool_calling)

    async def complete(
        self,
        messages: Sequence[ConversationItem],
        tools: Sequence[ToolDefinition] = (),
    ) -> ModelTurn:
        system, contents = _gemini_messages(messages)
        payload: dict[str, Any] = {"contents": contents}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.input_schema,
                        }
                        for tool in tools
                    ]
                }
            ]
        body = await _post_json(
            self._http,
            provider_name=self.provider_name,
            url=f"{self.base_url}/models/{self.model_id}:generateContent",
            headers={"x-goog-api-key": self._api_key},
            payload=payload,
        )
        candidates = body.get("candidates") or []
        if not candidates:
            raise ProviderError(
                "gemini response contained no candidates",
                kind=ProviderErrorKind.PROVIDER,
                provider_name=self.provider_name,
            )
        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts if "text" in part)
        calls: list[ToolCall] = []
        for part in parts:
            function_call = part.get("functionCall")
            if function_call:
                function_metadata = {
                    key: value
                    for key, value in function_call.items()
                    if key not in {"name", "args"}
                }
                part_metadata = {
                    key: value
                    for key, value in part.items()
                    if key not in {"functionCall", "text"}
                }
                provider_metadata = {
                    "gemini": {
                        "function_call": function_metadata,
                        "part": part_metadata,
                    }
                }
                tool_call_id = function_call.get("id")
                call_arguments: dict[str, Any] = {
                    "name": str(function_call["name"]),
                    "arguments": _parse_arguments(
                        function_call.get("args", {}), provider_name=self.provider_name
                    ),
                    "provider_metadata": provider_metadata,
                }
                if tool_call_id:
                    call_arguments["tool_call_id"] = str(tool_call_id)
                calls.append(ToolCall(**call_arguments))
        raw_usage = body.get("usageMetadata") or {}
        reasoning_tokens = raw_usage.get("thoughtsTokenCount", 0)
        output_tokens = raw_usage.get("candidatesTokenCount", 0) + reasoning_tokens
        return ModelTurn(
            content=text or None,
            tool_calls=tuple(calls),
            usage=Usage(
                input_tokens=raw_usage.get("promptTokenCount", 0),
                output_tokens=output_tokens,
                cached_input_tokens=raw_usage.get("cachedContentTokenCount", 0),
                reasoning_tokens=reasoning_tokens,
                provider_total_tokens=raw_usage.get("totalTokenCount"),
            ),
            finish_reason=candidate.get("finishReason"),
        )


def _gemini_messages(
    messages: Sequence[ConversationItem],
) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    result: list[dict[str, Any]] = []
    for item in messages:
        if isinstance(item, ToolObservation):
            result.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "id": item.tool_call_id,
                                "name": item.name,
                                "response": {
                                    "ok": item.ok,
                                    "content": item.content,
                                    "error": item.error,
                                },
                            }
                        }
                    ],
                }
            )
        elif item.role == "system":
            system_parts.append(item.content or "")
        else:
            parts: list[dict[str, Any]] = []
            if item.content:
                parts.append({"text": item.content})
            parts.extend(_gemini_tool_call_part(call) for call in item.tool_calls)
            result.append({"role": "model" if item.role == "assistant" else "user", "parts": parts})
    return "\n\n".join(system_parts) or None, result


def _gemini_tool_call_part(call: ToolCall) -> dict[str, Any]:
    metadata = call.provider_metadata.get("gemini", {})
    function_call = dict(metadata.get("function_call", {}))
    function_call.update({"name": call.name, "args": call.arguments})
    part = dict(metadata.get("part", {}))
    part["functionCall"] = function_call
    return part


class WorkersAIModel(_HttpModel):
    """Cloudflare Workers AI REST adapter for text-generation models."""

    provider_name = "workers-ai"
    protocol = ModelProtocol.WORKERS_AI_RUN

    def __init__(
        self,
        *,
        account_id: str,
        api_token: str,
        model: str,
        base_url: str = "https://api.cloudflare.com/client/v4",
        client: httpx.AsyncClient | None = None,
        supports_tool_calling: bool | None = None,
        capabilities: ModelCapabilities | None = None,
        allow_custom_endpoint: bool = False,
    ) -> None:
        validated_base_url = _validated_native_base_url(
            base_url,
            official_base_url="https://api.cloudflare.com/client/v4",
            allow_custom_endpoint=allow_custom_endpoint,
        )
        super().__init__(client)
        self.account_id = account_id
        self._api_token = api_token
        self.model_id = model
        self.base_url = validated_base_url
        self.capabilities = _capabilities(capabilities, supports_tool_calling)

    async def complete(
        self,
        messages: Sequence[ConversationItem],
        tools: Sequence[ToolDefinition] = (),
    ) -> ModelTurn:
        if tools and not self.capabilities.tool_calling:
            raise ProviderError(
                f"Workers AI model {self.model_id!r} is not configured for tool calling",
                kind=ProviderErrorKind.INVALID_REQUEST,
                provider_name=self.provider_name,
            )
        payload: dict[str, Any] = {"messages": _openai_messages(messages)}
        if tools:
            payload["tools"] = _openai_tools(tools)
        body = await _post_json(
            self._http,
            provider_name=self.provider_name,
            url=f"{self.base_url}/accounts/{self.account_id}/ai/run/{self.model_id}",
            headers={"authorization": f"Bearer {self._api_token}"},
            payload=payload,
        )
        if body.get("success") is False:
            raise _workers_envelope_error(body)
        result = body.get("result", body)
        if not isinstance(result, dict):
            raise ProviderError(
                "Workers AI returned a non-object result",
                kind=ProviderErrorKind.PROVIDER,
                provider_name=self.provider_name,
            )
        calls = tuple(_workers_tool_call(call) for call in (result.get("tool_calls") or ()))
        raw_usage = result.get("usage") or body.get("usage") or {}
        content = result.get("response") or result.get("text")
        return ModelTurn(
            content=content,
            tool_calls=calls,
            usage=Usage(
                input_tokens=raw_usage.get("prompt_tokens", raw_usage.get("input_tokens", 0)),
                output_tokens=raw_usage.get(
                    "completion_tokens", raw_usage.get("output_tokens", 0)
                ),
                provider_total_tokens=raw_usage.get("total_tokens"),
            ),
            finish_reason=result.get("finish_reason"),
        )


def _workers_tool_call(call: Mapping[str, Any]) -> ToolCall:
    function = call.get("function") or call
    tool_call_id = call.get("id") or call.get("tool_call_id")
    arguments: dict[str, Any] = {
        "name": str(function["name"]),
        "arguments": _parse_arguments(
            function.get("arguments", {}),
            provider_name="workers-ai",
        ),
    }
    if tool_call_id:
        arguments["tool_call_id"] = str(tool_call_id)
    return ToolCall(**arguments)


def _workers_envelope_error(body: Mapping[str, Any]) -> ProviderError:
    errors = body.get("errors") or ()
    first = errors[0] if isinstance(errors, list) and errors else {}
    if not isinstance(first, Mapping):
        first = {}
    code = first.get("code")
    try:
        numeric_code = int(code) if code is not None else None
    except (TypeError, ValueError):
        numeric_code = None
    if numeric_code == 7505:
        kind = ProviderErrorKind.RATE_LIMIT
    elif numeric_code in {7502, 7504, 7506}:
        kind = ProviderErrorKind.INVALID_REQUEST
    elif numeric_code in {10000, 9106, 9109}:
        kind = ProviderErrorKind.AUTH
    else:
        kind = ProviderErrorKind.PROVIDER
    result_info = body.get("result_info")
    if not isinstance(result_info, Mapping):
        result_info = {}
    request_id = (
        body.get("request_id")
        or body.get("ray_id")
        or first.get("request_id")
        or result_info.get("request_id")
    )
    return ProviderError(
        f"Workers AI request failed: {errors}",
        kind=kind,
        provider_name="workers-ai",
        provider_code=code,
        request_id=str(request_id) if request_id else None,
    )
