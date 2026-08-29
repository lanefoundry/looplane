from __future__ import annotations

import pytest

from rivumi.cli_config import SUPPORTED_PROVIDERS
from rivumi.contracts import Usage
from rivumi.provider_catalog import ModelRole, estimate_cost, role_candidates


def test_estimate_cost_marks_static_table_as_estimated() -> None:
    cost = estimate_cost(
        "openai",
        "gpt-5-mini",
        Usage(input_tokens=1_000_000, cached_input_tokens=200_000, output_tokens=100_000),
    )

    assert cost is not None
    assert cost.source == "estimated"
    assert cost.input_cost == pytest.approx(0.2)
    assert cost.cached_input_cost == pytest.approx(0.005)
    assert cost.output_cost == pytest.approx(0.2)
    assert cost.total_cost == pytest.approx(0.405)


def test_unknown_model_has_no_cost_estimate() -> None:
    assert estimate_cost("example", "unknown", Usage(input_tokens=1)) is None


def test_role_candidates_are_ranked_static_metadata() -> None:
    assert role_candidates(ModelRole.CHEAP) == (
        ("openai-compatible", "gpt-5-mini"),
        ("openai-compatible", "gpt-5.4-mini"),
    )


def test_role_candidates_filter_provider() -> None:
    assert role_candidates(ModelRole.CHEAP, provider="openai-compatible") == (
        ("openai-compatible", "gpt-5-mini"),
        ("openai-compatible", "gpt-5.4-mini"),
    )


def test_role_candidates_allow_only_explicit_unknown_price_routes() -> None:
    assert ("opencode-zen", "muse-spark-1.2-contributor-free") in role_candidates(
        ModelRole.PARSER
    )
    assert role_candidates(ModelRole.PARSER, provider="unknown") == ()


def test_role_candidates_return_supported_native_providers() -> None:
    for role in ModelRole:
        for provider, _model in role_candidates(role):
            assert provider in SUPPORTED_PROVIDERS
