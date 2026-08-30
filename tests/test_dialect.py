"""Tests for the in-band XML tool-calling dialect."""

from __future__ import annotations

import pytest

from looplane.contracts import Message, ToolCall, ToolDefinition, ToolObservation
from looplane.dialect import (
    XmlDialect,
    encode_inband_history,
    resolve_dialect,
)


# ── Fixtures ────────────────────────────────────────────────────


@pytest.fixture
def dialect() -> XmlDialect:
    return XmlDialect()


@pytest.fixture
def read_file_tool() -> ToolDefinition:
    return ToolDefinition(
        name="read_file",
        description="Read a file from disk.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
        },
    )


@pytest.fixture
def replace_text_tool() -> ToolDefinition:
    return ToolDefinition(
        name="replace_text",
        description="Replace text in a file.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["path", "old", "new"],
        },
    )


@pytest.fixture
def search_tool() -> ToolDefinition:
    return ToolDefinition(
        name="search_text",
        description="Search files for a pattern.",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["pattern"],
        },
    )


# ── tool_instructions ───────────────────────────────────────────


class TestToolInstructions:
    def test_generates_catalog_with_tools(
        self,
        dialect: XmlDialect,
        read_file_tool: ToolDefinition,
        replace_text_tool: ToolDefinition,
    ) -> None:
        result = dialect.tool_instructions([read_file_tool, replace_text_tool])
        assert "<tools>" in result
        assert "</tools>" in result
        assert '"read_file"' in result
        assert '"replace_text"' in result
        assert "Read a file from disk." in result
        assert "Replace text in a file." in result

    def test_empty_tools_list(self, dialect: XmlDialect) -> None:
        result = dialect.tool_instructions([])
        assert "<tools>" in result
        assert "</tools>" in result
        # Catalog section is empty between tags
        assert "<tools>\n\n</tools>" in result

    def test_catalog_contains_format_guide(
        self,
        dialect: XmlDialect,
        read_file_tool: ToolDefinition,
    ) -> None:
        result = dialect.tool_instructions([read_file_tool])
        assert "<invoke" in result
        assert "<parameter" in result
        assert "## Format guide" in result
        assert "## Rules" in result


# ── parse_tool_calls ────────────────────────────────────────────


class TestParseToolCalls:
    def test_single_invoke(self, dialect: XmlDialect) -> None:
        text = '<invoke name="read_file"><parameter name="path">src/main.py</parameter></invoke>'
        calls = dialect.parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].name == "read_file"
        assert calls[0].arguments == {"path": "src/main.py"}
        assert calls[0].tool_call_id.startswith("inband_")

    def test_multiple_invokes(self, dialect: XmlDialect) -> None:
        text = (
            '<invoke name="read_file"><parameter name="path">a.py</parameter></invoke>'
            '<invoke name="read_file"><parameter name="path">b.py</parameter></invoke>'
        )
        calls = dialect.parse_tool_calls(text)
        assert len(calls) == 2
        assert calls[0].arguments["path"] == "a.py"
        assert calls[1].arguments["path"] == "b.py"
        # Each call gets a unique id
        assert calls[0].tool_call_id != calls[1].tool_call_id

    def test_nested_params(self, dialect: XmlDialect) -> None:
        text = (
            '<invoke name="replace_text">'
            '<parameter name="path">foo.py</parameter>'
            '<parameter name="old">def hello():</parameter>'
            '<parameter name="new">def greet():</parameter>'
            "</invoke>"
        )
        calls = dialect.parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].arguments == {
            "path": "foo.py",
            "old": "def hello():",
            "new": "def greet():",
        }

    def test_json_params(self, dialect: XmlDialect, search_tool: ToolDefinition) -> None:
        text = (
            '<invoke name="search_text">'
            '<parameter name="pattern">TODO</parameter>'
            '<parameter name="paths">["src/", "lib/"]</parameter>'
            "</invoke>"
        )
        calls = dialect.parse_tool_calls(text, tools=[search_tool])
        assert len(calls) == 1
        assert calls[0].arguments["pattern"] == "TODO"
        assert calls[0].arguments["paths"] == ["src/", "lib/"]

    def test_no_invoke_returns_empty(self, dialect: XmlDialect) -> None:
        text = "I will now analyze the codebase and make changes."
        calls = dialect.parse_tool_calls(text)
        assert calls == []

    def test_multiline_param_value(self, dialect: XmlDialect) -> None:
        text = (
            '<invoke name="replace_text">'
            '<parameter name="path">test.py</parameter>'
            '<parameter name="old">line1\nline2\nline3</parameter>'
            '<parameter name="new">changed</parameter>'
            "</invoke>"
        )
        calls = dialect.parse_tool_calls(text)
        assert calls[0].arguments["old"] == "line1\nline2\nline3"

    def test_surrounding_text_ignored(self, dialect: XmlDialect) -> None:
        text = (
            "Let me read that file now.\n\n"
            '<invoke name="read_file"><parameter name="path">x.py</parameter></invoke>\n\n'
            "I will analyze the result."
        )
        calls = dialect.parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].name == "read_file"


# ── parse_tool_calls with coercion ──────────────────────────────


class TestParseToolCallsCoercion:
    def test_string_stays_string(
        self,
        dialect: XmlDialect,
        read_file_tool: ToolDefinition,
    ) -> None:
        text = '<invoke name="read_file"><parameter name="path">42</parameter></invoke>'
        calls = dialect.parse_tool_calls(text, tools=[read_file_tool])
        # "path" is typed as string in schema → stays as string "42"
        assert calls[0].arguments["path"] == "42"
        assert isinstance(calls[0].arguments["path"], str)

    def test_number_becomes_number(
        self,
        dialect: XmlDialect,
        read_file_tool: ToolDefinition,
    ) -> None:
        text = '<invoke name="read_file"><parameter name="path">f.py</parameter><parameter name="offset">10</parameter></invoke>'
        calls = dialect.parse_tool_calls(text, tools=[read_file_tool])
        assert calls[0].arguments["offset"] == 10
        assert isinstance(calls[0].arguments["offset"], int)

    def test_array_becomes_list(
        self,
        dialect: XmlDialect,
        search_tool: ToolDefinition,
    ) -> None:
        text = (
            '<invoke name="search_text">'
            '<parameter name="pattern">hello</parameter>'
            '<parameter name="paths">["a.py", "b.py"]</parameter>'
            "</invoke>"
        )
        calls = dialect.parse_tool_calls(text, tools=[search_tool])
        assert calls[0].arguments["paths"] == ["a.py", "b.py"]
        assert isinstance(calls[0].arguments["paths"], list)

    def test_no_schema_tries_json_parse(self, dialect: XmlDialect) -> None:
        # Without tool definitions, numeric values are still coerced via JSON
        text = '<invoke name="unknown"><parameter name="n">99</parameter></invoke>'
        calls = dialect.parse_tool_calls(text, tools=[])
        assert calls[0].arguments["n"] == 99

    def test_no_schema_non_json_stays_string(self, dialect: XmlDialect) -> None:
        text = '<invoke name="unknown"><parameter name="msg">hello world</parameter></invoke>'
        calls = dialect.parse_tool_calls(text, tools=[])
        assert calls[0].arguments["msg"] == "hello world"
        assert isinstance(calls[0].arguments["msg"], str)


# ── Fabrication stripping ───────────────────────────────────────


class TestFabricationStripping:
    def test_strips_tool_response(self, dialect: XmlDialect) -> None:
        text = (
            '<invoke name="read_file"><parameter name="path">x.py</parameter></invoke>\n'
            "<tool_response>\nfake result\n</tool_response>"
        )
        calls = dialect.parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].name == "read_file"

    def test_strips_tool_response_with_attrs(self, dialect: XmlDialect) -> None:
        text = (
            '<invoke name="read_file"><parameter name="path">x.py</parameter></invoke>\n'
            '<tool_response id="1">\nfake\n</tool_response>'
        )
        calls = dialect.parse_tool_calls(text)
        assert len(calls) == 1

    def test_preserves_invoke_before_response(self, dialect: XmlDialect) -> None:
        text = (
            '<invoke name="read_file"><parameter name="path">a.py</parameter></invoke>\n'
            '<invoke name="read_file"><parameter name="path">b.py</parameter></invoke>\n'
            "<tool_response>\nhallucinated\n</tool_response>"
        )
        calls = dialect.parse_tool_calls(text)
        assert len(calls) == 2


# ── resolve_dialect ─────────────────────────────────────────────


class TestResolveDialect:
    def test_free_model_returns_xml(self) -> None:
        result = resolve_dialect("deepseek/deepseek-r1:free")
        assert isinstance(result, XmlDialect)

    def test_minimax_returns_xml(self) -> None:
        result = resolve_dialect("minimax/minimax-m3:free")
        assert isinstance(result, XmlDialect)

    def test_minimax_nonfree_returns_xml(self) -> None:
        result = resolve_dialect("minimax/minimax-m3")
        assert isinstance(result, XmlDialect)

    def test_gpt4_returns_none(self) -> None:
        assert resolve_dialect("gpt-4o") is None

    def test_claude_returns_none(self) -> None:
        assert resolve_dialect("claude-sonnet-4-20250514") is None

    def test_gemini_returns_none(self) -> None:
        assert resolve_dialect("gemini-2.5-flash") is None

    def test_supports_tool_calling_false(self) -> None:
        result = resolve_dialect("some-obscure-model", supports_tool_calling=False)
        assert isinstance(result, XmlDialect)

    def test_supports_tool_calling_true(self) -> None:
        assert resolve_dialect("some-model", supports_tool_calling=True) is None

    def test_unknown_model_defaults_native(self) -> None:
        assert resolve_dialect("totally-unknown-model-v7") is None


# ── resolve_dialect force ───────────────────────────────────────


class TestResolveDialectForce:
    def test_force_xml_always_returns_xml(self) -> None:
        # Even for a model that would normally be native
        result = resolve_dialect("gpt-4o", force_dialect="xml")
        assert isinstance(result, XmlDialect)

    def test_force_xml_overrides_supports_true(self) -> None:
        result = resolve_dialect(
            "gpt-4o", supports_tool_calling=True, force_dialect="xml"
        )
        assert isinstance(result, XmlDialect)

    def test_force_native_always_returns_none(self) -> None:
        # Even for a model that would normally use in-band
        assert resolve_dialect("minimax/minimax-m3:free", force_dialect="native") is None

    def test_force_native_overrides_supports_false(self) -> None:
        assert (
            resolve_dialect(
                "some-model", supports_tool_calling=False, force_dialect="native"
            )
            is None
        )


# ── encode_inband_history ───────────────────────────────────────


class TestEncodeInbandHistory:
    def test_tool_call_message_becomes_xml_text(self, dialect: XmlDialect) -> None:
        call = ToolCall(tool_call_id="tc1", name="read_file", arguments={"path": "x.py"})
        msg = Message(role="assistant", content="Let me read that.", tool_calls=(call,))
        result = encode_inband_history([msg], dialect)
        assert len(result) == 1
        encoded = result[0]
        assert isinstance(encoded, Message)
        assert encoded.role == "assistant"
        assert encoded.tool_calls == ()
        assert "Let me read that." in encoded.content
        assert '<invoke name="read_file">' in encoded.content
        assert '<parameter name="path">x.py</parameter>' in encoded.content

    def test_tool_observation_becomes_user_message(self, dialect: XmlDialect) -> None:
        obs = ToolObservation(
            tool_call_id="tc1",
            name="read_file",
            ok=True,
            content="file contents here",
        )
        result = encode_inband_history([obs], dialect)
        assert len(result) == 1
        encoded = result[0]
        assert isinstance(encoded, Message)
        assert encoded.role == "user"
        assert "<tool_response>" in encoded.content
        assert "file contents here" in encoded.content

    def test_error_observation_includes_error_prefix(self, dialect: XmlDialect) -> None:
        obs = ToolObservation(
            tool_call_id="tc1",
            name="read_file",
            ok=False,
            content="",
            error="file not found",
        )
        result = encode_inband_history([obs], dialect)
        encoded = result[0]
        assert isinstance(encoded, Message)
        assert "Error:" in encoded.content

    def test_plain_messages_pass_through(self, dialect: XmlDialect) -> None:
        user_msg = Message(role="user", content="Hello")
        system_msg = Message(role="system", content="You are helpful.")
        assistant_msg = Message(role="assistant", content="Sure!")
        result = encode_inband_history([user_msg, system_msg, assistant_msg], dialect)
        assert result == [user_msg, system_msg, assistant_msg]

    def test_mixed_conversation(self, dialect: XmlDialect) -> None:
        call = ToolCall(tool_call_id="tc1", name="read_file", arguments={"path": "a.py"})
        messages = [
            Message(role="user", content="Fix the bug."),
            Message(role="assistant", content="Reading file.", tool_calls=(call,)),
            ToolObservation(
                tool_call_id="tc1", name="read_file", ok=True, content="def main(): pass"
            ),
            Message(role="assistant", content="Done."),
        ]
        result = encode_inband_history(messages, dialect)
        assert len(result) == 4
        assert result[0].content == "Fix the bug."  # pass-through
        assert '<invoke name="read_file">' in result[1].content  # re-encoded
        assert "<tool_response>" in result[2].content  # re-encoded
        assert result[3].content == "Done."  # pass-through

    def test_assistant_without_tool_calls_passes_through(
        self, dialect: XmlDialect
    ) -> None:
        msg = Message(role="assistant", content="I understand.")
        result = encode_inband_history([msg], dialect)
        assert result == [msg]


# ── render_tool_call / render_tool_result roundtrip ─────────────


class TestRenderRoundtrip:
    def test_render_tool_call_basic(self, dialect: XmlDialect) -> None:
        call = ToolCall(
            tool_call_id="tc1", name="read_file", arguments={"path": "main.py"}
        )
        xml = dialect.render_tool_call(call)
        assert '<invoke name="read_file">' in xml
        assert '<parameter name="path">main.py</parameter>' in xml
        assert xml.endswith("</invoke>")

    def test_render_tool_call_multiple_params(self, dialect: XmlDialect) -> None:
        call = ToolCall(
            tool_call_id="tc1",
            name="replace_text",
            arguments={"path": "f.py", "old": "foo", "new": "bar"},
        )
        xml = dialect.render_tool_call(call)
        assert '<parameter name="path">f.py</parameter>' in xml
        assert '<parameter name="old">foo</parameter>' in xml
        assert '<parameter name="new">bar</parameter>' in xml

    def test_render_tool_call_non_string_value(self, dialect: XmlDialect) -> None:
        call = ToolCall(
            tool_call_id="tc1",
            name="search_text",
            arguments={"pattern": "TODO", "paths": ["src/", "lib/"]},
        )
        xml = dialect.render_tool_call(call)
        # Non-string values are JSON-serialized
        assert '["src/", "lib/"]' in xml

    def test_render_tool_call_escapes_name(self, dialect: XmlDialect) -> None:
        call = ToolCall(
            tool_call_id="tc1",
            name='tool"with"quotes',
            arguments={},
        )
        xml = dialect.render_tool_call(call)
        assert "&quot;" in xml

    def test_render_tool_result(self, dialect: XmlDialect) -> None:
        xml = dialect.render_tool_result(
            name="read_file", content="hello world", is_error=False
        )
        assert "<tool_response>" in xml
        assert "hello world" in xml
        assert "</tool_response>" in xml

    def test_render_tool_result_error(self, dialect: XmlDialect) -> None:
        xml = dialect.render_tool_result(
            name="read_file", content="not found", is_error=True
        )
        assert "<tool_response>" in xml
        assert "not found" in xml

    def test_roundtrip_render_then_parse(
        self,
        dialect: XmlDialect,
        read_file_tool: ToolDefinition,
    ) -> None:
        """Rendering a tool call and parsing it back recovers the same call."""
        original = ToolCall(
            tool_call_id="tc1",
            name="read_file",
            arguments={"path": "src/main.py", "offset": 10},
        )
        xml = dialect.render_tool_call(original)
        parsed = dialect.parse_tool_calls(xml, tools=[read_file_tool])
        assert len(parsed) == 1
        assert parsed[0].name == original.name
        assert parsed[0].arguments["path"] == original.arguments["path"]
        assert parsed[0].arguments["offset"] == original.arguments["offset"]
