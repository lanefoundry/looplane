"""Non-secret provider metadata: fixed base URLs for the ``rivumi-agent`` runtime.

Single source of truth for the endpoints ``cli.py`` (model construction) and
``provider_verification.py`` (connection checks) both need. Kept out of
``native_credentials.py`` on purpose: that module's scope is narrowly "local credential
storage", and base URLs are not secrets.
"""

from __future__ import annotations

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
