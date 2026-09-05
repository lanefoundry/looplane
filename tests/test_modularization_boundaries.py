"""Keep extracted feature packages independent of legacy facade modules."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "looplane"
FEATURES = (
    "looplane.terminal", "looplane.runtimes", "looplane.tooling", "looplane.commands",
    "looplane.agent", "looplane.execution", "looplane.sandbox", "looplane.workspace",
)
FACADES = {
    "looplane.tui",
    "looplane.cli",
    "looplane.loop",
    "looplane.tools",
    "looplane.codex_app_server",
    "looplane.backends",
    "looplane.runtime",
}


def _within(module: str, prefixes: tuple[str, ...] | set[str]) -> bool:
    return any(module == prefix or module.startswith(prefix + ".") for prefix in prefixes)


def _imports(path: Path) -> set[str]:
    module = ".".join(("looplane", *path.relative_to(ROOT).with_suffix("").parts))
    package = module.removesuffix(".__init__") if path.name == "__init__.py" else (
        module.rsplit(".", 1)[0]
    )
    result: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = node.module or ""
            if node.level:
                parts = package.split(".")
                prefix = ".".join(
                    parts[: len(parts) - node.level + 1] + ([prefix] if prefix else [])
                )
            result.add(prefix)
            result.update(f"{prefix}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else ""
            )
            arg = node.args[0]
            if (
                name in {"import_module", "__import__"}
                and isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
            ):
                result.add(arg.value)
    return result


def _graph() -> dict[str, set[str]]:
    modules = {}
    for path in ROOT.rglob("*.py"):
        module = ".".join(("looplane", *path.relative_to(ROOT).with_suffix("").parts))
        modules[module.removesuffix(".__init__")] = _imports(path)
    return {
        module: {dependency for dependency in imports if dependency in modules}
        for module, imports in modules.items()
    }


def test_feature_packages_do_not_import_facades_or_higher_layers() -> None:
    violations = []
    for module, dependencies in _graph().items():
        if not _within(module, FEATURES):
            continue
        forbidden = set(FACADES)
        if not _within(module, ("looplane.commands",)):
            forbidden.add("looplane.commands")
        if module.startswith("looplane.runtimes."):
            forbidden.update({"looplane.terminal", "looplane.conversation_controller"})
        if module.startswith("looplane.tooling."):
            forbidden.update({"looplane.agent", "looplane.terminal", "looplane.runtimes"})
        violations.extend(
            f"{module} -> {dependency}"
            for dependency in dependencies
            if _within(dependency, forbidden)
        )
    assert not violations, "\n".join(sorted(violations))


def test_domain_and_policy_do_not_depend_on_product_or_vendor_layers() -> None:
    graph = _graph()
    owners = (
        "looplane.contracts", "looplane.events", "looplane.approvals", "looplane.policy",
        "looplane.runtime_semantics", "looplane.conversation_runtime", "looplane.external_agents",
    )
    forbidden = ("looplane.commands", "looplane.terminal", "looplane.runtimes")
    violations = [
        f"{owner} -> {dependency}"
        for owner in owners
        for dependency in graph.get(owner, ())
        if _within(dependency, forbidden)
    ]
    assert not violations, "\n".join(sorted(violations))


def test_canonical_events_have_one_definition_owner() -> None:
    from looplane import conversation_runtime

    names = {
        name for name, value in vars(conversation_runtime).items()
        if isinstance(value, type) and value.__module__ == conversation_runtime.__name__
        and name.endswith("Event")
    }
    expected = {name: "conversation_runtime.py" for name in names}
    expected.update(
        {"ConversationRuntimeEvent": "conversation_runtime.py", "RunEvent": "events.py"}
    )
    found: dict[str, list[str]] = {name: [] for name in expected}
    for path in ROOT.rglob("*.py"):
        for node in ast.parse(path.read_text()).body:
            declared = []
            if isinstance(node, ast.ClassDef):
                declared = [node.name]
            elif isinstance(node, ast.Assign):
                declared = [target.id for target in node.targets if isinstance(target, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                declared = [node.target.id]
            for name in declared:
                if name in found:
                    found[name].append(path.relative_to(ROOT).as_posix())
    assert found == {name: [owner] for name, owner in expected.items()}


def test_production_import_graph_has_no_cycles() -> None:
    graph = _graph()
    reachable = {}
    for start in graph:
        pending = list(graph[start])
        seen = set()
        while pending:
            current = pending.pop()
            if current not in seen:
                seen.add(current)
                pending.extend(graph.get(current, ()))
        reachable[start] = seen
    cycles = {
        frozenset(other for other in reachable[start] if start in reachable.get(other, ()))
        for start in graph if start in reachable[start]
    }
    assert not cycles, cycles


def test_extracted_packages_do_not_participate_in_import_cycles() -> None:
    graph = _graph()
    for start in graph:
        if not _within(start, FEATURES):
            continue
        pending = list(graph[start])
        seen = set()
        while pending:
            current = pending.pop()
            assert current != start, f"Import cycle reaches extracted module {start}"
            if current not in seen:
                seen.add(current)
                pending.extend(graph.get(current, ()))
