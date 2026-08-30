"""A bounded OpenAI Chat Completions gateway over the canonical model contract.

This module intentionally translates the incoming wire format into
``ConversationItem`` values before invoking a provider.  It is not an arbitrary
HTTP passthrough and therefore cannot be used to select an upstream URL.
"""

from __future__ import annotations

import hmac
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from http import HTTPStatus
from typing import Any
from uuid import uuid4

from looplane.contracts import (
    ConversationItem,
    Message,
    ModelTurn,
    ToolCall,
    ToolDefinition,
    ToolObservation,
)
from looplane.models import ModelProvider, ProviderError, ProviderErrorKind

ASGIScope = Mapping[str, Any]
ASGIReceive = Callable[[], Awaitable[dict[str, Any]]]
ASGISend = Callable[[dict[str, Any]], Awaitable[None]]


class GatewayRequestError(ValueError):
    """A safe client-facing error raised while decoding the foreign wire."""

    def __init__(self, message: str, *, status_code: int = 400, code: str = "invalid_request"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class ModelGateway:
    """Minimal pure-ASGI model gateway for one configured provider/model.

    The containing server should bind to a loopback address by default.  When
    ``bearer_token`` is configured, every ``/v1`` request must authenticate;
    ``/healthz`` intentionally remains usable as a non-sensitive liveness probe.
    """

    def __init__(
        self,
        provider: ModelProvider,
        *,
        bearer_token: str | None = None,
        max_request_bytes: int = 1_048_576,
    ) -> None:
        if bearer_token is not None and not bearer_token:
            raise ValueError("bearer_token cannot be blank")
        if max_request_bytes < 1:
            raise ValueError("max_request_bytes must be positive")
        self.provider = provider
        self.bearer_token = bearer_token
        self.max_request_bytes = max_request_bytes

    async def __call__(self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        if scope.get("type") == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope.get("type") != "http":
            return

        method = str(scope.get("method", "")).upper()
        path = str(scope.get("path", ""))
        try:
            if path == "/healthz":
                if method != "GET":
                    raise GatewayRequestError("method not allowed", status_code=405)
                await self._send_json(send, 200, {"status": "ok"})
                return

            if path not in {"/v1/models", "/v1/chat/completions"}:
                raise GatewayRequestError("route not found", status_code=404, code="not_found")
            self._authorize(scope)

            if path == "/v1/models":
                if method != "GET":
                    raise GatewayRequestError("method not allowed", status_code=405)
                await self._send_json(send, 200, self._models_response())
                return

            if method != "POST":
                raise GatewayRequestError("method not allowed", status_code=405)
            payload = await self._read_json(scope, receive)
            messages, tools = self._parse_chat_request(payload)
            turn = await self.provider.complete(messages, tools)
            await self._send_json(send, 200, self._encode_turn(turn))
        except GatewayRequestError as exc:
            await self._send_error(send, exc.status_code, str(exc), exc.code)
        except ProviderError as exc:
            status = _provider_status(exc.kind)
            await self._send_error(send, status, "upstream model request failed", "upstream_error")
        except Exception:
            # Gateway errors deliberately do not reflect exception strings. SDK
            # exceptions can contain request headers, endpoint credentials, or
            # provider response bodies.
            await self._send_error(send, 502, "upstream model request failed", "upstream_error")

    async def _lifespan(self, receive: ASGIReceive, send: ASGISend) -> None:
        while True:
            message = await receive()
            kind = message.get("type")
            if kind == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif kind == "lifespan.shutdown":
                try:
                    await self.provider.aclose()
                except Exception as exc:
                    await send({"type": "lifespan.shutdown.failed", "message": type(exc).__name__})
                else:
                    await send({"type": "lifespan.shutdown.complete"})
                return

    def _authorize(self, scope: ASGIScope) -> None:
        if self.bearer_token is None:
            return
        headers = _headers(scope)
        supplied = headers.get("authorization", "")
        prefix = "Bearer "
        if not supplied.startswith(prefix) or not hmac.compare_digest(
            supplied[len(prefix) :], self.bearer_token
        ):
            raise GatewayRequestError("unauthorized", status_code=401, code="unauthorized")

    async def _read_json(self, scope: ASGIScope, receive: ASGIReceive) -> dict[str, Any]:
        headers = _headers(scope)
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise GatewayRequestError("invalid content-length") from exc
            if declared_size < 0:
                raise GatewayRequestError("invalid content-length")
            if declared_size > self.max_request_bytes:
                raise GatewayRequestError(
                    "request body is too large", status_code=413, code="request_too_large"
                )

        body = bytearray()
        more_body = True
        while more_body:
            message = await receive()
            if message.get("type") == "http.disconnect":
                raise GatewayRequestError("request disconnected")
            if message.get("type") != "http.request":
                continue
            chunk = message.get("body", b"")
            if not isinstance(chunk, bytes):
                raise GatewayRequestError("invalid request body")
            if len(body) + len(chunk) > self.max_request_bytes:
                raise GatewayRequestError(
                    "request body is too large", status_code=413, code="request_too_large"
                )
            body.extend(chunk)
            more_body = bool(message.get("more_body", False))
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GatewayRequestError("request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise GatewayRequestError("request body must be a JSON object")
        return payload

    def _parse_chat_request(
        self, payload: Mapping[str, Any]
    ) -> tuple[tuple[ConversationItem, ...], tuple[ToolDefinition, ...]]:
        if payload.get("stream", False) is not False:
            raise GatewayRequestError("stream=true is not supported", code="unsupported_streaming")

        model = payload.get("model")
        if not isinstance(model, str) or not model:
            raise GatewayRequestError("model must be a non-empty string")
        if model != self.provider.model_id:
            raise GatewayRequestError("requested model is not available", status_code=404)

        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise GatewayRequestError("messages must be a non-empty array")
        tools = _parse_tools(payload.get("tools", []))
        messages = _parse_messages(raw_messages)
        return messages, tools

    def _models_response(self) -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": self.provider.model_id,
                    "object": "model",
                    "owned_by": self.provider.provider_name,
                }
            ],
        }

    def _encode_turn(self, turn: ModelTurn) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": turn.content}
        if turn.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments, ensure_ascii=False, separators=(",", ":")
                        ),
                    },
                }
                for call in turn.tool_calls
            ]
        return {
            "id": f"chatcmpl-{uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.provider.model_id,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": turn.finish_reason
                    or ("tool_calls" if turn.tool_calls else "stop"),
                }
            ],
            "usage": {
                "prompt_tokens": turn.usage.input_tokens,
                "completion_tokens": turn.usage.output_tokens,
                "total_tokens": turn.usage.total_tokens,
            },
        }

    async def _send_error(self, send: ASGISend, status: int, message: str, code: str) -> None:
        await self._send_json(
            send,
            status,
            {
                "error": {
                    "message": message,
                    "type": "invalid_request_error" if status < 500 else "upstream_error",
                    "code": code,
                }
            },
        )

    async def _send_json(self, send: ASGISend, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        if status == HTTPStatus.UNAUTHORIZED:
            headers.append((b"www-authenticate", b"Bearer"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def _headers(scope: ASGIScope) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers", []):
        try:
            name = raw_name.decode("latin-1").lower()
            value = raw_value.decode("latin-1")
        except (AttributeError, UnicodeDecodeError):
            continue
        result[name] = value
    return result


def _parse_tools(raw_tools: Any) -> tuple[ToolDefinition, ...]:
    if not isinstance(raw_tools, list):
        raise GatewayRequestError("tools must be an array")
    result: list[ToolDefinition] = []
    for raw_tool in raw_tools:
        if not isinstance(raw_tool, dict) or raw_tool.get("type") != "function":
            raise GatewayRequestError("only function tools are supported")
        function = raw_tool.get("function")
        if not isinstance(function, dict):
            raise GatewayRequestError("tool.function must be an object")
        name = function.get("name")
        description = function.get("description", "")
        parameters = function.get("parameters", {})
        if not isinstance(name, str) or not name:
            raise GatewayRequestError("tool function name must be a non-empty string")
        if not isinstance(description, str) or not isinstance(parameters, dict):
            raise GatewayRequestError("tool function schema is invalid")
        result.append(ToolDefinition(name=name, description=description, input_schema=parameters))
    return tuple(result)


def _parse_messages(raw_messages: Sequence[Any]) -> tuple[ConversationItem, ...]:
    result: list[ConversationItem] = []
    tool_names: dict[str, str] = {}
    for raw_message in raw_messages:
        if not isinstance(raw_message, dict):
            raise GatewayRequestError("each message must be an object")
        role = raw_message.get("role")
        if role == "tool":
            call_id = raw_message.get("tool_call_id")
            content = raw_message.get("content")
            if not isinstance(call_id, str) or not call_id:
                raise GatewayRequestError("tool message requires tool_call_id")
            if call_id not in tool_names:
                raise GatewayRequestError("tool message references an unknown tool call")
            if not isinstance(content, str):
                raise GatewayRequestError("tool message content must be a string")
            ok, observation_content, error = _decode_observation(content)
            result.append(
                ToolObservation(
                    tool_call_id=call_id,
                    name=tool_names[call_id],
                    ok=ok,
                    content=observation_content,
                    error=error,
                )
            )
            continue
        if role not in {"system", "user", "assistant"}:
            raise GatewayRequestError("message role is not supported")
        content = raw_message.get("content")
        if content is not None and not isinstance(content, str):
            raise GatewayRequestError("message content must be a string or null")
        calls = _parse_tool_calls(raw_message.get("tool_calls", []))
        if role != "assistant" and calls:
            raise GatewayRequestError("only assistant messages may contain tool calls")
        for call in calls:
            if call.tool_call_id in tool_names:
                raise GatewayRequestError("tool call IDs must be unique")
            tool_names[call.tool_call_id] = call.name
        try:
            result.append(Message(role=role, content=content, tool_calls=calls))
        except ValueError as exc:
            raise GatewayRequestError("message content is invalid") from exc
    return tuple(result)


def _parse_tool_calls(raw_calls: Any) -> tuple[ToolCall, ...]:
    if not isinstance(raw_calls, list):
        raise GatewayRequestError("message tool_calls must be an array")
    calls: list[ToolCall] = []
    for raw_call in raw_calls:
        if not isinstance(raw_call, dict) or raw_call.get("type") != "function":
            raise GatewayRequestError("only function tool calls are supported")
        call_id = raw_call.get("id")
        function = raw_call.get("function")
        if not isinstance(call_id, str) or not call_id or not isinstance(function, dict):
            raise GatewayRequestError("tool call is invalid")
        name = function.get("name")
        raw_arguments = function.get("arguments")
        if not isinstance(name, str) or not name or not isinstance(raw_arguments, str):
            raise GatewayRequestError("tool call function is invalid")
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise GatewayRequestError("tool call arguments must be valid JSON") from exc
        if not isinstance(arguments, dict):
            raise GatewayRequestError("tool call arguments must be a JSON object")
        calls.append(ToolCall(tool_call_id=call_id, name=name, arguments=arguments))
    return tuple(calls)


def _decode_observation(content: str) -> tuple[bool, str, str | None]:
    try:
        decoded = json.loads(content)
    except json.JSONDecodeError:
        return True, content, None
    if not isinstance(decoded, dict) or decoded.get("ok") is not False:
        return True, content, None
    error = decoded.get("error")
    observation_content = decoded.get("content", "")
    if not isinstance(error, str) or not error or not isinstance(observation_content, str):
        return True, content, None
    return False, observation_content, error


def _provider_status(kind: ProviderErrorKind) -> int:
    if kind == ProviderErrorKind.AUTH:
        return 502
    if kind == ProviderErrorKind.RATE_LIMIT:
        return 503
    if kind == ProviderErrorKind.INVALID_REQUEST:
        return 502
    if kind == ProviderErrorKind.RETRYABLE:
        return 503
    return 502
