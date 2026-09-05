import json
import pickle
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from looplane import contracts, tools
from looplane.policy import SafePathPolicy
from looplane.tooling import types
from looplane.tooling.definitions import tool_definitions


@pytest.mark.parametrize("name", ["ReviewablePatch", "ToolExecutionError", "_PathSnapshot"])
def test_facade_reexports_exact_canonical_objects(name: str) -> None:
    assert getattr(tools, name) is getattr(types, name)


@pytest.mark.parametrize(
    "name",
    ["ToolCall", "ToolDefinition", "ToolObservation", "VerificationCommand", "VerificationOutcome"],
)
def test_existing_domain_reexports_keep_their_owner(name: str) -> None:
    assert getattr(tools, name) is getattr(contracts, name)


def test_old_factory_is_the_canonical_function() -> None:
    assert tools.ToolExecutor._tool_definitions is tool_definitions


def test_builtin_output_matches_pre_extraction_snapshot_including_order() -> None:
    expected = Path(__file__).with_name("builtin_definitions.json").read_text()
    actual = json.dumps(
        [definition.model_dump(mode="json") for definition in tool_definitions()], indent=2
    ) + "\n"
    assert actual == expected


def test_definition_calls_do_not_share_mutable_schemas() -> None:
    first = tool_definitions()
    second = tool_definitions()
    assert first is not second
    assert all(left is not right for left, right in zip(first, second, strict=True))
    first[0].input_schema["properties"]["path"]["default"] = "changed"
    first[6].input_schema["properties"]["name"]["enum"].append("changed")
    assert second[0].input_schema["properties"]["path"]["default"] == "."
    assert second[6].input_schema["properties"]["name"]["enum"] == []


def test_executor_allowlist_is_sorted_and_does_not_leak_between_instances(tmp_path: Path) -> None:
    commands = tuple(
        contracts.VerificationCommand(name=name, argv=("unused",))
        for name in ("zeta", "alpha")
    )
    executor = tools.ToolExecutor(tmp_path, SafePathPolicy(tmp_path), commands)
    empty = tools.ToolExecutor(tmp_path, SafePathPolicy(tmp_path), ())
    expected = [item.model_dump(mode="json") for item in tool_definitions()]
    expected[6]["input_schema"]["properties"]["name"]["enum"] = ["alpha", "zeta"]
    assert [item.model_dump(mode="json") for item in executor.definitions] == expected
    assert empty.definitions == tool_definitions()
    assert executor.refresh_mcp_tool_definitions() is False
    assert [item.model_dump(mode="json") for item in executor.definitions] == expected


def test_value_types_remain_frozen_and_pickle_compatible() -> None:
    patch = types.ReviewablePatch("diff", ("one.py", "two.py"))
    assert patch == tools.ReviewablePatch("diff", ("one.py", "two.py"))
    assert hash(patch) == hash(tools.ReviewablePatch("diff", ("one.py", "two.py")))
    with pytest.raises(FrozenInstanceError):
        patch.content = "changed"
    snapshot = types._PathSnapshot(True, b"old", 0o644)
    with pytest.raises(FrozenInstanceError):
        snapshot.data = b"changed"
    assert pickle.loads(pickle.dumps(patch)) == patch
    assert pickle.loads(pickle.dumps(snapshot)) == snapshot
    # Protocol 0 GLOBAL references emitted by older releases must still resolve.
    for name in ("ReviewablePatch", "ToolExecutionError", "_PathSnapshot"):
        old_global = f"clooplane.tools\n{name}\n.".encode("ascii")
        assert pickle.loads(old_global) is getattr(types, name)


def test_executor_raises_the_canonical_exception(tmp_path: Path) -> None:
    executor = tools.ToolExecutor(tmp_path, SafePathPolicy(tmp_path), ())
    with pytest.raises(types.ToolExecutionError, match="steps must not be empty") as caught:
        executor.tool_program([])
    assert type(caught.value) is tools.ToolExecutionError
    assert isinstance(caught.value, RuntimeError)


@pytest.mark.parametrize("module", ["types", "definitions"])
def test_leaf_imports_do_not_load_the_facade_or_execution_stack(module: str) -> None:
    code = f"""
import importlib
import sys
importlib.import_module('looplane.tooling.{module}')
for name in (
    'looplane.tools', 'looplane.loop', 'looplane.cli', 'looplane.tui',
    'looplane.runtime', 'looplane.mcp_client', 'looplane.agent',
):
    assert name not in sys.modules, name
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr
