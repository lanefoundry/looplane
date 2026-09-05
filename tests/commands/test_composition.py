"""Behavioral and dependency contracts for the CLI composition slice."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from looplane import cli
from looplane.cli_config import CliConfig
from looplane.commands import bootstrap
from looplane.commands.ports import CommandServices, RuntimePorts
from looplane.commands.session_index import SessionIndex
from looplane.startup_trace import _STARTUP


@pytest.fixture
def services() -> CommandServices:
    def unexpected(*args, **kwargs):
        raise AssertionError("unrequested dependency construction")

    async def start_controller(controller):
        raise AssertionError("unrequested warmup")

    return CommandServices(
        startup=_STARTUP,
        model_factory=unexpected,
        stdin_is_tty=lambda: False,
        supports_tui=lambda: False,
        terminal_size=lambda: None,
        interactive_setup=unexpected,
        discover_models=lambda: (),
        credential_path=unexpected,
        fetch_models=lambda: (),
        runtime=RuntimePorts(
            native_runtime=unexpected,
            terminal_app=unexpected,
            terminal_context_id=unexpected,
            start_controller=start_controller,
        ),
    )


def test_commands_import_without_loading_facades_or_heavy_dependencies(tmp_path: Path) -> None:
    code = """
import importlib, json, pkgutil, sys
import looplane.commands
for module in pkgutil.iter_modules(looplane.commands.__path__):
    importlib.import_module(module.name if module.name.startswith('looplane.') else
                            'looplane.commands.' + module.name)
print(json.dumps(sorted(sys.modules)))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "HOME": str(tmp_path), "LOOPLANE_CONFIG": str(tmp_path / "none")},
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    loaded = set(json.loads(result.stdout))
    forbidden = {
        "looplane.cli",
        "looplane.loop",
        "looplane.tui",
        "looplane.tools",
        "looplane.backends",
        "looplane.codex_app_server",
        "looplane.models",
        "looplane.conversation_controller",
        "openai",
        "httpx",
        "textual",
        "uvicorn",
    }
    assert not (loaded & forbidden)


def test_commands_have_no_imports_of_compatibility_facades() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "looplane" / "commands"
    forbidden = {"cli", "tui", "loop", "tools", "backends", "codex_app_server"}
    violations = []
    paths = tuple(root.glob("*.py"))
    assert paths
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            imports = []
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                prefix = node.module or ""
                if node.level:
                    package = ["looplane", "commands"][: 3 - node.level]
                    prefix = ".".join([*package, *([prefix] if prefix else [])])
                imports = [prefix, *(f"{prefix}.{alias.name}" for alias in node.names)]
            for module in imports:
                if any(module == f"looplane.{name}" for name in forbidden):
                    violations.append(f"{path.name}:{node.lineno}: {module}")
    assert not violations, "\n".join(violations)


def test_legacy_model_patch_is_supplied_as_an_explicit_factory(monkeypatch) -> None:
    calls = []
    model = object()

    def make_model(**kwargs):
        calls.append(kwargs)
        return model

    monkeypatch.setattr(cli, "_model_from_env", make_model)
    selection = bootstrap.ModelSelection(
        services=cli._command_services(),
        fallback_specs=("ollama/test",),
        auto_review=False,
        allow_custom_provider_endpoint=False,
        experimental_subscription=False,
    )
    assert calls == []
    assert selection.build_fallback_models() == (model,)
    assert selection.build_fallback_models() == (model,)
    assert len(calls) == 1
    assert calls[0]["provider"] == "ollama"
    assert calls[0]["tool_calling"] is True
    assert selection.build_review_model("ollama") is None


def test_model_caches_are_owned_by_each_command(services) -> None:
    created = []

    def make_model(**kwargs):
        result = object()
        created.append((kwargs, result))
        return result

    configured = replace(services, model_factory=make_model)

    def selection():
        return bootstrap.ModelSelection(configured, ("ollama/test",), True, False, False)

    first, second = selection(), selection()
    first_fallback = first.build_fallback_models()
    first_review = first.build_review_model("openai-compatible")
    assert first.build_review_model("openai-compatible") is first_review
    assert second.build_fallback_models() != first_fallback
    assert len(created) == 3
    assert created[1][0]["tool_calling"] is False


def test_invalid_fallback_does_not_construct_a_provider(services) -> None:
    selection = bootstrap.ModelSelection(services, ("unqualified",), False, False, False)
    with pytest.raises(typer.BadParameter, match="provider/model or @role"):
        selection.build_fallback_models()


def test_native_factory_preserves_constructor_arguments(services, tmp_path) -> None:
    captured = {}
    runner = object()

    def construct(*args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        return runner

    services = replace(
        services, runtime=replace(services.runtime, native_runtime=lambda: (construct, ValueError))
    )
    task, model = object(), object()
    assert (
        bootstrap.build_native_runner(
            services,
            task,
            model,
            tmp_path,
            allow_unsafe_local_exec=False,
            allow_direct_repo_edit=False,
            sandbox_checks=True,
            continuation=True,
            run_id="persisted",
        )
        is runner
    )
    assert captured["args"] == (task, model, tmp_path)
    assert captured["kwargs"] == {
        "allow_unsafe_local_exec": False,
        "allow_direct_repo_edit": False,
        "sandbox_checks": True,
        "continuation": True,
        "run_id": "persisted",
    }


async def test_warmup_and_turn_share_owned_native_controller(services, tmp_path) -> None:
    warmed = []

    async def warm(controller):
        warmed.append(controller)

    services = replace(services, runtime=replace(services.runtime, start_controller=warm))
    config = CliConfig(runtime="codex-cli", runtime_model="test")
    factory = bootstrap.ChatRuntimeFactory(
        services=services,
        repository=tmp_path,
        check=None,
        run_root=tmp_path / "runs",
        unsafe_local_exec=False,
        edit_real_repo=False,
        permission_guard=object(),
        sandbox_checks=True,
        initial_config=config,
        allow_custom_provider_endpoint=False,
        experimental_subscription=False,
        model_selection=bootstrap.ModelSelection(services, (), False, False, False),
        guarded=lambda policy: policy,
    )
    await factory.warmup("context-one")
    request = SimpleNamespace(
        runtime="codex-cli",
        repository=tmp_path,
        model="test",
        context_id="context-one",
        instruction="explain",
    )
    turn, controller = factory.make_runner(request, None, None)
    assert warmed == [controller]
    assert turn.controller is controller
    assert len(factory.native_controllers) == 1
    request.context_id = "context-two"
    _, other = factory.make_runner(request, None, None)
    assert other is not controller
    assert len(factory.native_controllers) == 2
    await controller.aclose()
    await other.aclose()


async def test_run_resources_close_when_runner_fails() -> None:
    closed = []

    async def close():
        closed.append(True)

    async def fail():
        raise ValueError("run failed")

    with pytest.raises(ValueError, match="run failed"):
        await bootstrap._run_and_close(SimpleNamespace(run=fail), SimpleNamespace(aclose=close))
    assert closed == [True]


def test_session_index_bounds_reads_and_rejects_ambiguous_or_unsafe_paths(tmp_path) -> None:
    index = SessionIndex(tmp_path, query="needle", max_json_bytes=32)
    first = tmp_path / "alpha-one"
    second = tmp_path / "alpha-two"
    first.mkdir()
    second.mkdir()
    (tmp_path / "linked").symlink_to(first, target_is_directory=True)
    assert index.resolve_run_dir("alpha") is None
    assert index.resolve_run_dir("alpha-one") == first
    assert index.resolve_run_dir("../alpha-one") is None
    assert index.resolve_run_dir("linked") is None
    payload = first / "result.json"
    payload.write_text('{"summary":"needle"}')
    assert index.read_json(payload) == {"summary": "needle"}
    payload.write_text('"' + "x" * 33 + '"')
    assert index.read_json(payload) is None
    parts = []
    bounded = replace(index, max_event_search_parts=2, max_event_search_part_chars=3)
    bounded.bounded_event_search_parts(["abcdef", {"part": "ghijkl"}, "ignored"], parts)
    assert parts == ["abc", "ghi"]


def test_command_dependencies_are_acyclic() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "looplane" / "commands"
    graph = {}
    for path in root.glob("*.py"):
        dependencies = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module == "looplane.commands":
                dependencies.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "looplane.commands."
            ):
                dependencies.add(node.module.split(".")[2])
        graph[path.stem] = dependencies
    for start in graph:
        pending = list(graph[start])
        visited = set()
        while pending:
            current = pending.pop()
            assert current != start, f"command import cycle involving {start}"
            if current not in visited:
                visited.add(current)
                pending.extend(graph.get(current, ()))
