"""Keep extracted feature packages independent of legacy facade modules."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "looplane"
FEATURES = ("looplane.terminal", "looplane.runtimes", "looplane.tooling")
FACADES = {
    "looplane.tui",
    "looplane.cli",
    "looplane.loop",
    "looplane.tools",
    "looplane.codex_app_server",
    "looplane.backends",
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
        forbidden = set(FACADES) | {"looplane.commands"}
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
