"""Best-effort connection checks and model discovery for ``rivumi-agent`` providers.

Kept separate from ``native_credentials.py`` (pure local credential storage, no network
I/O) and from ``models.py`` (the task-execution model contract). This module answers one
question only: given these credential field values, can we reach the provider, and what
models does it expose? It is used by both the CLI (``rivumi auth set-key``/``auth list``)
and the TUI onboarding wizard immediately after a credential is entered, so a wrong key
surfaces before the user ever submits a task instead of failing mid-run.

Every function here is designed to never raise on a provider/network failure -- callers
get a ``VerificationResult`` (or an empty model tuple) back and decide what the UI does
with it. The three endpoints below were not reachable with a real credential while writing
this module (``zai``/``opencode-zen``'s ``/models`` support, and the exact key name inside
Cloudflare's ``result[]`` model objects); see the inline notes at each call site for the
degrade path and what to check first if a real account surfaces a mismatch.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from rivumi.models import ProviderErrorKind, _error_kind
from rivumi.provider_catalog import (
    ANTHROPIC_BASE_URL,
    GEMINI_BASE_URL,
    OPENAI_COMPATIBLE_BASE_URLS,
    WORKERS_AI_BASE_URL,
    provider_base_url,
)

# Status codes meaning "the endpoint we probed does not exist here", not "the credential is
# invalid". Provider connectivity is real; we just can't confirm the key works this way.
_DEGRADE_STATUS_CODES = frozenset({404, 405, 501})

ANTHROPIC_MODELS_LIMIT = 100


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of one best-effort connection check."""

    ok: bool
    message: str
    models: tuple[str, ...] = ()
    skipped: bool = False


async def verify_native_credential(
    provider: str,
    fields: Mapping[str, str],
    *,
    timeout: float = 10.0,
    client: httpx.AsyncClient | None = None,
) -> VerificationResult:
    """Check that ``fields`` actually authenticate against ``provider``.

    ``timeout`` defaults far shorter than the 60s used for task execution in
    ``models.py``: this runs on a UI path where a human is watching a spinner.
    ``client`` lets callers inject an ``httpx.AsyncClient`` (e.g. with a
    ``MockTransport``) for tests; it is reused as-is for the raw-HTTP providers and
    passed through as the OpenAI SDK's own transport for the OpenAI-compatible family.
    """

    if provider == "ollama":
        return VerificationResult(
            ok=True, message="Local Ollama does not require verification.", skipped=True
        )
    if provider == "anthropic":
        return await _verify_anthropic(fields, timeout=timeout, client=client)
    if provider == "gemini":
        return await _verify_gemini(fields, timeout=timeout, client=client)
    if provider == "workers-ai":
        return await _verify_workers_ai(fields, timeout=timeout, client=client)
    if provider == "openai-compatible" or provider in OPENAI_COMPATIBLE_BASE_URLS:
        return await _verify_openai_compatible(provider, fields, timeout=timeout, client=client)
    raise ValueError(f"unsupported provider for verification: {provider}")


async def fetch_models_result(
    provider: str,
    fields: Mapping[str, str],
    *,
    timeout: float = 10.0,
    client: httpx.AsyncClient | None = None,
) -> VerificationResult:
    """Model discovery that reports *why* a listing came back empty.

    Unlike a bare models-tuple return this keeps the reason for UI callers:
    ``ok=False`` carries the provider's failure message; ``ok=True`` with empty
    models means the endpoint answered but exposes no listing (degraded).
    Never raises on provider/network failures -- unexpected programming errors
    still propagate.
    """

    try:
        result = await verify_native_credential(provider, fields, timeout=timeout, client=client)
    except Exception as exc:  # noqa: BLE001 - this is the UI's fallback-to-free-input safety net
        return VerificationResult(ok=False, message=f"{provider} model listing failed: {exc}")
    return result


def _auth_failure(provider: str, status_code: int) -> VerificationResult:
    return VerificationResult(
        ok=False, message=f"{provider} rejected the credential ({status_code})."
    )


def _generic_failure(provider: str, status_code: int) -> VerificationResult:
    return VerificationResult(ok=False, message=f"{provider} request failed ({status_code}).")


def _degraded_ok(provider: str) -> VerificationResult:
    return VerificationResult(
        ok=True,
        message=f"Connected to {provider}, but it does not support listing models.",
    )


async def _verify_openai_compatible(
    provider: str,
    fields: Mapping[str, str],
    *,
    timeout: float,
    client: httpx.AsyncClient | None,
) -> VerificationResult:
    api_key = fields.get("api_key")
    if not api_key:
        return VerificationResult(ok=False, message="api_key is required")

    base_url = provider_base_url(provider)
    if provider == "openai-compatible" and base_url is None:
        base_url = os.environ.get("OPENAI_BASE_URL")

    # Retries are handled uniformly by AgentRunner._complete_model_with_retry for
    # agent traffic; verification is a one-shot call, so the SDK's built-in
    # retries are disabled to keep failures immediate and observable.
    sdk_client = AsyncOpenAI(
        api_key=api_key, base_url=base_url, timeout=timeout, http_client=client,
        max_retries=0,
    )
    try:
        page = await sdk_client.models.list()
    except (APIConnectionError, APITimeoutError) as exc:
        return VerificationResult(ok=False, message=f"{provider} connection failed: {exc}")
    except APIStatusError as exc:
        status = exc.status_code
        if status in _DEGRADE_STATUS_CODES:
            return _degraded_ok(provider)
        if _error_kind(status) is ProviderErrorKind.AUTH:
            return _auth_failure(provider, status)
        return _generic_failure(provider, status)
    finally:
        await sdk_client.close()

    models = tuple(
        dict.fromkeys(item.id for item in getattr(page, "data", ()) if getattr(item, "id", None))
    )
    return VerificationResult(ok=True, message=f"Connected to {provider}.", models=models)


async def _verify_anthropic(
    fields: Mapping[str, str], *, timeout: float, client: httpx.AsyncClient | None
) -> VerificationResult:
    api_key = fields.get("api_key")
    if not api_key:
        return VerificationResult(ok=False, message="api_key is required")

    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=timeout)
    try:
        try:
            response = await http_client.get(
                f"{ANTHROPIC_BASE_URL}/v1/models",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                params={"limit": ANTHROPIC_MODELS_LIMIT},
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            return VerificationResult(ok=False, message=f"anthropic connection failed: {exc}")
        if response.status_code in _DEGRADE_STATUS_CODES:
            return _degraded_ok("anthropic")
        if response.is_error:
            if _error_kind(response.status_code) is ProviderErrorKind.AUTH:
                return _auth_failure("anthropic", response.status_code)
            return _generic_failure("anthropic", response.status_code)
        try:
            body = response.json()
        except ValueError:
            return VerificationResult(
                ok=False, message="anthropic returned an unexpected response."
            )
        models = tuple(
            dict.fromkeys(
                item["id"]
                for item in body.get("data", ())
                if isinstance(item, dict) and item.get("id")
            )
        )
        return VerificationResult(ok=True, message="Connected to anthropic.", models=models)
    finally:
        if owns_client:
            await http_client.aclose()


async def _verify_gemini(
    fields: Mapping[str, str], *, timeout: float, client: httpx.AsyncClient | None
) -> VerificationResult:
    api_key = fields.get("api_key")
    if not api_key:
        return VerificationResult(ok=False, message="api_key is required")

    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=timeout)
    try:
        # x-goog-api-key mirrors GeminiModel.complete()'s auth (models.py); Google's own
        # docs show a `?key=` query param for this endpoint instead. If this header turns
        # out not to be accepted here, switch to `params={"key": api_key}`.
        try:
            response = await http_client.get(
                f"{GEMINI_BASE_URL}/models", headers={"x-goog-api-key": api_key}
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            return VerificationResult(ok=False, message=f"gemini connection failed: {exc}")
        if response.status_code in _DEGRADE_STATUS_CODES:
            return _degraded_ok("gemini")
        if response.is_error:
            if _error_kind(response.status_code) is ProviderErrorKind.AUTH:
                return _auth_failure("gemini", response.status_code)
            return _generic_failure("gemini", response.status_code)
        try:
            body = response.json()
        except ValueError:
            return VerificationResult(ok=False, message="gemini returned an unexpected response.")
        # Gemini's `name` field is prefixed ("models/gemini-2.5-pro"); GeminiModel.complete()
        # builds its URL from a bare model id, so the prefix must be stripped here or a
        # model chosen from this list would 404 with a doubled "models/models/" path.
        models = tuple(
            dict.fromkeys(
                str(item["name"]).removeprefix("models/")
                for item in body.get("models", ())
                if isinstance(item, dict) and item.get("name")
            )
        )
        return VerificationResult(ok=True, message="Connected to gemini.", models=models)
    finally:
        if owns_client:
            await http_client.aclose()


async def _verify_workers_ai(
    fields: Mapping[str, str], *, timeout: float, client: httpx.AsyncClient | None
) -> VerificationResult:
    account_id = fields.get("account_id")
    api_token = fields.get("api_token")
    if not account_id or not api_token:
        return VerificationResult(ok=False, message="account_id and api_token are both required")

    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=timeout)
    try:
        try:
            response = await http_client.get(
                f"{WORKERS_AI_BASE_URL}/accounts/{account_id}/ai/models/search",
                headers={"authorization": f"Bearer {api_token}"},
                params={"hide_experimental": "true", "per_page": "50"},
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            return VerificationResult(ok=False, message=f"workers-ai connection failed: {exc}")
        if response.status_code in _DEGRADE_STATUS_CODES:
            return _degraded_ok("workers-ai")
        if response.is_error:
            if _error_kind(response.status_code) is ProviderErrorKind.AUTH:
                return _auth_failure("workers-ai", response.status_code)
            return _generic_failure("workers-ai", response.status_code)
        try:
            body = response.json()
        except ValueError:
            return VerificationResult(
                ok=False, message="workers-ai returned an unexpected response."
            )
        if not isinstance(body, dict) or body.get("success") is not True:
            errors = body.get("errors") if isinstance(body, dict) else None
            detail = errors[0].get("message") if errors else "request rejected"
            return VerificationResult(
                ok=False, message=f"workers-ai rejected the credential: {detail}"
            )
        # Cloudflare's exact key name inside each result[] object was not confirmed against
        # a real account while writing this. If a live search shows model ids live under a
        # different key (e.g. nested under "properties"), fix the extraction below -- a
        # shape mismatch here degrades to an empty model list, it does not fail verification.
        models: tuple[str, ...] = ()
        result = body.get("result")
        if isinstance(result, list):
            try:
                models = tuple(
                    dict.fromkeys(
                        str(item["name"])
                        for item in result
                        if isinstance(item, dict) and item.get("name")
                    )
                )
            except (KeyError, TypeError):
                models = ()
        return VerificationResult(ok=True, message="Connected to workers-ai.", models=models)
    finally:
        if owns_client:
            await http_client.aclose()
