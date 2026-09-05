"""In-band XML tool-calling dialect for models without native function calling.

When a model does not support provider-native tool calling (or when the user
forces in-band mode), this module:

1. Generates XML format instructions injected into the system prompt.
2. Re-encodes conversation history so prior tool calls and results appear
   as XML text the model can read back.
3. Parses the model's text output to extract ``<invoke>`` / ``<parameter>``
   blocks back into structured ``ToolCall`` objects.

The harness sends the request *without* a ``tools`` parameter; the model
emits tool calls as literal XML text, and this module converts them into
the same ``ModelTurn`` shape that native tool calling produces — making
the dialect fully transparent to ``loop.py``.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Sequence

    from looplane.contracts import (
        ConversationItem,
        ToolCall,
        ToolDefinition,
    )


# ── Dialect protocol ────────────────────────────────────────────


class Dialect(ABC):
    """Base class for in-band tool-calling dialects."""

    @abstractmethod
    def tool_instructions(self, tools: Sequence[ToolDefinition]) -> str:
        """Generate XML format instructions + tool catalog for system prompt."""
        ...

    @abstractmethod
    def parse_tool_calls(
        self,
        text: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> list[ToolCall]:
        """Parse model text output and extract in-band tool calls."""
        ...

    @abstractmethod
    def render_tool_call(self, call: ToolCall) -> str:
        """Render a structured tool call as XML text (for history re-encoding)."""
        ...

    @abstractmethod
    def render_tool_result(
        self,
        *,
        name: str,
        content: str,
        is_error: bool = False,
    ) -> str:
        """Render a tool result as XML text (for history re-encoding)."""
        ...

    def should_use_native_tools(self) -> bool:  # noqa: PLR6301
        """In-band dialects always return False — suppress the ``tools`` parameter."""
        return False

    @property
    @abstractmethod
    def response_open_tokens(self) -> tuple[str, ...]:
        """Tokens that signal the model is fabricating its own tool result.

        Text at or after the first occurrence of any token is stripped before
        invoke parsing so that hallucinated results do not leak through.
        """
        ...

    @property
    def excluded_tools(self) -> frozenset[str]:
        """Tool names too complex for weak models to use via in-band XML.

        These are filtered out of the tool catalog sent to the model.
        """
        return frozenset()


# ── Constants ───────────────────────────────────────────────────

_TOOL_CATALOG_TEMPLATE = """\
# Tools

You may call one or more functions to assist with the user query.
Tool calls are emitted as text using the exact syntax below, not as native \
provider tool messages.

Available functions are listed inside `<tools></tools>` as one JSON object \
per line:

<tools>
{tools}
</tools>

## Format guide

A call is one `<invoke>` element whose `<parameter>` children carry its \
arguments:

```text
<invoke name="fn"><parameter name="arg">value</parameter></invoke>
```

Emit consecutive `<invoke>…</invoke>` blocks for multiple calls; you MAY \
wrap them in `<tool_calls>…</tool_calls>`. Each call's result arrives as a \
response block:

```text
<tool_response>
verbatim tool result
</tool_response>
```

## Rules

- `name` MUST match a listed function.
- Parameter values are read literally by regex (delimiter matching), NOT a \
real XML parser: write them verbatim and never HTML-escape (emit `a & b`, \
never `a &amp; b`; `<`/`>` stay literal too). Only the body's own \
`</parameter>` closing tag is reserved. Non-string values are JSON.
- Read each `<tool_response>` in call order. NEVER emit `<tool_response>` \
yourself.
- Emit the stop sequence ONLY after the call is fully written — NEVER \
announce a tool then stop. Write the complete call, THEN the stop sequence, \
THEN halt.

## Usage examples

### Reading a file (always read before editing):
<invoke name="read_file"><parameter name="path">src/main.py</parameter></invoke>

### Editing with replace_text:
<invoke name="replace_text"><parameter name="path">src/main.py</parameter>\
<parameter name="old_text">old exact text</parameter>\
<parameter name="new_text">new text</parameter></invoke>

## Efficiency rules
- For LARGE changes (translating, reformatting, or rewriting most of a file): use replace_text \
with LARGE blocks — replace entire sections or the whole file at once, not line by line
- NEVER make more than 5 replace_text calls total — combine edits into fewer, larger replacements
- Minimize total tool calls — fewer calls = faster completion

## Important rules
- ALWAYS read a file before editing it — never guess the content
- replace_text: the old_text must EXACTLY match existing file content (character-for-character)
- Only use tools listed above — do NOT invent tool names"""

# Regex to match <invoke name="...">...</invoke> blocks.
# Uses re.DOTALL so `.` matches newlines inside parameter values.
_INVOKE_RE = re.compile(
    r'<invoke\s+name\s*=\s*"([^"]+)"\s*>(.*?)</invoke>',
    re.DOTALL,
)

# Regex to extract <parameter name="...">...</parameter> within an invoke body.
_PARAM_RE = re.compile(
    r'<parameter\s+name\s*=\s*"([^"]+)"(?:\s+[^>]*)?>(.*?)</parameter>',
    re.DOTALL,
)

# Tokens that mark the start of a fabricated tool result.
_RESPONSE_OPEN_TOKENS = ("<tool_response>", "<tool_response ")


# ── Helpers ─────────────────────────────────────────────────────


def _escape_xml_attr(value: str) -> str:
    """Escape a string for use inside an XML attribute value (double-quoted)."""
    return (
        value
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _strip_fabricated_responses(text: str) -> str:
    """Truncate text at the first fabricated ``<tool_response>`` token.

    Models occasionally hallucinate tool results after their tool calls.
    Everything from the first response-open token onward is discarded so the
    harness executes the real tool itself.
    """
    for token in _RESPONSE_OPEN_TOKENS:
        idx = text.find(token)
        if idx != -1:
            return text[:idx]
    return text


def _param_schema(
    tool: ToolDefinition,
    param_name: str,
) -> dict[str, Any] | None:
    """Extract the JSON Schema for a single parameter from a tool definition."""
    props = tool.input_schema.get("properties")
    if not isinstance(props, dict):
        return None
    prop = props.get(param_name)
    return prop if isinstance(prop, dict) else None


def _coerce_param_value(
    raw: str,
    *,
    schema: dict[str, Any] | None = None,
) -> object:
    """Coerce a raw parameter string to its appropriate Python type.

    String-typed parameters (per schema) are returned as-is.
    Everything else attempts JSON parse; falls back to raw string on failure.
    """
    if schema is not None and schema.get("type") == "string":
        return raw
    trimmed = raw.strip()
    if not trimmed:
        return raw
    try:
        return json.loads(trimmed)
    except (json.JSONDecodeError, ValueError):
        return raw


# ── XmlDialect ──────────────────────────────────────────────────


class XmlDialect(Dialect):
    """Generic XML in-band tool-calling dialect.

    Based on OMP's ``xml`` dialect — the simplest and most universally
    compatible format.  Uses ``<invoke>``/``<parameter>`` tags without
    any mandatory wrapper element.
    """

    def tool_instructions(self, tools: Sequence[ToolDefinition]) -> str:
        catalog_lines: list[str] = []
        for tool in tools:
            entry = {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.input_schema,
                },
            }
            catalog_lines.append(json.dumps(entry, ensure_ascii=False))
        return _TOOL_CATALOG_TEMPLATE.format(tools="\n".join(catalog_lines))

    def parse_tool_calls(
        self,
        text: str,
        tools: Sequence[ToolDefinition] = (),
    ) -> list[ToolCall]:
        from looplane.contracts import ToolCall as TC

        # Strip any fabricated tool-response blocks the model hallucinated.
        clean = _strip_fabricated_responses(text)

        tool_map = {t.name: t for t in tools}
        calls: list[TC] = []
        for invoke_match in _INVOKE_RE.finditer(clean):
            name = invoke_match.group(1).strip()
            body = invoke_match.group(2)
            arguments: dict[str, Any] = {}
            tool_def = tool_map.get(name)
            if tool_map and tool_def is None:
                continue  # Skip hallucinated / unknown tool names
            for param_match in _PARAM_RE.finditer(body):
                param_name = param_match.group(1).strip()
                raw_value = param_match.group(2)
                schema = _param_schema(tool_def, param_name) if tool_def else None
                arguments[param_name] = _coerce_param_value(raw_value, schema=schema)
            calls.append(
                TC(
                    tool_call_id=f"inband_{uuid4().hex[:12]}",
                    name=name,
                    arguments=arguments,
                )
            )
        return calls

    def render_tool_call(self, call: ToolCall) -> str:
        parts: list[str] = [f'<invoke name="{_escape_xml_attr(call.name)}">']
        for key, value in call.arguments.items():
            rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            parts.append(
                f'<parameter name="{_escape_xml_attr(key)}">{rendered}</parameter>'
            )
        parts.append("</invoke>")
        return "".join(parts)

    def render_tool_result(
        self,
        *,
        name: str,
        content: str,
        is_error: bool = False,
    ) -> str:
        return f"<tool_response>\n{content}\n</tool_response>"

    @property
    def response_open_tokens(self) -> tuple[str, ...]:
        return _RESPONSE_OPEN_TOKENS

    @property
    def excluded_tools(self) -> frozenset[str]:
        # Weak models cannot reliably generate unified text diffs.
        return frozenset({"apply_patch"})


# ── Dialect resolution ──────────────────────────────────────────

# Model-name substrings that reliably indicate native tool-calling support.
_NATIVE_TOOL_MODEL_PATTERNS: tuple[str, ...] = (
    "gpt-4",
    "gpt-3.5",
    "claude-",
    "gemini-",
    "o1-",
    "o3-",
    "o4-",
)

# Model-name substrings / suffixes that indicate the model likely does NOT
# support native tool calling and should default to in-band dialect.
_INBAND_MODEL_PATTERNS: tuple[tuple[str, ...], ...] = (
    # OpenRouter free-tier models
    (":free",),
    # MiniMax (unreliable native tool calling on many providers)
    ("minimax",),
    # Local / quantised formats
    ("gguf", "ggml"),
)


def resolve_dialect(
    model_name: str,
    *,
    supports_tool_calling: bool | None = None,
    force_dialect: str | None = None,
) -> Dialect | None:
    """Determine whether a model needs an in-band dialect.

    Returns:
        An :class:`XmlDialect` instance when in-band tool calling should be
        used, or ``None`` when native tool calling is available.

    Resolution order:
        1. ``force_dialect="xml"``    → always use XmlDialect.
        2. ``force_dialect="native"`` → always use native (``None``).
        3. ``supports_tool_calling=False`` → XmlDialect.
        4. ``supports_tool_calling=True``  → native.
        5. Model-name heuristic.
    """
    # Explicit override
    if force_dialect == "native":
        return None
    if force_dialect is not None:
        # Currently only "xml"; extend here when adding more dialects.
        return XmlDialect()

    # Adapter-declared capability
    if supports_tool_calling is False:
        return XmlDialect()
    if supports_tool_calling is True:
        return None

    # Heuristic — check known-good patterns first
    lower = model_name.lower()
    for pattern in _NATIVE_TOOL_MODEL_PATTERNS:
        if pattern in lower:
            return None
    for pattern_group in _INBAND_MODEL_PATTERNS:
        if any(p in lower for p in pattern_group):
            return XmlDialect()

    # Default: assume native tool calling is available
    return None


# ── History re-encoding ─────────────────────────────────────────


def encode_inband_history(
    messages: Sequence[ConversationItem],
    dialect: Dialect,
) -> list[ConversationItem]:
    """Re-encode structured tool-call / result items as plain XML text.

    When using an in-band dialect the model must see tool calls and results
    as XML text in the conversation history, not as separate structured items.

    Converts:
      - Assistant messages with ``tool_calls`` → assistant messages whose
        ``content`` contains the rendered XML.
      - ``ToolObservation`` items → ``Message(role="user", ...)`` carrying
        the XML result block.
      - Everything else passes through unchanged.
    """
    from looplane.contracts import Message, ToolObservation

    result: list[ConversationItem] = []
    for item in messages:
        if isinstance(item, Message) and item.role == "assistant" and item.tool_calls:
            xml_parts: list[str] = []
            if item.content:
                xml_parts.append(item.content)
            for call in item.tool_calls:
                xml_parts.append(dialect.render_tool_call(call))
            result.append(
                Message(
                    role="assistant",
                    content="\n".join(xml_parts),
                    tool_calls=(),
                )
            )
        elif isinstance(item, ToolObservation):
            if item.ok:
                obs_content = item.content
            else:
                error_parts = []
                if item.error:
                    error_parts.append(f"Error: {item.error}")
                if item.content:
                    error_parts.append(item.content)
                obs_content = "\n".join(error_parts) or "Error: tool failed"
            result.append(
                Message(
                    role="user",
                    content=dialect.render_tool_result(
                        name=item.name,
                        content=obs_content,
                        is_error=not item.ok,
                    ),
                )
            )
        else:
            result.append(item)
    return result
