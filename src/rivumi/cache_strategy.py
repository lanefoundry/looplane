"""Prompt cache strategy helpers for provider adapters."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from rivumi.contracts import ConversationItem, Message, ToolDefinition
from rivumi.prompts import PromptSection

_SECTION_RE = re.compile(
    r"<section name='(?P<name>[^']+)' cache='(?P<cache>stable|dynamic)'>\n"
    r"(?P<content>.*?)\n</section>",
    re.DOTALL,
)


@dataclass(frozen=True)
class RenderedPromptSection:
    name: str
    cache: str
    text: str


@dataclass(frozen=True)
class ProviderCacheTrace:
    provider: str
    prompt_cache_key: str | None
    tool_schema_fingerprint: str | None
    cache_control_blocks: int
    warnings: tuple[str, ...] = ()

    @property
    def cache_ready(self) -> bool:
        if self.provider == "anthropic":
            return self.cache_control_blocks > 0 and not self.warnings
        return self.prompt_cache_key is not None and not self.warnings


class CacheAwarePromptOrderingMode(StrEnum):
    """Call-site policy for cache-aware prompt section ordering."""

    DISABLED = "disabled"
    TRACE_READY = "trace_ready"
    ALWAYS = "always"


@dataclass(frozen=True)
class CacheAwarePromptOrdering:
    ordered_sections: tuple[PromptSection, ...]
    stable_prefix_sections: tuple[str, ...]
    reordered: bool
    trace_ready: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderCacheMapping:
    provider: str
    prompt_cache_key_location: str | None = None
    anthropic_cache_control: bool = False


def provider_cache_mapping(provider: str) -> ProviderCacheMapping:
    """Return Rivumi's default prompt-cache hint mapping for a provider adapter."""

    if provider == "anthropic":
        return ProviderCacheMapping(provider=provider, anthropic_cache_control=True)
    if provider == "openai-compatible":
        return ProviderCacheMapping(
            provider=provider,
            prompt_cache_key_location="extra_body.prompt_cache_key",
        )
    if provider == "openai-responses":
        return ProviderCacheMapping(provider=provider, prompt_cache_key_location="prompt_cache_key")
    return ProviderCacheMapping(provider=provider)


def apply_provider_cache_defaults(
    provider: str,
    request: dict[str, Any],
    messages: tuple[ConversationItem, ...],
    tools: tuple[ToolDefinition, ...],
    *,
    namespace: str,
) -> dict[str, Any]:
    """Apply provider-specific prompt-cache defaults without overwriting caller hints."""

    mapping = provider_cache_mapping(provider)
    result = dict(request)
    if mapping.anthropic_cache_control and isinstance(result.get("system"), str):
        result["system"] = anthropic_system_with_cache_control(result["system"])
    if mapping.prompt_cache_key_location == "prompt_cache_key":
        result.setdefault(
            "prompt_cache_key",
            prompt_cache_key(messages, tools, namespace=namespace),
        )
    elif mapping.prompt_cache_key_location == "extra_body.prompt_cache_key":
        extra_body = dict(result.get("extra_body") or {})
        extra_body.setdefault(
            "prompt_cache_key",
            prompt_cache_key(messages, tools, namespace=namespace),
        )
        result["extra_body"] = extra_body
    return result


def prompt_sections_from_rendered(prompt: str) -> tuple[RenderedPromptSection, ...]:
    """Parse Rivumi-rendered prompt section boundaries."""

    sections: list[RenderedPromptSection] = []
    position = 0
    for match in _SECTION_RE.finditer(prompt):
        if prompt[position : match.start()].strip():
            return ()
        sections.append(
            RenderedPromptSection(
                name=match.group("name"),
                cache=match.group("cache"),
                text=match.group(0),
            )
        )
        position = match.end()
    if not sections or prompt[position:].strip():
        return ()
    return tuple(sections)


def cache_aware_prompt_ordering(
    sections: Sequence[PromptSection],
    traces: Sequence[ProviderCacheTrace] = (),
    *,
    mode: CacheAwarePromptOrderingMode | str = CacheAwarePromptOrderingMode.TRACE_READY,
) -> CacheAwarePromptOrdering:
    """Return a trace-gated stable-prefix ordering for prompt sections."""

    original = tuple(sections)
    ordering_mode = CacheAwarePromptOrderingMode(mode)
    if not original:
        return CacheAwarePromptOrdering(
            ordered_sections=(),
            stable_prefix_sections=(),
            reordered=False,
            trace_ready=False,
            warnings=("no prompt sections supplied",),
        )
    stable_prefix_sections = _stable_prefix_names(original)
    if ordering_mode is CacheAwarePromptOrderingMode.DISABLED:
        return CacheAwarePromptOrdering(
            ordered_sections=original,
            stable_prefix_sections=stable_prefix_sections,
            reordered=False,
            trace_ready=False,
            warnings=("cache-aware prompt ordering disabled by call-site policy",),
        )
    warnings: list[str] = []
    trace_ready = bool(traces) and all(trace.cache_ready for trace in traces)
    if not trace_ready and ordering_mode is CacheAwarePromptOrderingMode.TRACE_READY:
        warnings.append("cache-aware prompt ordering requires cache-ready provider traces")
        for trace in traces:
            warnings.extend(trace.warnings)
        return CacheAwarePromptOrdering(
            ordered_sections=original,
            stable_prefix_sections=stable_prefix_sections,
            reordered=False,
            trace_ready=False,
            warnings=tuple(dict.fromkeys(warnings)),
        )
    if not trace_ready:
        warnings.append("cache-aware prompt ordering forced without cache-ready provider traces")
    stable = tuple(section for section in original if section.cache_stable)
    dynamic = tuple(section for section in original if not section.cache_stable)
    ordered = (*stable, *dynamic)
    return CacheAwarePromptOrdering(
        ordered_sections=ordered,
        stable_prefix_sections=tuple(section.name for section in stable),
        reordered=ordered != original,
        trace_ready=trace_ready,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def anthropic_system_with_cache_control(system: str) -> str | list[dict[str, Any]]:
    """Apply Anthropic prompt caching to the stable prompt prefix when possible."""

    sections = prompt_sections_from_rendered(system)
    if not sections:
        return system

    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": section.text} for section in sections
    ]
    cache_boundary: int | None = None
    for index, section in enumerate(sections):
        if section.cache != "stable":
            break
        cache_boundary = index
    if cache_boundary is not None:
        blocks[cache_boundary]["cache_control"] = {"type": "ephemeral"}
    return blocks


def prompt_cache_key(
    messages: tuple[ConversationItem, ...],
    tools: tuple[ToolDefinition, ...],
    *,
    namespace: str = "rivumi",
) -> str:
    """Build a stable provider prompt-cache affinity key."""

    stable_parts: list[str] = []
    for message in messages:
        if not isinstance(message, Message) or message.role != "system" or not message.content:
            continue
        sections = prompt_sections_from_rendered(message.content)
        if not sections:
            stable_parts.append(message.content)
            continue
        stable_parts.extend(section.text for section in sections if section.cache == "stable")
        break
    payload = {
        "namespace": namespace,
        "system": stable_parts,
        "tools": [tool.model_dump(mode="json") for tool in tools],
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"{namespace}:{digest[:48]}"


def _stable_prefix_names(sections: tuple[PromptSection, ...]) -> tuple[str, ...]:
    names: list[str] = []
    for section in sections:
        if not section.cache_stable:
            break
        names.append(section.name)
    return tuple(names)


def provider_cache_trace(provider: str, request: dict[str, Any]) -> ProviderCacheTrace:
    """Extract provider prompt-cache metadata from a concrete adapter request."""

    prompt_key = _request_prompt_cache_key(provider, request)
    tools = request.get("tools")
    fingerprint = _tool_schema_fingerprint(tools) if isinstance(tools, list) else None
    cache_control_blocks = _cache_control_block_count(request.get("system"))
    warnings: list[str] = []
    if provider == "anthropic":
        if cache_control_blocks == 0:
            warnings.append("missing Anthropic cache_control block")
    elif prompt_key is None:
        warnings.append("missing prompt_cache_key")
    if tools is not None and fingerprint is None:
        warnings.append("tool schema payload is not a list")
    return ProviderCacheTrace(
        provider=provider,
        prompt_cache_key=prompt_key,
        tool_schema_fingerprint=fingerprint,
        cache_control_blocks=cache_control_blocks,
        warnings=tuple(warnings),
    )


def _request_prompt_cache_key(provider: str, request: dict[str, Any]) -> str | None:
    if provider == "openai-compatible":
        extra_body = request.get("extra_body")
        if isinstance(extra_body, dict) and isinstance(extra_body.get("prompt_cache_key"), str):
            return extra_body["prompt_cache_key"]
        return None
    value = request.get("prompt_cache_key")
    return value if isinstance(value, str) else None


def _tool_schema_fingerprint(tools: list[Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(tools, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return digest[:48]


def _cache_control_block_count(system: object) -> int:
    if not isinstance(system, list):
        return 0
    return sum(
        1
        for block in system
        if isinstance(block, dict) and isinstance(block.get("cache_control"), dict)
    )
