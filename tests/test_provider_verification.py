from __future__ import annotations

import httpx
import pytest

from looplane.native_credentials import NATIVE_CREDENTIAL_FIELDS
from looplane.provider_verification import (
    VerificationResult,
    fetch_models_result,
    verify_native_credential,
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _fail_if_called(request: httpx.Request) -> httpx.Response:  # pragma: no cover - guard
    pytest.fail(f"unexpected HTTP request: {request.method} {request.url}")


@pytest.mark.asyncio
async def test_verify_ollama_is_skipped_without_any_network_call() -> None:
    result = await verify_native_credential("ollama", {}, client=_client(_fail_if_called))

    assert result == VerificationResult(
        ok=True, message="Local Ollama does not require verification.", skipped=True
    )


@pytest.mark.asyncio
async def test_verify_openai_compatible_family_success_lists_models() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-test"
        return httpx.Response(
            200,
            json={"data": [{"id": "gpt-5.6"}, {"id": "gpt-5.6-mini"}]},
            request=request,
        )

    result = await verify_native_credential("groq", {"api_key": "sk-test"}, client=_client(handler))

    assert result.ok is True
    assert result.models == ("gpt-5.6", "gpt-5.6-mini")


@pytest.mark.asyncio
async def test_verify_openai_compatible_family_401_is_reported_as_auth_failure() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid api key"}, request=request)

    result = await verify_native_credential("groq", {"api_key": "bad-key"}, client=_client(handler))

    assert result.ok is False
    assert "401" in result.message


@pytest.mark.asyncio
async def test_verify_openai_compatible_family_missing_models_endpoint_degrades_gracefully() -> (
    None
):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"}, request=request)

    result = await verify_native_credential(
        "opencode-zen", {"api_key": "sk-test"}, client=_client(handler)
    )

    assert result.ok is True
    assert result.models == ()


@pytest.mark.asyncio
async def test_verify_anthropic_success_lists_models_with_expected_headers() -> None:
    captured: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(
            200,
            json={"data": [{"id": "claude-sonnet-5"}, {"id": "claude-opus-5"}]},
            request=request,
        )

    result = await verify_native_credential(
        "anthropic", {"api_key": "sk-ant-test"}, client=_client(handler)
    )

    assert result.ok is True
    assert result.models == ("claude-sonnet-5", "claude-opus-5")
    assert captured["x-api-key"] == "sk-ant-test"
    assert captured["anthropic-version"] == "2023-06-01"


@pytest.mark.asyncio
async def test_verify_anthropic_auth_failure() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"message": "invalid x-api-key"}}, request=request
        )

    result = await verify_native_credential(
        "anthropic", {"api_key": "bad"}, client=_client(handler)
    )

    assert result.ok is False
    assert "401" in result.message


@pytest.mark.asyncio
async def test_verify_gemini_strips_models_prefix() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "gm-test"
        return httpx.Response(
            200,
            json={
                "models": [{"name": "models/gemini-2.5-pro"}, {"name": "models/gemini-2.5-flash"}]
            },
            request=request,
        )

    result = await verify_native_credential(
        "gemini", {"api_key": "gm-test"}, client=_client(handler)
    )

    assert result.ok is True
    assert result.models == ("gemini-2.5-pro", "gemini-2.5-flash")
    assert "models/" not in result.models[0]


@pytest.mark.asyncio
async def test_verify_gemini_auth_failure() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "permission denied"}, request=request)

    result = await verify_native_credential("gemini", {"api_key": "bad"}, client=_client(handler))

    assert result.ok is False
    assert "403" in result.message


@pytest.mark.asyncio
async def test_verify_workers_ai_requires_both_fields_present() -> None:
    result = await verify_native_credential(
        "workers-ai", {"account_id": "acct-1"}, client=_client(_fail_if_called)
    )

    assert result.ok is False
    assert "account_id" in result.message or "api_token" in result.message


@pytest.mark.asyncio
async def test_verify_workers_ai_success() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer cf-token"
        assert "/accounts/acct-1/ai/models/search" in str(request.url)
        return httpx.Response(
            200,
            json={
                "success": True,
                "errors": [],
                "messages": [],
                "result": [{"name": "@cf/meta/llama-3.1-8b-instruct"}],
            },
            request=request,
        )

    result = await verify_native_credential(
        "workers-ai",
        {"account_id": "acct-1", "api_token": "cf-token"},
        client=_client(handler),
    )

    assert result.ok is True
    assert result.models == ("@cf/meta/llama-3.1-8b-instruct",)


@pytest.mark.asyncio
async def test_verify_workers_ai_success_false_is_a_credential_failure() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": False,
                "errors": [{"code": 10000, "message": "Authentication error"}],
                "messages": [],
                "result": None,
            },
            request=request,
        )

    result = await verify_native_credential(
        "workers-ai",
        {"account_id": "acct-1", "api_token": "wrong-token"},
        client=_client(handler),
    )

    assert result.ok is False
    assert "Authentication error" in result.message


@pytest.mark.asyncio
async def test_verify_workers_ai_result_shape_mismatch_degrades_to_empty_models() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "errors": [],
                "messages": [],
                "result": [{"unexpected_key": "no name field here"}],
            },
            request=request,
        )

    result = await verify_native_credential(
        "workers-ai",
        {"account_id": "acct-1", "api_token": "cf-token"},
        client=_client(handler),
    )

    assert result.ok is True
    assert result.models == ()


@pytest.mark.asyncio
async def test_fetch_models_result_reports_models_and_reasons() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "model-a"}]}, request=request)

    result = await fetch_models_result("deepseek", {"api_key": "sk-test"}, client=_client(handler))

    assert result.ok is True
    assert result.models == ("model-a",)


@pytest.mark.asyncio
async def test_fetch_models_result_keeps_provider_failure_message() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"}, request=request)

    result = await fetch_models_result("deepseek", {"api_key": "bad"}, client=_client(handler))

    assert result.ok is False
    assert "401" in result.message


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", sorted(NATIVE_CREDENTIAL_FIELDS))
async def test_fetch_models_result_never_raises_on_network_failure(provider: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    fields = {field: "dummy-value" for field in NATIVE_CREDENTIAL_FIELDS[provider]}

    result = await fetch_models_result(provider, fields, client=_client(handler))

    assert result.ok is False
    assert result.message


@pytest.mark.asyncio
async def test_fetch_models_result_wraps_unsupported_provider() -> None:
    result = await fetch_models_result("totally-unknown-provider", {})

    assert result.ok is False
    assert "unsupported provider" in result.message
