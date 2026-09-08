"""Tests for tool argument validation and coercion."""

from __future__ import annotations

import pytest

from looplane.tooling.validation import (
    ValidationError,
    build_schema_index,
    validate_and_sanitize,
)

SEARCH_TEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1},
        "path": {"type": "string", "default": "."},
        "glob": {"type": ["string", "null"]},
        "case_sensitive": {"type": "boolean", "default": True},
        "regex": {"type": "boolean", "default": False},
    },
    "required": ["query"],
    "additionalProperties": False,
}

READ_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1},
    },
    "required": ["path"],
    "additionalProperties": False,
}

SHELL_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "minLength": 1},
    },
    "required": ["command"],
    "additionalProperties": False,
}

HTTP_SCHEMA = {
    "type": "object",
    "properties": {
        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
        "url": {"type": "string", "minLength": 1},
        "timeout": {"type": "integer", "default": 30, "minimum": 1, "maximum": 60},
    },
    "required": ["method", "url"],
    "additionalProperties": False,
}


class TestStripUnknownParams:
    """Layer 1: unknown parameters are stripped before dispatch."""

    def test_hallucinated_limit_on_search_text(self):
        result = validate_and_sanitize(
            "search_text",
            {"query": "foo", "limit": 10},
            SEARCH_TEXT_SCHEMA,
        )
        assert "limit" not in result
        assert result["query"] == "foo"

    def test_multiple_unknown_params_stripped(self):
        result = validate_and_sanitize(
            "search_text",
            {"query": "bar", "limit": 5, "offset": 0, "max_results": 10},
            SEARCH_TEXT_SCHEMA,
        )
        assert "limit" not in result
        assert "offset" not in result
        assert "max_results" not in result
        assert result["query"] == "bar"

    def test_valid_params_preserved(self):
        result = validate_and_sanitize(
            "search_text",
            {"query": "test", "path": "src/", "case_sensitive": False, "regex": True},
            SEARCH_TEXT_SCHEMA,
        )
        assert result == {
            "query": "test",
            "path": "src/",
            "case_sensitive": False,
            "regex": True,
        }

    def test_no_unknown_params_passthrough(self):
        result = validate_and_sanitize(
            "read_file",
            {"path": "foo.py", "offset": 10, "limit": 50},
            READ_FILE_SCHEMA,
        )
        assert result == {"path": "foo.py", "offset": 10, "limit": 50}


class TestCoercion:
    """Layer 2: common hallucination patterns are coerced."""

    def test_string_to_integer(self):
        result = validate_and_sanitize(
            "read_file",
            {"path": "a.py", "offset": "10", "limit": "50"},
            READ_FILE_SCHEMA,
        )
        assert result["offset"] == 10
        assert result["limit"] == 50
        assert isinstance(result["offset"], int)

    def test_string_to_boolean(self):
        result = validate_and_sanitize(
            "search_text",
            {"query": "x", "case_sensitive": "false", "regex": "true"},
            SEARCH_TEXT_SCHEMA,
        )
        assert result["case_sensitive"] is False
        assert result["regex"] is True

    def test_null_on_non_nullable_stripped(self):
        result = validate_and_sanitize(
            "search_text",
            {"query": "x", "path": None},
            SEARCH_TEXT_SCHEMA,
        )
        assert "path" not in result

    def test_string_null_to_none(self):
        result = validate_and_sanitize(
            "search_text",
            {"query": "x", "glob": "null"},
            SEARCH_TEXT_SCHEMA,
        )
        assert result["glob"] is None

    def test_int_to_bool(self):
        result = validate_and_sanitize(
            "search_text",
            {"query": "x", "regex": 1},
            SEARCH_TEXT_SCHEMA,
        )
        assert result["regex"] is True

    def test_enum_whitespace_trimmed(self):
        result = validate_and_sanitize(
            "http_request",
            {"method": "GET ", "url": "https://example.com"},
            HTTP_SCHEMA,
        )
        assert result["method"] == "GET"


class TestValidation:
    """Layer 3: required fields and types are validated."""

    def test_missing_required_field(self):
        with pytest.raises(ValidationError) as exc_info:
            validate_and_sanitize("search_text", {}, SEARCH_TEXT_SCHEMA)
        assert "query" in str(exc_info.value)
        assert "missing" in str(exc_info.value).lower()

    def test_wrong_type(self):
        with pytest.raises(ValidationError) as exc_info:
            validate_and_sanitize(
                "search_text",
                {"query": 123},
                SEARCH_TEXT_SCHEMA,
            )
        assert "wrong type" in str(exc_info.value).lower()

    def test_empty_string_on_minlength(self):
        with pytest.raises(ValidationError) as exc_info:
            validate_and_sanitize(
                "shell",
                {"command": ""},
                SHELL_SCHEMA,
            )
        assert "at least" in str(exc_info.value).lower()

    def test_integer_below_minimum(self):
        with pytest.raises(ValidationError) as exc_info:
            validate_and_sanitize(
                "read_file",
                {"path": "a.py", "offset": -1},
                READ_FILE_SCHEMA,
            )
        assert ">=" in str(exc_info.value)

    def test_integer_above_maximum(self):
        with pytest.raises(ValidationError) as exc_info:
            validate_and_sanitize(
                "http_request",
                {"method": "GET", "url": "https://x.com", "timeout": 999},
                HTTP_SCHEMA,
            )
        assert "<=" in str(exc_info.value)

    def test_invalid_enum_value(self):
        with pytest.raises(ValidationError) as exc_info:
            validate_and_sanitize(
                "http_request",
                {"method": "TRACE", "url": "https://x.com"},
                HTTP_SCHEMA,
            )
        assert "must be one of" in str(exc_info.value)

    def test_error_message_is_model_friendly(self):
        with pytest.raises(ValidationError) as exc_info:
            validate_and_sanitize("search_text", {"query": 123}, SEARCH_TEXT_SCHEMA)
        msg = str(exc_info.value)
        assert "Invalid arguments for tool 'search_text'" in msg
        assert "Parameter 'query' has wrong type" in msg

    def test_multiple_issues_reported(self):
        with pytest.raises(ValidationError) as exc_info:
            validate_and_sanitize(
                "http_request",
                {"method": "TRACE", "url": "", "timeout": 999},
                HTTP_SCHEMA,
            )
        err = exc_info.value
        assert len(err.issues) >= 2


class TestBuildSchemaIndex:
    def test_builds_from_definitions(self):
        from looplane.tooling.definitions import tool_definitions

        defs = tool_definitions()
        index = build_schema_index(defs)
        assert "search_text" in index
        assert "read_file" in index
        assert "properties" in index["search_text"]

    def test_empty_definitions(self):
        index = build_schema_index([])
        assert index == {}


class TestEndToEnd:
    """The exact bug scenario: search_text called with hallucinated limit."""

    def test_original_bug_now_strips_and_succeeds(self):
        result = validate_and_sanitize(
            "search_text",
            {"query": "subagent_dispatch", "limit": 10},
            SEARCH_TEXT_SCHEMA,
        )
        assert result == {"query": "subagent_dispatch"}

    def test_combined_strip_and_coerce(self):
        result = validate_and_sanitize(
            "search_text",
            {"query": "x", "case_sensitive": "true", "limit": 5, "unknown_flag": True},
            SEARCH_TEXT_SCHEMA,
        )
        assert result == {"query": "x", "case_sensitive": True}
        assert "limit" not in result
        assert "unknown_flag" not in result
