"""Non-secret provider metadata: fixed base URLs for the ``rivumi-agent`` runtime.

Single source of truth for the endpoints ``cli.py`` (model construction) and
``provider_verification.py`` (connection checks) both need. Kept out of
``native_credentials.py`` on purpose: that module's scope is narrowly "local credential
storage", and base URLs are not secrets.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rivumi.contracts import CostBreakdown, Usage

ANTHROPIC_BASE_URL = "https://api.anthropic.com"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
WORKERS_AI_BASE_URL = "https://api.cloudflare.com/client/v4"

# Single API key, fixed OpenAI-compatible endpoint providers. Values verified against
# @earendil-works/pi-ai's own provider source (the package pi/omp depend on), except
# nvidia-nim/opencode-zen/ollama-cloud which come from the free-llm-models skill notes.
OPENAI_COMPATIBLE_BASE_URLS: dict[str, str] = {
    "openrouter": "https://openrouter.ai/api/v1",
    "deepseek": "https://api.deepseek.com",
    "groq": "https://api.groq.com/openai/v1",
    "moonshotai": "https://api.moonshot.ai/v1",
    "zai": "https://api.z.ai/api/coding/paas/v4",
    "xai": "https://api.x.ai/v1",
    "nvidia-nim": "https://integrate.api.nvidia.com/v1",
    "opencode-zen": "https://opencode.ai/zen/v1",
    "ollama-cloud": "https://ollama.com/v1",
}


@dataclass(frozen=True)
class TokenPricing:
    """USD per one million text tokens, maintained as an estimate table."""

    input_per_million: float
    output_per_million: float
    cached_input_per_million: float | None = None
    source_url: str = ""
    effective_date: str = "2026-08-29"


class ModelRole(StrEnum):
    """Non-behavioral model role labels for future rivumi-agent routing."""

    PRIMARY = "primary"
    CHEAP = "cheap"
    FAST = "fast"
    REASONING = "reasoning"
    SUMMARIZER = "summarizer"
    REVIEWER = "reviewer"
    PARSER = "parser"


@dataclass(frozen=True)
class ModelRoute:
    """Static provider/model candidate for one or more model roles."""

    provider: str
    model: str
    roles: frozenset[ModelRole]
    priority: int
    notes: str = ""


# Static estimates only. Absence from this table means Rivumi should show token usage
# without a dollar estimate instead of inventing a price.
TOKEN_PRICING: dict[tuple[str, str], TokenPricing] = {
    ("openai", "gpt-5"): TokenPricing(
        input_per_million=1.25,
        cached_input_per_million=0.125,
        output_per_million=10.0,
        source_url="https://platform.openai.com/docs/models/gpt-5-chat-latest",
    ),
    ("openai", "gpt-5-chat-latest"): TokenPricing(
        input_per_million=1.25,
        cached_input_per_million=0.125,
        output_per_million=10.0,
        source_url="https://platform.openai.com/docs/models/gpt-5-chat-latest",
    ),
    ("openai", "gpt-5-mini"): TokenPricing(
        input_per_million=0.25,
        cached_input_per_million=0.025,
        output_per_million=2.0,
        source_url="https://developers.openai.com/api/docs/models/gpt-5-mini",
    ),
    ("openai", "gpt-5.4-mini"): TokenPricing(
        input_per_million=0.75,
        cached_input_per_million=0.075,
        output_per_million=4.5,
        source_url="https://developers.openai.com/api/docs/models/gpt-5.4-mini",
    ),
}


MODEL_ROUTES: tuple[ModelRoute, ...] = (
    ModelRoute(
        provider="openai-compatible",
        model="gpt-5",
        roles=frozenset({ModelRole.PRIMARY, ModelRole.REASONING, ModelRole.REVIEWER}),
        priority=10,
        notes="Strong default when OpenAI billing is available.",
    ),
    ModelRoute(
        provider="openai-compatible",
        model="gpt-5-mini",
        roles=frozenset({ModelRole.CHEAP, ModelRole.FAST, ModelRole.SUMMARIZER, ModelRole.PARSER}),
        priority=10,
        notes="Lower-cost utility lane for summaries, parsing, and fallback work.",
    ),
    ModelRoute(
        provider="openai-compatible",
        model="gpt-5.4-mini",
        roles=frozenset({ModelRole.CHEAP, ModelRole.FAST, ModelRole.SUMMARIZER}),
        priority=20,
        notes="Backup mini-class OpenAI candidate.",
    ),
    ModelRoute(
        provider="opencode-zen",
        model="muse-spark-1.2-contributor-free",
        roles=frozenset({ModelRole.PARSER}),
        priority=50,
        notes="Explicit opt-in free-tier route; do not use for sensitive code.",
    ),
)


def pricing_for_model(provider: str, model: str) -> TokenPricing | None:
    """Return an exact or alias-normalized pricing row."""

    provider_key = provider.casefold()
    model_key = model.casefold()
    exact = TOKEN_PRICING.get((provider_key, model_key))
    if exact is not None:
        return exact
    if provider_key in {"openai-compatible", "openai-responses"}:
        return TOKEN_PRICING.get(("openai", model_key))
    return None


def _route_estimated_price(route: ModelRoute) -> float:
    pricing = pricing_for_model(route.provider, route.model)
    if pricing is None:
        return float("inf")
    return pricing.input_per_million + pricing.output_per_million


def role_candidates(
    role: ModelRole | str,
    *,
    provider: str | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return statically ranked provider/model candidates for a future model role."""

    role = ModelRole(role)
    routes = [
        route
        for route in MODEL_ROUTES
        if role in route.roles and (provider is None or route.provider == provider)
    ]
    routes.sort(key=lambda route: (route.priority, _route_estimated_price(route), route.model))
    return tuple((route.provider, route.model) for route in routes)


def estimate_cost(provider: str, model: str, usage: Usage) -> CostBreakdown | None:
    pricing = pricing_for_model(provider, model)
    if pricing is None:
        return None
    cached = min(usage.cached_input_tokens, usage.input_tokens)
    uncached_input = usage.input_tokens - cached
    return CostBreakdown(
        provider=provider,
        model=model,
        source="estimated",
        input_cost=uncached_input / 1_000_000 * pricing.input_per_million,
        cached_input_cost=(
            cached
            / 1_000_000
            * (pricing.cached_input_per_million or pricing.input_per_million)
        ),
        output_cost=usage.output_tokens / 1_000_000 * pricing.output_per_million,
    )


# Models that only implement the OpenAI Responses API on their provider's endpoint.
# These bypass OpenAICompatibleModel (whose /chat/completions passthrough fails on them,
# e.g. Zen muse-spark returning 500 on 2026-08-24) and are routed to ResponsesModel.
RESPONSES_PROTOCOL_MODELS: dict[str, frozenset[str]] = {
    "opencode-zen": frozenset({"muse-spark-1.2-contributor-free"}),
}


def uses_responses_protocol(provider: str, model: str) -> bool:
    """Whether ``model`` on ``provider`` must be reached via the Responses API."""

    return model in RESPONSES_PROTOCOL_MODELS.get(provider, frozenset())


def provider_base_url(provider: str) -> str | None:
    """Fixed base URL for ``provider``.

    Returns ``None`` for ``openai-compatible`` (user-supplied, via ``OPENAI_BASE_URL`` or a
    CLI flag) and for providers this catalog does not know about, such as ``ollama``.
    """

    if provider == "anthropic":
        return ANTHROPIC_BASE_URL
    if provider == "gemini":
        return GEMINI_BASE_URL
    if provider == "workers-ai":
        return WORKERS_AI_BASE_URL
    return OPENAI_COMPATIBLE_BASE_URLS.get(provider)
