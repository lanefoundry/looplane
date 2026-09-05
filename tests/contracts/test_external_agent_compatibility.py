"""Naming-only compatibility for external-agent runners and legacy patch points."""

from __future__ import annotations

import pickle
import sys
from importlib import import_module
from pathlib import Path

import pytest

from looplane.external_agents import ExternalAgentBackend, ExternalAgentRunner, ExternalAgentTask
from looplane.omp_runner import OmpRunner
from looplane.opencode_runner import OpenCodeRunner
from looplane.pi_runner import PiRunner
from looplane.structured_cli_runner import StructuredCliRunner

VENDORS = [
    ("codex", "CodexCliBackend", "CodexCliRunner"),
    ("claude", "ClaudeCodeBackend", "ClaudeCodeRunner"),
    ("opencode", "OpenCodeBackend", "OpenCodeRunner"),
    ("pi", "PiBackend", "PiRunner"),
    ("omp", "OmpBackend", "OmpRunner"),
]
ALIASES = [
    (f"{vendor}_backend", f"{vendor}_runner", old, new)
    for vendor, old, new in VENDORS
] + [("external_cli_base", "structured_cli_runner", "StreamJsonCliBackend", "StructuredCliRunner")]


@pytest.mark.parametrize("legacy_module,entry_module,old_name,new_name", ALIASES)
def test_canonical_class_is_legacy_alias(
    legacy_module: str, entry_module: str, old_name: str, new_name: str
) -> None:
    legacy = import_module(f"looplane.{legacy_module}")
    canonical = getattr(import_module(f"looplane.{entry_module}"), new_name)

    assert canonical is getattr(legacy, old_name)
    assert canonical is getattr(legacy, new_name)
    assert canonical.__name__ == new_name
    assert canonical.__module__ == legacy.__name__
    assert pickle.loads(pickle.dumps(canonical)) is canonical
    # Protocol-0 GLOBAL references model pickles written with the old class name.
    old_pickle = f"clooplane.{legacy_module}\n{old_name}\n.".encode("ascii")
    assert pickle.loads(old_pickle) is canonical


@pytest.mark.parametrize("vendor,old_name,new_name", VENDORS)
def test_vendor_instances_satisfy_runtime_protocol(
    vendor: str, old_name: str, new_name: str
) -> None:
    canonical = getattr(import_module(f"looplane.{vendor}_runner"), new_name)
    legacy = getattr(import_module(f"looplane.{vendor}_backend"), old_name)

    assert ExternalAgentBackend is ExternalAgentRunner
    assert isinstance(canonical(), ExternalAgentRunner)
    assert isinstance(legacy(), ExternalAgentRunner)


def test_structured_runner_preserves_subclass_contract() -> None:
    class CustomRunner(StructuredCliRunner):
        backend_name = "custom"

    assert isinstance(CustomRunner(), ExternalAgentRunner)
    assert issubclass(OpenCodeRunner, StructuredCliRunner)
    assert issubclass(PiRunner, StructuredCliRunner)
    assert issubclass(OmpRunner, PiRunner)
    assert OmpRunner.run is StructuredCliRunner.run


@pytest.mark.parametrize("vendor,old_name,new_name", VENDORS)
@pytest.mark.parametrize("legacy_import", [False, True])
async def test_legacy_command_monkeypatch_reaches_both_imports(
    vendor: str,
    old_name: str,
    new_name: str,
    legacy_import: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = f"{vendor}_backend" if legacy_import else f"{vendor}_runner"
    runner_class = getattr(
        import_module(f"looplane.{module}"), old_name if legacy_import else new_name
    )
    implementation = (
        vendor + "_backend" if vendor in {"codex", "claude"} else "external_cli_base"
    )
    calls: list[tuple[str, ...]] = []

    class CommandIntercepted(Exception):
        pass

    def intercept(argv: tuple[str, ...], **kwargs: object) -> None:
        calls.append(argv)
        assert kwargs["cwd"] == tmp_path
        raise CommandIntercepted

    monkeypatch.setattr(f"looplane.{implementation}.run_bounded_command", intercept)
    runner = runner_class(executable=sys.executable)
    with pytest.raises(CommandIntercepted):
        await runner.run(
            ExternalAgentTask(task_id="compatibility", instruction="inspect"),
            working_directory=tmp_path,
        )
    assert len(calls) == 1
