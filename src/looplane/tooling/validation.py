"""Tool call argument validation and coercion.

Validates LLM-supplied arguments against tool JSON Schemas before dispatch,
preventing TypeError from hallucinated parameters and giving the model
structured feedback to self-correct.

Three layers applied in order:
1. Strip unknown properties not declared in the schema.
2. Coerce common hallucination patterns (string↔number, null on optional).
3. Validate required fields and types; return a model-facing error on failure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from looplane.contracts import ToolDefinition


class ValidationError(Exception):
    """Structured validation failure with a model-friendly message."""

    def __init__(self, tool_name: str, issues: Sequence[str]) -> None:
        self.tool_name = tool_name
        self.issues = list(issues)
        super().__init__(self.format())

    def format(self) -> str:
        lines = [f"Invalid arguments for tool '{self.tool_name}':"]
        for issue in self.issues:
            lines.append(f"  - {issue}")
        return "\n".join(lines)


def build_schema_index(
    definitions: Sequence[ToolDefinition],
) -> dict[str, dict[str, Any]]:
    return {d.name: d.input_schema for d in definitions if d.input_schema}


def validate_and_sanitize(
    name: str,
    arguments: Mapping[str, Any],
    schema: dict[str, Any],
) -> dict[str, Any]:
    """Validate and sanitize tool arguments against a JSON Schema.

    Returns cleaned arguments ready for handler dispatch.
    Raises ValidationError with a model-friendly message on failure.
    """
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    args = dict(arguments)

    # --- Layer 1: strip unknown parameters ---
    allowed = set(properties.keys())
    unknown = set(args.keys()) - allowed
    for key in unknown:
        del args[key]

    # --- Layer 2: coerce common hallucination patterns ---
    args = _coerce(args, properties)

    # --- Layer 3: validate required fields and types ---
    issues: list[str] = []

    for field in required:
        if field not in args:
            prop = properties.get(field, {})
            desc = prop.get("description", "")
            hint = f" ({desc})" if desc else ""
            issues.append(f"Required parameter '{field}' is missing{hint}")

    for field, value in args.items():
        prop = properties.get(field, {})
        if prop:
            issue = _check_type(field, value, prop)
            if issue:
                issues.append(issue)

    if issues:
        raise ValidationError(name, issues)

    return args


def _coerce(args: dict[str, Any], properties: dict[str, Any]) -> dict[str, Any]:
    """Coerce arguments toward expected types when the intent is unambiguous."""
    for field, value in list(args.items()):
        prop = properties.get(field, {})
        if not prop:
            continue
        expected = prop.get("type")
        if expected is None:
            continue

        types = expected if isinstance(expected, list) else [expected]

        # null/None on optional: strip it so the handler uses its default
        if value is None and "null" not in types:
            del args[field]
            continue

        # string "null" → None when null is allowed
        if isinstance(value, str) and value == "null" and "null" in types:
            args[field] = None
            continue

        # string → integer coercion
        if isinstance(value, str) and "integer" in types and "string" not in types:
            try:
                args[field] = int(value)
                continue
            except (ValueError, TypeError):
                pass

        # string → number coercion
        if isinstance(value, str) and "number" in types and "string" not in types:
            try:
                args[field] = float(value)
                continue
            except (ValueError, TypeError):
                pass

        # string → boolean coercion
        if isinstance(value, str) and "boolean" in types and "string" not in types:
            lower = value.lower().strip()
            if lower in ("true", "1", "yes"):
                args[field] = True
                continue
            if lower in ("false", "0", "no"):
                args[field] = False
                continue

        # int → bool when boolean expected (LLM sends 0/1)
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and "boolean" in types
            and "integer" not in types
        ):
            args[field] = bool(value)
            continue

        # bool → int when integer expected
        if isinstance(value, bool) and "integer" in types and "boolean" not in types:
            args[field] = int(value)
            continue

        # string with extra whitespace on enum values
        if isinstance(value, str) and "enum" in prop:
            stripped = value.strip()
            if stripped in prop["enum"] and value not in prop["enum"]:
                args[field] = stripped
                continue

    return args


_JSON_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
    "null": (type(None),),
}


def _check_type(field: str, value: Any, prop: dict[str, Any]) -> str | None:
    """Return a type-mismatch issue string, or None if valid."""
    expected = prop.get("type")
    if expected is None:
        return None

    types = expected if isinstance(expected, list) else [expected]
    allowed_types: tuple[type, ...] = ()
    for t in types:
        allowed_types += _JSON_TYPE_MAP.get(t, ())

    if not allowed_types:
        return None

    # bool is a subclass of int in Python — reject bool when only integer is expected
    if isinstance(value, bool) and bool not in allowed_types:
        got = "boolean"
        return f"Parameter '{field}' has wrong type: expected {'/'.join(types)}, got {got}"

    if not isinstance(value, allowed_types):
        got = type(value).__name__
        return f"Parameter '{field}' has wrong type: expected {'/'.join(types)}, got {got}"

    # Additional constraints
    if isinstance(value, str):
        min_len = prop.get("minLength")
        if min_len is not None and len(value) < min_len:
            return f"Parameter '{field}' must be at least {min_len} character(s)"

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = prop.get("minimum")
        if minimum is not None and value < minimum:
            return f"Parameter '{field}' must be >= {minimum}, got {value}"
        maximum = prop.get("maximum")
        if maximum is not None and value > maximum:
            return f"Parameter '{field}' must be <= {maximum}, got {value}"

    if isinstance(value, str) and "enum" in prop and value not in prop["enum"]:
        return f"Parameter '{field}' must be one of {prop['enum']}, got '{value}'"

    return None
