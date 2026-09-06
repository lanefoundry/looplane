"""Disk-cached model catalogs for ``looplane-agent`` providers.

The onboarding wizard and the in-session ``/model`` selector need the same
answer: which models does this provider expose? Hitting the provider's listing
endpoint costs 0.5-2s, so successful listings live in the shared startup disk
cache keyed by ``provider + credential fingerprint`` -- changing the stored key
or its env var invalidates the entry automatically.

Readers get a :class:`CatalogSnapshot` instantly and decide staleness
themselves (stale-while-revalidate: show stale, refresh in the background via
:func:`refresh`). Only non-empty listings are cached, so degraded providers
(connected but no listing support) keep retrying on demand instead of caching
a permanent empty answer.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import httpx

from looplane.native_credentials import NATIVE_CREDENTIAL_FIELDS, resolve_native_field
from looplane.provider_verification import VerificationResult, verify_native_credential
from looplane.startup_cache import read_entry, write_entry

CATALOG_VERSION = "model-catalog-v1"
# Model lists change rarely (releases, deprecations); a day is fresh enough for
# a picker that revalidates in the background anyway.
CATALOG_TTL_SECONDS = 24 * 3600.0

# Aggregators proxy hundreds of upstream models that churn faster; refresh more
# often so stale/removed entries don't linger a full day.
AGGREGATOR_PROVIDERS = frozenset({"openrouter", "opencode-zen", "ollama-cloud"})
AGGREGATOR_CATALOG_TTL_SECONDS = 4 * 3600.0  # 4 hours for aggregators

# Disk-persisted denylist for models that exhausted their retry budget.
DENYLIST_VERSION = "model-denylist-v1"
DENYLIST_TTL_SECONDS = 3600.0  # denied models recover after 1 hour


@dataclass(frozen=True)
class CatalogSnapshot:
    """A previously fetched listing plus when it was fetched."""

    models: tuple[str, ...]
    fetched_at: float  # epoch seconds; 0.0 for pre-timestamp entries


# Catalog order is arbitrary (usually alphabetical), so "first model" is often
# an experimental or free variant. Prefer the provider's own flagship families
# first, then well-known cross-provider families for aggregators.
_PROVIDER_BRAND_PATTERNS: dict[str, tuple[str, ...]] = {
    "anthropic": ("claude",),
    "deepseek": ("deepseek",),
    "groq": ("llama", "gpt-oss", "deepseek", "qwen", "kimi", "glm", "mistral"),
    "moonshotai": ("kimi", "moonshot"),
    "zai": ("glm", "zhipu"),
    "xai": ("grok",),
    "nvidia-nim": ("nemotron", "nvidia", "qwen", "llama", "deepseek", "mistral"),
    "ollama": ("llama", "qwen", "deepseek", "gpt-oss", "mistral"),
    # Aggregators with no own family: the generic preference list applies.
    "openrouter": (),
    "opencode-zen": (),
    "ollama-cloud": (),
    "openai-compatible": (),
}

_MODEL_PREFERENCE: tuple[str, ...] = (
    "claude-sonnet",
    "claude-opus",
    "gpt-",
    "gemini-",
    "deepseek-chat",
    "deepseek-reasoner",
    "kimi-k",
    "glm-",
    "qwen3",
    "llama-4",
    "mistral",
)


def default_model(
    models: tuple[str, ...] | list[str],
    provider: str | None = None,
) -> str | None:
    """Pick a sensible default from a provider listing; ``None`` if nothing usable.

    Excludes ``:free`` variants (frequently blocked by privacy guardrails and
    lower quality), prefers the provider's own brand families, then well-known
    cross-provider families, before falling back to the first remaining entry.
    """

    usable = [
        model for model in models if model and ":free" not in model and not model.startswith("@")
    ]
    for pattern in _PROVIDER_BRAND_PATTERNS.get(provider or "", ()):
        for model in usable:
            if pattern in model.lower():
                return model
    for pattern in _MODEL_PREFERENCE:
        for model in usable:
            if pattern in model.lower():
                return model
    return usable[0] if usable else None


def _credential_fingerprint(provider: str) -> str | None:
    """Stable hash of every resolved credential field; ``None`` if incomplete.

    The fingerprint doubles as the "do we have a usable credential" check: one
    missing field means no fetch is possible under any cache key.
    """

    fields = NATIVE_CREDENTIAL_FIELDS.get(provider)
    if fields is None:
        return None
    parts: list[str] = []
    for field in fields:
        value = resolve_native_field(provider, field)
        if value is None:
            return None
        parts.append(f"{field}={value}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def snapshot(provider: str) -> CatalogSnapshot | None:
    """Instant local read of the cached listing; ``None`` when nothing cached.

    Ignores the TTL -- staleness is the caller's call (see :func:`is_stale`).
    """

    fingerprint = _credential_fingerprint(provider)
    if fingerprint is None:
        return None
    entry = read_entry(f"{provider}:{fingerprint}", CATALOG_VERSION)
    if entry is None:
        return None
    fetched_at, value = entry
    if not isinstance(value, list):
        return None
    models = tuple(str(model) for model in value if model)
    return CatalogSnapshot(models=models, fetched_at=fetched_at)


def is_stale(snapshot: CatalogSnapshot | None, provider: str | None = None) -> bool:
    """True when there is no snapshot or it is older than the TTL."""

    if snapshot is None:
        return True
    if provider in AGGREGATOR_PROVIDERS:
        ttl = AGGREGATOR_CATALOG_TTL_SECONDS
    else:
        ttl = CATALOG_TTL_SECONDS
    return time.time() - snapshot.fetched_at > ttl


def store_models(provider: str, models: tuple[str, ...]) -> None:
    """Persist a successful listing (no-op for empty listings)."""

    if not models:
        return
    fingerprint = _credential_fingerprint(provider)
    if fingerprint is None:
        return
    write_entry(f"{provider}:{fingerprint}", CATALOG_VERSION, list(models))


async def refresh(
    provider: str,
    *,
    timeout: float = 10.0,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, ...]:
    """Fetch the listing over the network and store it.

    Any failure returns ``()`` -- this is the background path where nobody is
    watching an error message; callers just keep showing what they had.
    """

    try:
        result = await _fetch(provider, timeout=timeout, client=client)
    except Exception:  # noqa: BLE001 - background refresh never surfaces errors
        return ()
    if not result.ok or not result.models:
        return ()
    store_models(provider, result.models)
    return result.models


RECENTS_VERSION = "model-recents-v1"
MAX_RECENT_MODELS = 8


def record_recent(provider: str, model_id: str) -> None:
    """Add a model to the front of the recent list."""
    key = f"recents:{provider}"
    entry = read_entry(key, RECENTS_VERSION)
    recents: list[str] = []
    if entry is not None:
        _, value = entry
        if isinstance(value, list):
            recents = [str(m) for m in value if m]
    if model_id in recents:
        recents.remove(model_id)
    recents.insert(0, model_id)
    recents = recents[:MAX_RECENT_MODELS]
    write_entry(key, RECENTS_VERSION, recents)


def recent_models(provider: str) -> tuple[str, ...]:
    """Return recently used models, most recent first."""
    key = f"recents:{provider}"
    entry = read_entry(key, RECENTS_VERSION)
    if entry is None:
        return ()
    _, value = entry
    if not isinstance(value, list):
        return ()
    return tuple(str(m) for m in value if m)


async def _fetch(
    provider: str,
    *,
    timeout: float,
    client: httpx.AsyncClient | None,
) -> VerificationResult:
    """Verify-then-list against resolved credentials; raises without them."""

    fields_spec = NATIVE_CREDENTIAL_FIELDS.get(provider)
    if fields_spec is None:
        raise ValueError(f"unsupported provider for model discovery: {provider}")
    values: dict[str, str] = {}
    for field in fields_spec:
        value = resolve_native_field(provider, field)
        if value is None:
            raise ValueError(f"{provider} credential is incomplete (missing {field})")
        values[field] = value
    return await verify_native_credential(provider, values, timeout=timeout, client=client)


# ---------------------------------------------------------------------------
# Failed-model denylist
# ---------------------------------------------------------------------------


def report_failure(provider: str, model_id: str) -> None:
    """Record a model that exhausted its retry budget.

    The denial persists for ``DENYLIST_TTL_SECONDS``; after that the model is
    silently re-admitted so transient outages don't permanently hide it.
    """

    key = f"denylist:{provider}"
    entry = read_entry(key, DENYLIST_VERSION)
    if entry is not None and isinstance(entry[1], dict):
        denials: dict[str, float] = dict(entry[1])
    else:
        denials: dict[str, float] = {}
    denials[model_id] = time.time()
    write_entry(key, DENYLIST_VERSION, denials)


def denied_models(provider: str) -> dict[str, float]:
    """Return ``{model_id: denied_at_epoch}`` for models still within TTL."""

    key = f"denylist:{provider}"
    entry = read_entry(key, DENYLIST_VERSION)
    if entry is None or not isinstance(entry[1], dict):
        return {}
    now = time.time()
    return {
        model_id: denied_at
        for model_id, denied_at in entry[1].items()
        if isinstance(denied_at, (int, float)) and now - denied_at <= DENYLIST_TTL_SECONDS
    }


def clear_denial(provider: str, model_id: str) -> None:
    """Remove a model from the denylist (e.g. on successful use)."""

    key = f"denylist:{provider}"
    entry = read_entry(key, DENYLIST_VERSION)
    if entry is None or not isinstance(entry[1], dict):
        return
    denials: dict[str, float] = dict(entry[1])
    if model_id not in denials:
        return
    del denials[model_id]
    write_entry(key, DENYLIST_VERSION, denials)
