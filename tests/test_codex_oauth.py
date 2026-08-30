from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from looplane.codex_oauth import (
    CODEX_CLIENT_ID,
    CODEX_RESPONSES_URL,
    TOKEN_URL,
    CodexCredentialManager,
    CodexCredentials,
    CodexCredentialStore,
    CodexOAuthClient,
    OpenAICodexResponsesModel,
)
from looplane.contracts import Message, ToolCall, ToolDefinition, ToolObservation
from looplane.models import ProviderError, ProviderErrorKind


def jwt(account_id: str = "account-test") -> str:
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def credentials(*, expired: bool = False, suffix: str = "old") -> CodexCredentials:
    return CodexCredentials(
        access_token=f"{jwt()}{suffix}",
        refresh_token=f"refresh-{suffix}",
        expires_at=time.time() + (-60 if expired else 3600),
        account_id="account-test",
    )


def test_pkce_authorization_has_expected_fixed_audience() -> None:
    oauth = CodexOAuthClient(client=httpx.AsyncClient())

    authorization = oauth.begin_login(originator="rockcode-test")
    query = parse_qs(urlsplit(authorization.url).query)

    assert urlsplit(authorization.url).netloc == "auth.openai.com"
    assert query["client_id"] == [CODEX_CLIENT_ID]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [authorization.state]
    assert query["originator"] == ["rockcode-test"]
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(authorization.verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert query["code_challenge"] == [expected]


@pytest.mark.asyncio
async def test_code_exchange_uses_pkce_and_extracts_account_claim() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": jwt("account-from-token"),
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    oauth = CodexOAuthClient(client=http)
    result = await oauth.exchange_code(code="secret-code", verifier="secret-verifier")

    assert requests[0].url == TOKEN_URL
    assert b"grant_type=authorization_code" in requests[0].content
    assert b"code_verifier=secret-verifier" in requests[0].content
    assert result.account_id == "account-from-token"
    assert "secret" not in repr(result)
    await http.aclose()


@pytest.mark.asyncio
async def test_oauth_errors_do_not_echo_response_or_submitted_secrets() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="server leaked token-value", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    oauth = CodexOAuthClient(client=http)

    with pytest.raises(ProviderError) as caught:
        await oauth.refresh("local-refresh-secret")

    assert caught.value.kind == ProviderErrorKind.AUTH
    assert "token-value" not in str(caught.value)
    assert "local-refresh-secret" not in str(caught.value)
    await http.aclose()


def test_credential_store_is_atomic_0600_and_rejects_loose_permissions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "codex.json"
    store = CodexCredentialStore(path)
    expected = credentials()

    store.save(expected)

    assert store.load() == expected
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert not list(path.parent.glob("*.tmp"))
    os.chmod(path, 0o644)
    with pytest.raises(PermissionError, match="0600"):
        store.load()


def test_credential_store_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}")
    link = tmp_path / "codex.json"
    link.symlink_to(target)

    with pytest.raises(PermissionError, match="regular file"):
        CodexCredentialStore(link).load()


class FakeOAuth:
    def __init__(self) -> None:
        self.calls = 0

    async def refresh(self, refresh_token: str) -> CodexCredentials:
        assert refresh_token == "refresh-old"
        self.calls += 1
        await asyncio.sleep(0)
        return credentials(suffix="new")


@pytest.mark.asyncio
async def test_expired_credential_refresh_is_single_flight_and_atomic(tmp_path: Path) -> None:
    store = CodexCredentialStore(tmp_path / "codex.json")
    store.save(credentials(expired=True))
    oauth = FakeOAuth()
    manager = CodexCredentialManager(store, oauth)  # type: ignore[arg-type]

    first, second = await asyncio.gather(manager.credentials(), manager.credentials())

    assert oauth.calls == 1
    assert first.refresh_token == second.refresh_token == "refresh-new"
    assert store.load() == first


@pytest.mark.asyncio
async def test_forced_refresh_is_single_flight_for_same_rejected_token(tmp_path: Path) -> None:
    store = CodexCredentialStore(tmp_path / "codex.json")
    store.save(credentials())
    oauth = FakeOAuth()
    manager = CodexCredentialManager(store, oauth)  # type: ignore[arg-type]

    first, second = await asyncio.gather(
        manager.credentials(force_refresh=True),
        manager.credentials(force_refresh=True),
    )

    assert oauth.calls == 1
    assert first.access_token == second.access_token


class StaticCredentials:
    def __init__(self) -> None:
        self.forced = 0

    async def credentials(self, *, force_refresh: bool = False) -> CodexCredentials:
        if force_refresh:
            self.forced += 1
        return credentials(suffix="new" if force_refresh else "old")


def sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()


@pytest.mark.asyncio
async def test_codex_responses_transport_roundtrips_tools_usage_and_fixed_endpoint() -> None:
    requests: list[httpx.Request] = []
    events = [
        {"type": "response.output_text.delta", "delta": "Inspecting."},
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "read_file",
                "arguments": '{"path":"README.md"}',
            },
        },
        {
            "type": "response.done",
            "response": {
                "status": "completed",
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 5,
                    "total_tokens": 17,
                    "input_tokens_details": {"cached_tokens": 3},
                },
            },
        },
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(events),
            request=request,
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICodexResponsesModel(
        model="gpt-test",
        credentials=StaticCredentials(),  # type: ignore[arg-type]
        client=http,
        experimental=True,
    )
    tool = ToolDefinition(
        name="read_file",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )

    turn = await provider.complete(
        [Message(role="system", content="Be careful"), Message(role="user", content="Read")],
        [tool],
    )

    assert requests[0].url == CODEX_RESPONSES_URL
    assert requests[0].headers["authorization"].startswith("Bearer ")
    assert requests[0].headers["chatgpt-account-id"] == "account-test"
    body = json.loads(requests[0].content)
    assert body["instructions"] == "Be careful"
    assert body["stream"] is True
    assert body["store"] is False
    assert body["tools"][0]["name"] == "read_file"
    assert turn.content == "Inspecting."
    assert turn.tool_calls[0] == ToolCall(
        tool_call_id="call_1",
        name="read_file",
        arguments={"path": "README.md"},
        provider_metadata={"codex_item_id": "fc_1"},
    )
    assert turn.usage.input_tokens == 12
    assert turn.usage.cached_input_tokens == 3
    assert turn.usage.total_tokens == 17
    await http.aclose()


@pytest.mark.asyncio
async def test_codex_transport_replays_native_tool_call_and_observation() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        final = {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Done."}],
                    }
                ],
            },
        }
        return httpx.Response(200, content=sse([final]), request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICodexResponsesModel(
        model="gpt-test",
        credentials=StaticCredentials(),  # type: ignore[arg-type]
        client=http,
        experimental=True,
    )
    prior_call = ToolCall(
        tool_call_id="call_1",
        name="read_file",
        arguments={"path": "README.md"},
        provider_metadata={"codex_item_id": "fc_1"},
    )
    await provider.complete(
        [
            Message(role="assistant", tool_calls=(prior_call,)),
            ToolObservation(tool_call_id="call_1", name="read_file", ok=True, content="contents"),
        ]
    )

    native_input = json.loads(requests[0].content)["input"]
    assert native_input[0]["id"] == "fc_1"
    assert native_input[0]["call_id"] == "call_1"
    assert native_input[1] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "contents",
    }
    await http.aclose()


@pytest.mark.asyncio
async def test_unauthorized_response_forces_one_refresh_and_retry() -> None:
    attempts = 0
    credential_manager = StaticCredentials()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(401, request=request)
        event = {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Recovered."}],
                    }
                ],
            },
        }
        return httpx.Response(200, content=sse([event]), request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICodexResponsesModel(
        model="gpt-test",
        credentials=credential_manager,  # type: ignore[arg-type]
        client=http,
        experimental=True,
    )

    turn = await provider.complete([Message(role="user", content="Hello")])

    assert turn.content == "Recovered."
    assert attempts == 2
    assert credential_manager.forced == 1
    await http.aclose()


def test_codex_provider_is_fail_closed_until_experimental_opt_in() -> None:
    with pytest.raises(ValueError, match="experimental"):
        OpenAICodexResponsesModel(
            model="gpt-test",
            credentials=StaticCredentials(),  # type: ignore[arg-type]
        )
