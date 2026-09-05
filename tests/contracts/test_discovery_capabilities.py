"""Check actual constructed sessions against registry discovery declarations."""

from dataclasses import replace

import pytest

from looplane.claude_agent_session import ClaudeAgentSession
from looplane.codex_app_server import CodexAppServerSession
from looplane.runtime_registry import (
    RUNTIME_REGISTRY,
    RuntimeCapability,
    validate_runtime_capabilities,
)


@pytest.mark.parametrize(
    ("slug", "session_type"),
    [("claude-code", ClaudeAgentSession), ("codex-cli", CodexAppServerSession)],
)
def test_constructed_live_session_matches_discovery(slug, session_type, tmp_path) -> None:
    session = session_type(working_directory=tmp_path)
    validate_runtime_capabilities(RUNTIME_REGISTRY[slug], session.capabilities)


@pytest.mark.parametrize(
    "removed",
    [RuntimeCapability.USAGE, RuntimeCapability.APPROVAL, RuntimeCapability.DIFF_REPORTING],
)
def test_live_capabilities_cannot_exceed_discovery(removed, tmp_path) -> None:
    session = ClaudeAgentSession(working_directory=tmp_path)
    adapter = RUNTIME_REGISTRY["claude-code"]
    inconsistent = replace(adapter, capabilities=adapter.capabilities - {removed})
    with pytest.raises(ValueError, match="discovery"):
        validate_runtime_capabilities(inconsistent, session.capabilities)


def test_discovery_usage_cannot_overclaim_live_session(tmp_path) -> None:
    session = CodexAppServerSession(working_directory=tmp_path)
    without_usage = session.capabilities.model_copy(update={"token_usage": False})
    with pytest.raises(ValueError, match="token usage"):
        validate_runtime_capabilities(RUNTIME_REGISTRY["codex-cli"], without_usage)
