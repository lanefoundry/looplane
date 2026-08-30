"""Tests for the disk-cached looplane-agent model catalog."""

from __future__ import annotations

import httpx
import pytest

from looplane import model_catalog


@pytest.fixture()
def _cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture()
def _isolated_credentials(tmp_path, monkeypatch):
    """Remove env credentials and point the credential store at tmp.

    The dev machine may export real provider keys; these tests assert behavior
    when no credential resolves at all.
    """

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _models_payload(*ids: str) -> dict:
    return {"object": "list", "data": [{"id": model_id} for model_id in ids]}


@pytest.mark.usefixtures("_cache_dir")
def test_snapshot_roundtrips_through_store(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-1")

    assert model_catalog.snapshot("openrouter") is None

    model_catalog.store_models("openrouter", ("m/a", "m/b"))

    snap = model_catalog.snapshot("openrouter")
    assert snap is not None
    assert snap.models == ("m/a", "m/b")
    assert model_catalog.is_stale(snap) is False


@pytest.mark.usefixtures("_cache_dir")
def test_credential_change_invalidates_cache(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-1")
    model_catalog.store_models("openrouter", ("m/a",))

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-2")

    assert model_catalog.snapshot("openrouter") is None


@pytest.mark.usefixtures("_cache_dir", "_isolated_credentials")
def test_missing_credential_has_no_snapshot() -> None:
    assert model_catalog.snapshot("openrouter") is None
    model_catalog.store_models("openrouter", ("m/a",))  # no-op without credentials
    assert model_catalog.snapshot("openrouter") is None


@pytest.mark.usefixtures("_cache_dir")
def test_is_stale_without_snapshot_and_after_ttl(monkeypatch) -> None:
    assert model_catalog.is_stale(None) is True

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-1")
    model_catalog.store_models("openrouter", ("m/a",))
    snap = model_catalog.snapshot("openrouter")
    assert model_catalog.is_stale(snap) is False

    monkeypatch.setattr(model_catalog, "CATALOG_TTL_SECONDS", -1.0)
    assert model_catalog.is_stale(snap) is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("_cache_dir")
async def test_refresh_fetches_lists_and_stores(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-1")
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=_models_payload("m/a", "m/b"), request=request)

    models = await model_catalog.refresh("openrouter", client=_client(handler))

    assert models == ("m/a", "m/b")
    assert len(calls) == 1
    snap = model_catalog.snapshot("openrouter")
    assert snap is not None
    assert snap.models == ("m/a", "m/b")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_cache_dir")
async def test_refresh_failure_returns_empty_and_stores_nothing(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-1")

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    assert await model_catalog.refresh("openrouter", client=_client(handler)) == ()
    assert model_catalog.snapshot("openrouter") is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("_cache_dir")
async def test_refresh_auth_failure_returns_empty(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "bad")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "nope"}}, request=request)

    assert await model_catalog.refresh("openrouter", client=_client(handler)) == ()
    assert model_catalog.snapshot("openrouter") is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("_cache_dir", "_isolated_credentials")
async def test_refresh_without_credential_returns_empty() -> None:
    assert await model_catalog.refresh("openrouter") == ()
    assert model_catalog.snapshot("openrouter") is None


def test_default_model_prefers_known_families_over_first_entry() -> None:
    models = ("aardvark/experimental-1", "anthropic/claude-sonnet-4", "zzz/thing")

    assert model_catalog.default_model(models) == "anthropic/claude-sonnet-4"


def test_default_model_skips_free_variants() -> None:
    models = ("m/a:free", "m/b", "m/c:free")

    assert model_catalog.default_model(models) == "m/b"


def test_default_model_skips_role_aliases() -> None:
    models = ("@cheap", "m/a:free", "anthropic/claude-sonnet-4")

    assert model_catalog.default_model(models) == "anthropic/claude-sonnet-4"


def test_default_model_all_free_returns_none() -> None:
    assert model_catalog.default_model(("m/a:free", ":free")) is None


def test_default_model_empty() -> None:
    assert model_catalog.default_model(()) is None


def test_default_model_prefers_provider_brand_over_claude() -> None:
    models = ("moonshot/kimi-k2", "anthropic/claude-sonnet-4", "qwen/qwen3")

    assert model_catalog.default_model(models, "moonshotai") == "moonshot/kimi-k2"
    # zai serves glm, which is absent here -> generic list applies (claude first).
    assert model_catalog.default_model(models, "zai") == "anthropic/claude-sonnet-4"
    assert model_catalog.default_model(models, "openrouter") == "anthropic/claude-sonnet-4"


def test_default_model_xai_picks_grok() -> None:
    models = ("grok-4", "claude-sonnet")

    assert model_catalog.default_model(models, "xai") == "grok-4"


def test_default_model_nvidia_picks_nemotron() -> None:
    models = ("claude-sonnet", "nvidia/nemotron-3-ultra")

    # nvidia-nim does not serve claude; brand pattern wins anyway when present.
    assert model_catalog.default_model(models, "nvidia-nim") == "nvidia/nemotron-3-ultra"
