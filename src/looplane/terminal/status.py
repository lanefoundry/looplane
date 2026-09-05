"""Pure status formatting and usage arithmetic for terminal displays."""

from __future__ import annotations

from looplane.contracts import Usage


def format_token_count(count: int) -> str:
    """Compact token count for status displays, e.g. 1234 -> 1.2k."""
    if count >= 1_000:
        return f"{count / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(count)


def _add_usage(left: Usage, right: Usage) -> Usage:
    return Usage(
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        cached_input_tokens=left.cached_input_tokens + right.cached_input_tokens,
        reasoning_tokens=left.reasoning_tokens + right.reasoning_tokens,
        provider_total_tokens=(
            (left.provider_total_tokens or left.total_tokens)
            + (right.provider_total_tokens or right.total_tokens)
        ),
    )


def _usage_bar(percent: float, *, width: int = 10) -> str:
    filled = max(0, min(width, round(percent / 100 * width)))
    return "▰" * filled + "▱" * (width - filled)
