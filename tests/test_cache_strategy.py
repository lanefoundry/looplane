from __future__ import annotations

from rivumi.cache_strategy import (
    CacheAwarePromptOrderingMode,
    ProviderCacheTrace,
    anthropic_system_with_cache_control,
    apply_provider_cache_defaults,
    cache_aware_prompt_ordering,
    prompt_cache_key,
    prompt_sections_from_rendered,
    provider_cache_mapping,
    provider_cache_trace,
)
from rivumi.contracts import Message, ToolDefinition
from rivumi.prompts import PromptSection, render_prompt_sections


def test_prompt_sections_from_rendered_parses_rivumi_boundaries() -> None:
    prompt = render_prompt_sections(
        (
            PromptSection("core", "Stable rules", cache_stable=True),
            PromptSection("workspace", "Dynamic state"),
        )
    )

    sections = prompt_sections_from_rendered(prompt)

    assert [section.name for section in sections] == ["core", "workspace"]
    assert [section.cache for section in sections] == ["stable", "dynamic"]


def test_anthropic_cache_control_marks_only_stable_prefix_boundary() -> None:
    prompt = render_prompt_sections(
        (
            PromptSection("core", "Stable rules", cache_stable=True),
            PromptSection("workspace", "Dynamic state"),
        )
    )

    system = anthropic_system_with_cache_control(prompt)

    assert isinstance(system, list)
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in system[1]


def test_anthropic_cache_control_leaves_unsectioned_prompt_unchanged() -> None:
    assert anthropic_system_with_cache_control("plain prompt") == "plain prompt"


def test_prompt_cache_key_uses_stable_prompt_prefix_and_tools() -> None:
    stable = render_prompt_sections(
        (
            PromptSection("core", "Stable rules", cache_stable=True),
            PromptSection("workspace", "Dynamic state"),
        )
    )
    changed_dynamic = render_prompt_sections(
        (
            PromptSection("core", "Stable rules", cache_stable=True),
            PromptSection("workspace", "Different dynamic state"),
        )
    )
    tools = (
        ToolDefinition(
            name="read_file",
            input_schema={"type": "object"},
            read_only=True,
        ),
    )

    assert prompt_cache_key((Message(role="system", content=stable),), tools) == (
        prompt_cache_key((Message(role="system", content=changed_dynamic),), tools)
    )


def test_provider_cache_mapping_documents_default_locations() -> None:
    assert provider_cache_mapping("anthropic").anthropic_cache_control is True
    assert provider_cache_mapping("openai-compatible").prompt_cache_key_location == (
        "extra_body.prompt_cache_key"
    )
    assert provider_cache_mapping("openai-responses").prompt_cache_key_location == (
        "prompt_cache_key"
    )
    assert provider_cache_mapping("workers-ai").prompt_cache_key_location is None


def test_apply_provider_cache_defaults_preserves_existing_openai_hint() -> None:
    request = {"extra_body": {"prompt_cache_key": "caller:key", "think": False}}

    updated = apply_provider_cache_defaults(
        "openai-compatible",
        request,
        (Message(role="system", content="Stable rules"),),
        (),
        namespace="rivumi-openai",
    )

    assert updated["extra_body"] == {"prompt_cache_key": "caller:key", "think": False}
    assert request["extra_body"] == {"prompt_cache_key": "caller:key", "think": False}


def test_apply_provider_cache_defaults_adds_responses_hint() -> None:
    request = {"model": "gpt-5", "input": "task"}

    updated = apply_provider_cache_defaults(
        "openai-responses",
        request,
        (Message(role="system", content="Stable rules"),),
        (),
        namespace="rivumi-responses",
    )

    assert updated["prompt_cache_key"].startswith("rivumi-responses:")
    assert "prompt_cache_key" not in request


def test_apply_provider_cache_defaults_adds_anthropic_cache_control() -> None:
    system = render_prompt_sections(
        (
            PromptSection("core", "Stable rules", cache_stable=True),
            PromptSection("runtime", "Dynamic facts"),
        )
    )

    updated = apply_provider_cache_defaults(
        "anthropic",
        {"system": system},
        (Message(role="system", content=system),),
        (),
        namespace="rivumi-anthropic",
    )

    assert updated["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_provider_cache_trace_validates_openai_compatible_request_metadata() -> None:
    trace = provider_cache_trace(
        "openai-compatible",
        {
            "extra_body": {"prompt_cache_key": "rivumi-openai:abc"},
            "tools": [{"type": "function", "function": {"name": "read_file"}}],
        },
    )

    assert trace.cache_ready is True
    assert trace.prompt_cache_key == "rivumi-openai:abc"
    assert trace.tool_schema_fingerprint is not None
    assert trace.warnings == ()


def test_provider_cache_trace_validates_responses_request_metadata() -> None:
    trace = provider_cache_trace(
        "openai-responses",
        {
            "prompt_cache_key": "rivumi-responses:abc",
            "tools": [{"type": "function", "name": "read_file"}],
        },
    )

    assert trace.cache_ready is True
    assert trace.prompt_cache_key == "rivumi-responses:abc"
    assert trace.tool_schema_fingerprint is not None


def test_provider_cache_trace_validates_anthropic_cache_control_metadata() -> None:
    trace = provider_cache_trace(
        "anthropic",
        {
            "system": [
                {
                    "type": "text",
                    "text": "Stable",
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        },
    )

    assert trace.cache_ready is True
    assert trace.cache_control_blocks == 1
    assert trace.warnings == ()


def test_provider_cache_trace_warns_when_cache_metadata_is_missing() -> None:
    trace = provider_cache_trace("openai-responses", {"tools": []})

    assert trace.cache_ready is False
    assert trace.warnings == ("missing prompt_cache_key",)


def test_cache_aware_prompt_ordering_requires_ready_trace() -> None:
    sections = (
        PromptSection("workspace", "Dynamic state"),
        PromptSection("core", "Stable rules", cache_stable=True),
    )

    decision = cache_aware_prompt_ordering(sections)

    assert decision.ordered_sections == sections
    assert decision.reordered is False
    assert decision.trace_ready is False
    assert decision.warnings == (
        "cache-aware prompt ordering requires cache-ready provider traces",
    )


def test_cache_aware_prompt_ordering_can_be_disabled_by_call_site() -> None:
    sections = (
        PromptSection("workspace", "Dynamic state"),
        PromptSection("core", "Stable rules", cache_stable=True),
    )
    trace = ProviderCacheTrace(
        provider="openai-responses",
        prompt_cache_key="rivumi-responses:abc",
        tool_schema_fingerprint="tools",
        cache_control_blocks=0,
    )

    decision = cache_aware_prompt_ordering(
        sections,
        (trace,),
        mode=CacheAwarePromptOrderingMode.DISABLED,
    )

    assert decision.ordered_sections == sections
    assert decision.reordered is False
    assert decision.warnings == ("cache-aware prompt ordering disabled by call-site policy",)


def test_cache_aware_prompt_ordering_can_be_forced_for_validation() -> None:
    sections = (
        PromptSection("workspace", "Dynamic state"),
        PromptSection("core", "Stable rules", cache_stable=True),
    )

    decision = cache_aware_prompt_ordering(sections, mode=CacheAwarePromptOrderingMode.ALWAYS)

    assert [section.name for section in decision.ordered_sections] == ["core", "workspace"]
    assert decision.reordered is True
    assert decision.trace_ready is False
    assert decision.warnings == (
        "cache-aware prompt ordering forced without cache-ready provider traces",
    )


def test_cache_aware_prompt_ordering_moves_stable_sections_to_prefix() -> None:
    sections = (
        PromptSection("workspace", "Dynamic state"),
        PromptSection("core", "Stable rules", cache_stable=True),
        PromptSection("tools", "Stable tool policy", cache_stable=True),
        PromptSection("memory", "Dynamic memory"),
    )
    trace = ProviderCacheTrace(
        provider="openai-responses",
        prompt_cache_key="rivumi-responses:abc",
        tool_schema_fingerprint="tools",
        cache_control_blocks=0,
    )

    decision = cache_aware_prompt_ordering(sections, (trace,))

    assert [section.name for section in decision.ordered_sections] == [
        "core",
        "tools",
        "workspace",
        "memory",
    ]
    assert decision.stable_prefix_sections == ("core", "tools")
    assert decision.reordered is True
    assert decision.trace_ready is True
    assert decision.warnings == ()
