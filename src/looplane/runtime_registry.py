"""Runtime registry and capability matrix for external coding CLI runtimes.

Claude Code and Codex CLI are registered today. OpenCode, Pi, and OMP are added
in later M13 slices as ``RuntimeAdapter`` entries plus their conversation/backend
implementations; the TUI picker and ``run`` dispatch read from this registry, so
no per-runtime branching needs to be added elsewhere.

The registry never imports the vendor backend modules: backend classes are stored
as ``"module.ClassName"`` import paths and resolved lazily, so importing this
module does not pull in the heavy external-runner stack (preserving the lazy
startup work from M12).
"""

from __future__ import annotations

import importlib
import shutil
from dataclasses import dataclass, field
from enum import StrEnum


class RuntimeKind(StrEnum):
    NATIVE = "looplane-agent"
    EXTERNAL = "external"


class RuntimeCapability(StrEnum):
    STREAMING_TEXT = "streaming_text"
    TOOL_EVENTS = "tool_events"
    APPROVAL = "approval"
    DIFF_REPORTING = "diff_reporting"
    MULTI_TURN = "multi_turn"
    MODEL_SWITCHING = "model_switching"
    MCP = "mcp"
    CANCELLATION = "cancellation"
    USAGE = "usage"


@dataclass(frozen=True)
class RuntimeAdapter:
    slug: str
    label: str
    kind: RuntimeKind
    executable: str | None = None
    backend: str | None = None
    native_session: str | None = None
    model_options: tuple[tuple[str, str | None], ...] = ()
    capabilities: frozenset[RuntimeCapability] = field(default_factory=frozenset)


def _resolve_class(import_path: str) -> type:
    """Import and return a class by ``module.ClassName`` (lazy)."""

    module_name, _, cls_name = import_path.rpartition(".")
    module = importlib.import_module(module_name)
    return getattr(module, cls_name)


_NATIVE_CAPS = frozenset(
    {
        RuntimeCapability.STREAMING_TEXT,
        RuntimeCapability.TOOL_EVENTS,
        RuntimeCapability.APPROVAL,
        RuntimeCapability.DIFF_REPORTING,
        RuntimeCapability.MULTI_TURN,
        RuntimeCapability.MODEL_SWITCHING,
        RuntimeCapability.MCP,
        RuntimeCapability.CANCELLATION,
        RuntimeCapability.USAGE,
    }
)

RUNTIME_REGISTRY: dict[str, RuntimeAdapter] = {
    "claude-code": RuntimeAdapter(
        slug="claude-code",
        label="Claude Code · uses installed local login",
        kind=RuntimeKind.EXTERNAL,
        executable="claude",
        backend="looplane.claude_backend.ClaudeCodeBackend",
        native_session="looplane.claude_conversation.IsolatedClaudeConversation",
        model_options=(
            ("Automatic · account default (recommended)", None),
            ("Sonnet · daily coding", "sonnet"),
            ("Opus · complex reasoning", "opus"),
            ("Haiku · fast and efficient", "haiku"),
            ("Best · strongest available", "best"),
        ),
        capabilities=_NATIVE_CAPS,
    ),
    "codex-cli": RuntimeAdapter(
        slug="codex-cli",
        label="Codex CLI · uses installed local login",
        kind=RuntimeKind.EXTERNAL,
        executable="codex",
        backend="looplane.codex_backend.CodexCliBackend",
        native_session="looplane.codex_conversation.IsolatedCodexConversation",
        model_options=(
            ("Automatic · Codex default (recommended)", None),
            ("GPT-5.6 Terra · balanced", "gpt-5.6-terra"),
            ("GPT-5.6 Sol · strongest", "gpt-5.6-sol"),
            ("GPT-5.6 Luna · fast", "gpt-5.6-luna"),
        ),
        capabilities=_NATIVE_CAPS,
    ),
    "opencode": RuntimeAdapter(
        slug="opencode",
        label="OpenCode · local CLI, 75+ providers",
        kind=RuntimeKind.EXTERNAL,
        executable="opencode",
        backend="looplane.opencode_backend.OpenCodeBackend",
        model_options=(
            ("Automatic · configured provider (recommended)", None),
            ("Anthropic · Claude Sonnet", "anthropic/claude-sonnet-4-5"),
            ("OpenAI · GPT-5.6", "openai/gpt-5.6"),
            ("Google · Gemini 2.5 Pro", "google/gemini-2.5-pro"),
            ("Ollama · local model", "ollama/llama3.1"),
        ),
        capabilities=frozenset(
            {
                RuntimeCapability.STREAMING_TEXT,
                RuntimeCapability.TOOL_EVENTS,
                RuntimeCapability.MULTI_TURN,
                RuntimeCapability.MODEL_SWITCHING,
                RuntimeCapability.CANCELLATION,
                RuntimeCapability.USAGE,
                RuntimeCapability.MCP,
                RuntimeCapability.DIFF_REPORTING,
            }
        ),
    ),
    "pi": RuntimeAdapter(
        slug="pi",
        label="Pi · local coding agent (JSON event stream)",
        kind=RuntimeKind.EXTERNAL,
        executable="pi",
        backend="looplane.pi_backend.PiBackend",
        model_options=(
            ("Automatic · configured provider (recommended)", None),
            ("Anthropic · Claude Opus", "anthropic/claude-opus-4-7"),
            ("OpenAI · GPT-5.6", "openai/gpt-5.6"),
            ("Google · Gemini 2.5 Pro", "google/gemini-2.5-pro"),
            ("Ollama · local model", "ollama/llama3.1"),
        ),
        capabilities=frozenset(
            {
                RuntimeCapability.STREAMING_TEXT,
                RuntimeCapability.TOOL_EVENTS,
                RuntimeCapability.MULTI_TURN,
                RuntimeCapability.MODEL_SWITCHING,
                RuntimeCapability.CANCELLATION,
                RuntimeCapability.USAGE,
                RuntimeCapability.DIFF_REPORTING,
            }
        ),
    ),
    "omp": RuntimeAdapter(
        slug="omp",
        label="Oh My Pi · IDE-wired coding agent",
        kind=RuntimeKind.EXTERNAL,
        executable="omp",
        backend="looplane.omp_backend.OmpBackend",
        model_options=(
            ("Automatic · configured provider (recommended)", None),
            ("Anthropic · Claude Opus", "anthropic/claude-opus-4-7"),
            ("OpenAI · GPT-5.6", "openai/gpt-5.6"),
            ("Google · Gemini 2.5 Pro", "google/gemini-2.5-pro"),
            ("Ollama · local model", "ollama/llama3.1"),
        ),
        capabilities=frozenset(
            {
                RuntimeCapability.STREAMING_TEXT,
                RuntimeCapability.TOOL_EVENTS,
                RuntimeCapability.MULTI_TURN,
                RuntimeCapability.MODEL_SWITCHING,
                RuntimeCapability.CANCELLATION,
                RuntimeCapability.USAGE,
                RuntimeCapability.MCP,
                RuntimeCapability.DIFF_REPORTING,
            }
        ),
    ),
    "looplane-agent": RuntimeAdapter(
        slug="looplane-agent",
        label="looplane · API key or local model",
        kind=RuntimeKind.NATIVE,
        capabilities=_NATIVE_CAPS,
    ),
}


def runtime_options() -> tuple[tuple[str, str], ...]:
    """Selectable runtimes for the TUI picker, in registry order.

    External runtimes are listed only when their executable is installed, matching
    the previous ``_tui_runtime_options`` behavior.
    """

    out: list[tuple[str, str]] = []
    for slug, adapter in RUNTIME_REGISTRY.items():
        if (
            adapter.kind is RuntimeKind.EXTERNAL
            and adapter.executable
            and not shutil.which(adapter.executable)
        ):
            continue
        out.append((slug, adapter.label))
    return tuple(out)


def runtime_model_options(slug: str) -> tuple[tuple[str, str | None], ...]:
    """Model presets offered for ``slug`` in the TUI model picker."""

    adapter = RUNTIME_REGISTRY.get(slug)
    return adapter.model_options if adapter else ()


def runtime_model_map() -> dict[str, tuple[tuple[str, str | None], ...]]:
    """``slug -> model presets`` for every registered runtime (TUI ``runtime_models``)."""

    return {slug: adapter.model_options for slug, adapter in RUNTIME_REGISTRY.items()}


def external_runtimes() -> tuple[str, ...]:
    return tuple(
        slug for slug, adapter in RUNTIME_REGISTRY.items() if adapter.kind is RuntimeKind.EXTERNAL
    )
