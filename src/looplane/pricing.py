"""Per-model token pricing for cost estimation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelPricing:
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_write_per_mtok: float


def _anthropic(input_price: float, output_price: float) -> ModelPricing:
    return ModelPricing(
        input_per_mtok=input_price,
        output_per_mtok=output_price,
        cache_read_per_mtok=input_price * 0.1,
        cache_write_per_mtok=input_price * 1.25,
    )


_PRICING: tuple[tuple[str, ModelPricing], ...] = (
    ("claude-opus-4", _anthropic(15.0, 75.0)),
    ("claude-sonnet-4", _anthropic(3.0, 15.0)),
    ("claude-sonnet-3-5", _anthropic(3.0, 15.0)),
    ("claude-sonnet-3.5", _anthropic(3.0, 15.0)),
    ("claude-haiku-3-5", _anthropic(0.80, 4.0)),
    ("claude-haiku-3.5", _anthropic(0.80, 4.0)),
    ("claude-3-opus", _anthropic(15.0, 75.0)),
    ("claude-3-5-sonnet", _anthropic(3.0, 15.0)),
    ("claude-3-5-haiku", _anthropic(0.80, 4.0)),
)


def _lookup(model: str) -> ModelPricing | None:
    lower = model.lower()
    for prefix, pricing in _PRICING:
        if lower.startswith(prefix):
            return pricing
    return None


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float | None:
    """Estimate USD cost for a model call. Returns None if model is unknown."""
    pricing = _lookup(model)
    if pricing is None:
        return None
    fresh_input = max(0, input_tokens - cached_input_tokens)
    cost = (
        fresh_input * pricing.input_per_mtok
        + cached_input_tokens * pricing.cache_read_per_mtok
        + cache_creation_tokens * pricing.cache_write_per_mtok
        + output_tokens * pricing.output_per_mtok
    ) / 1_000_000
    return cost


def format_cost(usd: float) -> str:
    """Human-readable cost string."""
    if usd < 0.01:
        return "<$0.01"
    if usd < 1.0:
        return f"${usd:.2f}"
    if usd < 10.0:
        return f"${usd:.2f}"
    return f"${usd:.1f}"
