"""Experimental ChatGPT/Codex OAuth and Responses transport.

This module owns an independent OAuth grant for this application.  It never
reads credentials written by Codex CLI, OpenCode, Pi, or another harness.
ChatGPT/Codex is a distinct protocol and credential audience; these tokens must
not be attached to a generic or user-configurable base URL.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import os
import secrets
import stat
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from rivumi.contracts import (
    ConversationItem,
    ModelCapabilities,
    ModelProtocol,
    ModelTurn,
    ToolCall,
    ToolDefinition,
    ToolObservation,
    Usage,
)
from rivumi.models import ProviderError, ProviderErrorKind

AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
REDIRECT_URI = "http://localhost:1455/auth/callback"

# OAuth client identifiers are public identifiers, not client secrets.  This is
# the public Codex client used by the pinned OpenCode/Pi implementations.  Keep
# the adapter experimental and revalidate upstream authorization before release.
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
JWT_AUTH_CLAIM = "https://api.openai.com/auth"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode_jwt_payload(token: str) -> Mapping[str, Any]:
    """Decode unverified claims only to obtain the account routing identifier."""

    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("OAuth access token is not a JWT")
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("OAuth access token contains invalid claims") from exc
    if not isinstance(value, dict):
        raise ValueError("OAuth access token claims must be an object")
    return value


def _account_id(access_token: str) -> str:
    claims = _decode_jwt_payload(access_token)
    auth = claims.get(JWT_AUTH_CLAIM)
    account_id = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    if not isinstance(account_id, str) or not account_id:
        raise ValueError("OAuth access token does not contain a ChatGPT account ID")
    return account_id


@dataclass(frozen=True, repr=False)
class CodexCredentials:
    """Provider-scoped secrets; repr deliberately reveals no credential data."""

    access_token: str
    refresh_token: str
    expires_at: float
    account_id: str

    def __post_init__(self) -> None:
        if not self.access_token or not self.refresh_token or not self.account_id:
            raise ValueError("Codex OAuth credentials are incomplete")
        if self.expires_at <= 0:
            raise ValueError("Codex OAuth expiry must be positive")

    def __repr__(self) -> str:
        return "CodexCredentials(access_token=<redacted>, refresh_token=<redacted>)"

    def to_json(self) -> dict[str, str | float]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "account_id": self.account_id,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> CodexCredentials:
        try:
            return cls(
                access_token=str(value["access_token"]),
                refresh_token=str(value["refresh_token"]),
                expires_at=float(value["expires_at"]),
                account_id=str(value["account_id"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Codex credential file is invalid") from exc


class CodexCredentialStore:
    """Single-account JSON store with symlink rejection and atomic 0600 writes."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()

    def load(self) -> CodexCredentials | None:
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise PermissionError("Codex credential path must be a regular file")
        if metadata.st_mode & 0o077:
            raise PermissionError("Codex credential file permissions must be 0600")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Codex credential file could not be read") from exc
        if not isinstance(value, dict):
            raise ValueError("Codex credential file must contain an object")
        return CodexCredentials.from_json(value)

    def save(self, credentials: CodexCredentials) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError as exc:
            raise PermissionError("Codex credential directory cannot be secured") from exc
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(temporary, flags, 0o600)
        try:
            payload = json.dumps(credentials.to_json(), separators=(",", ":")).encode()
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()


@dataclass(frozen=True)
class CodexAuthorization:
    url: str
    verifier: str
    state: str
    redirect_uri: str = REDIRECT_URI


class CodexOAuthClient:
    """PKCE code exchange/refresh client without browser or callback side effects."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self._http = client or httpx.AsyncClient(timeout=30.0)

    def begin_login(self, *, originator: str = "rivumi") -> CodexAuthorization:
        verifier = secrets.token_urlsafe(64)
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        state = secrets.token_urlsafe(24)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": CODEX_CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "scope": "openid profile email offline_access",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "id_token_add_organizations": "true",
                "codex_cli_simplified_flow": "true",
                "originator": originator,
            }
        )
        return CodexAuthorization(
            url=f"{AUTHORIZE_URL}?{query}", verifier=verifier, state=state
        )

    async def exchange_code(
        self,
        *,
        code: str,
        verifier: str,
        redirect_uri: str = REDIRECT_URI,
    ) -> CodexCredentials:
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "client_id": CODEX_CLIENT_ID,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            }
        )

    async def refresh(self, refresh_token: str) -> CodexCredentials:
        return await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CODEX_CLIENT_ID,
            }
        )

    async def _token_request(self, form: Mapping[str, str]) -> CodexCredentials:
        try:
            response = await self._http.post(TOKEN_URL, data=form)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderError(
                "Codex OAuth transport failed",
                kind=ProviderErrorKind.RETRYABLE,
                provider_name="openai-codex",
            ) from exc
        if response.is_error:
            kind = (
                ProviderErrorKind.RETRYABLE
                if response.status_code >= 500
                else ProviderErrorKind.AUTH
            )
            raise ProviderError(
                f"Codex OAuth request failed ({response.status_code})",
                kind=kind,
                provider_name="openai-codex",
                status_code=response.status_code,
            )
        try:
            value = response.json()
            access = value["access_token"]
            refresh = value["refresh_token"]
            expires_in = float(value["expires_in"])
            if not isinstance(access, str) or not isinstance(refresh, str):
                raise TypeError
            account = _account_id(access)
        except (ValueError, TypeError, KeyError) as exc:
            raise ProviderError(
                "Codex OAuth response is invalid",
                kind=ProviderErrorKind.AUTH,
                provider_name="openai-codex",
            ) from exc
        return CodexCredentials(
            access_token=access,
            refresh_token=refresh,
            expires_at=time.time() + expires_in,
            account_id=account,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()


class CodexCredentialManager:
    """Single-flight refresh manager which persists rotated refresh tokens."""

    def __init__(
        self,
        store: CodexCredentialStore,
        oauth: CodexOAuthClient,
        *,
        refresh_skew_seconds: float = 60.0,
    ) -> None:
        self.store = store
        self.oauth = oauth
        self.refresh_skew_seconds = refresh_skew_seconds
        self._refresh_lock = asyncio.Lock()

    async def credentials(self, *, force_refresh: bool = False) -> CodexCredentials:
        current = self.store.load()
        if current is None:
            raise ProviderError(
                "Codex OAuth login is required",
                kind=ProviderErrorKind.AUTH,
                provider_name="openai-codex",
            )
        if not force_refresh and current.expires_at > time.time() + self.refresh_skew_seconds:
            return current
        stale_access_token = current.access_token
        async with self._refresh_lock:
            current = self.store.load()
            if current is None:
                raise ProviderError(
                    "Codex OAuth login is required",
                    kind=ProviderErrorKind.AUTH,
                    provider_name="openai-codex",
                )
            if not force_refresh and current.expires_at > time.time() + self.refresh_skew_seconds:
                return current
            # Another waiter may already have replaced the rejected/expired
            # access token while this caller waited for the process lock.
            if force_refresh and current.access_token != stale_access_token:
                return current
            updated = await self.oauth.refresh(current.refresh_token)
            self.store.save(updated)
            return updated

    async def aclose(self) -> None:
        await self.oauth.aclose()


def _tool_output(item: ToolObservation) -> str:
    if item.ok:
        return item.content
    return json.dumps(
        {"ok": False, "content": item.content, "error": item.error},
        ensure_ascii=False,
    )


def _codex_input(messages: Sequence[ConversationItem]) -> tuple[str | None, list[dict[str, Any]]]:
    instructions: list[str] = []
    result: list[dict[str, Any]] = []
    for item in messages:
        if isinstance(item, ToolObservation):
            result.append(
                {
                    "type": "function_call_output",
                    "call_id": item.tool_call_id.split("|", 1)[0],
                    "output": _tool_output(item),
                }
            )
        elif item.role == "system":
            instructions.append(item.content or "")
        elif item.role == "user":
            result.append(
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": item.content or ""}],
                }
            )
        else:
            if item.content:
                result.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": item.content}],
                    }
                )
            for call in item.tool_calls:
                native: dict[str, Any] = {
                    "type": "function_call",
                    "call_id": call.tool_call_id.split("|", 1)[0],
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                }
                item_id = call.provider_metadata.get("codex_item_id")
                if isinstance(item_id, str) and item_id:
                    native["id"] = item_id
                result.append(native)
    return "\n\n".join(instructions) or None, result


def _codex_tools(tools: Sequence[ToolDefinition]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
            "strict": False,
        }
        for tool in tools
    ]


def _parse_tool(item: Mapping[str, Any]) -> ToolCall:
    arguments = item.get("arguments", "{}")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "Codex returned malformed tool arguments",
                kind=ProviderErrorKind.PROVIDER,
                provider_name="openai-codex",
            ) from exc
    if not isinstance(arguments, dict):
        raise ProviderError(
            "Codex returned non-object tool arguments",
            kind=ProviderErrorKind.PROVIDER,
            provider_name="openai-codex",
        )
    call_id = item.get("call_id")
    name = item.get("name")
    if not isinstance(call_id, str) or not isinstance(name, str):
        raise ProviderError(
            "Codex returned an invalid tool call",
            kind=ProviderErrorKind.PROVIDER,
            provider_name="openai-codex",
        )
    item_id = item.get("id")
    metadata = {"codex_item_id": item_id} if isinstance(item_id, str) else {}
    return ToolCall(
        tool_call_id=call_id,
        name=name,
        arguments=arguments,
        provider_metadata=metadata,
    )


def _turn_from_events(events: Sequence[Mapping[str, Any]]) -> ModelTurn:
    text_parts: list[str] = []
    tool_items: dict[str, Mapping[str, Any]] = {}
    final_response: Mapping[str, Any] | None = None
    for event in events:
        event_type = event.get("type")
        if event_type == "response.output_text.delta" and isinstance(event.get("delta"), str):
            text_parts.append(event["delta"])
        elif event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                key = str(item.get("id") or item.get("call_id"))
                tool_items[key] = item
        elif event_type in {"response.completed", "response.done", "response.incomplete"}:
            response = event.get("response")
            if isinstance(response, dict):
                final_response = response
        elif event_type in {"error", "response.failed"}:
            raise ProviderError(
                "Codex response stream reported a failure",
                kind=ProviderErrorKind.PROVIDER,
                provider_name="openai-codex",
            )

    if final_response is not None:
        for item in final_response.get("output", ()):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call":
                key = str(item.get("id") or item.get("call_id"))
                tool_items[key] = item
            elif item.get("type") == "message" and not text_parts:
                for content in item.get("content", ()):
                    if isinstance(content, dict) and content.get("type") == "output_text":
                        text = content.get("text")
                        if isinstance(text, str):
                            text_parts.append(text)

    calls = tuple(_parse_tool(item) for item in tool_items.values())
    usage_value = final_response.get("usage", {}) if final_response else {}
    usage = usage_value if isinstance(usage_value, dict) else {}
    input_details = usage.get("input_tokens_details", {})
    cached = input_details.get("cached_tokens", 0) if isinstance(input_details, dict) else 0
    status = final_response.get("status") if final_response else None
    finish_reason = "tool_calls" if calls else ("length" if status == "incomplete" else "stop")
    content = "".join(text_parts) or None
    if content is None and not calls:
        raise ProviderError(
            "Codex response contained no assistant output",
            kind=ProviderErrorKind.PROVIDER,
            provider_name="openai-codex",
        )
    return ModelTurn(
        content=content,
        tool_calls=calls,
        usage=Usage(
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cached_input_tokens=int(cached or 0),
            provider_total_tokens=usage.get("total_tokens"),
        ),
        finish_reason=finish_reason,
    )


class OpenAICodexResponsesModel:
    """ChatGPT subscription adapter for the Codex Responses SSE protocol."""

    provider_name = "openai-codex"
    protocol = ModelProtocol.OPENAI_CODEX_RESPONSES
    capabilities = ModelCapabilities(
        tool_calling=True,
        streaming=True,
        structured_output=False,
    )

    def __init__(
        self,
        *,
        model: str,
        credentials: CodexCredentialManager,
        client: httpx.AsyncClient | None = None,
        experimental: bool = False,
        originator: str = "rivumi",
    ) -> None:
        if not experimental:
            raise ValueError(
                "ChatGPT/Codex OAuth is experimental; pass experimental=True after "
                "reviewing the current service authorization"
            )
        self.model_id = model
        self.credentials = credentials
        self.originator = originator
        self._owns_client = client is None
        self._http = client or httpx.AsyncClient(timeout=120.0)

    async def complete(
        self,
        messages: Sequence[ConversationItem],
        tools: Sequence[ToolDefinition] = (),
    ) -> ModelTurn:
        instructions, native_input = _codex_input(messages)
        payload: dict[str, Any] = {
            "model": self.model_id,
            "store": False,
            "stream": True,
            "instructions": instructions,
            "input": native_input,
            "text": {"verbosity": "low"},
            "include": ["reasoning.encrypted_content"],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }
        if tools:
            payload["tools"] = _codex_tools(tools)

        credential = await self.credentials.credentials()
        response = await self._request(payload, credential)
        if response.status_code == 401:
            await response.aclose()
            credential = await self.credentials.credentials(force_refresh=True)
            response = await self._request(payload, credential)
        try:
            if response.is_error:
                kind = (
                    ProviderErrorKind.AUTH
                    if response.status_code in {401, 403}
                    else ProviderErrorKind.RATE_LIMIT
                    if response.status_code == 429
                    else ProviderErrorKind.RETRYABLE
                    if response.status_code >= 500
                    else ProviderErrorKind.PROVIDER
                )
                raise ProviderError(
                    f"Codex request failed ({response.status_code})",
                    kind=kind,
                    provider_name=self.provider_name,
                    status_code=response.status_code,
                )
            events: list[Mapping[str, Any]] = []
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise ProviderError(
                        "Codex returned malformed SSE data",
                        kind=ProviderErrorKind.PROVIDER,
                        provider_name=self.provider_name,
                    ) from exc
                if isinstance(event, dict):
                    events.append(event)
            return _turn_from_events(events)
        finally:
            await response.aclose()

    async def _request(
        self, payload: Mapping[str, Any], credential: CodexCredentials
    ) -> httpx.Response:
        request = self._http.build_request(
            "POST",
            CODEX_RESPONSES_URL,
            headers={
                "authorization": f"Bearer {credential.access_token}",
                "chatgpt-account-id": credential.account_id,
                "originator": self.originator,
                "openai-beta": "responses=experimental",
                "accept": "text/event-stream",
                "content-type": "application/json",
            },
            json=payload,
        )
        try:
            return await self._http.send(request, stream=True)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderError(
                "Codex transport failed",
                kind=ProviderErrorKind.RETRYABLE,
                provider_name=self.provider_name,
            ) from exc

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()
        await self.credentials.aclose()
