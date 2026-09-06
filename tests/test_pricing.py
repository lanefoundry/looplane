"""Tests for looplane.pricing cost estimation."""

from __future__ import annotations

import pytest

from looplane.pricing import estimate_cost, format_cost


class TestEstimateCost:
    def test_sonnet_4_basic(self) -> None:
        cost = estimate_cost("claude-sonnet-4-20250514", input_tokens=1000, output_tokens=500)
        assert cost is not None
        expected = (1000 * 3.0 + 500 * 15.0) / 1_000_000
        assert cost == pytest.approx(expected)

    def test_opus_4(self) -> None:
        cost = estimate_cost("claude-opus-4-20250514", input_tokens=1000, output_tokens=500)
        assert cost is not None
        expected = (1000 * 15.0 + 500 * 75.0) / 1_000_000
        assert cost == pytest.approx(expected)

    def test_haiku_35(self) -> None:
        cost = estimate_cost("claude-haiku-3-5-20241022", input_tokens=10000, output_tokens=1000)
        assert cost is not None
        expected = (10000 * 0.80 + 1000 * 4.0) / 1_000_000
        assert cost == pytest.approx(expected)

    def test_cached_input_discount(self) -> None:
        cost = estimate_cost(
            "claude-sonnet-4-20250514",
            input_tokens=10000,
            output_tokens=0,
            cached_input_tokens=8000,
        )
        assert cost is not None
        fresh = 2000 * 3.0 / 1_000_000
        cached = 8000 * 0.3 / 1_000_000
        assert cost == pytest.approx(fresh + cached)

    def test_cache_creation_premium(self) -> None:
        cost = estimate_cost(
            "claude-sonnet-4-20250514",
            input_tokens=0,
            output_tokens=0,
            cache_creation_tokens=10000,
        )
        assert cost is not None
        expected = 10000 * 3.75 / 1_000_000
        assert cost == pytest.approx(expected)

    def test_unknown_model_returns_none(self) -> None:
        assert estimate_cost("gpt-4o", input_tokens=100, output_tokens=50) is None

    def test_case_insensitive(self) -> None:
        cost = estimate_cost("Claude-Sonnet-4-20250514", input_tokens=1000, output_tokens=500)
        assert cost is not None

    def test_prefix_matching(self) -> None:
        cost1 = estimate_cost("claude-sonnet-4-20250514", input_tokens=1000, output_tokens=0)
        cost2 = estimate_cost("claude-sonnet-4-20260101", input_tokens=1000, output_tokens=0)
        assert cost1 == cost2

    def test_zero_tokens(self) -> None:
        cost = estimate_cost("claude-sonnet-4-20250514", input_tokens=0, output_tokens=0)
        assert cost == 0.0


class TestFormatCost:
    def test_under_penny(self) -> None:
        assert format_cost(0.001) == "<$0.01"
        assert format_cost(0.0) == "<$0.01"

    def test_under_dollar(self) -> None:
        assert format_cost(0.03) == "$0.03"
        assert format_cost(0.99) == "$0.99"

    def test_under_ten(self) -> None:
        assert format_cost(1.50) == "$1.50"
        assert format_cost(9.99) == "$9.99"

    def test_above_ten(self) -> None:
        assert format_cost(15.123) == "$15.1"
        assert format_cost(100.0) == "$100.0"
