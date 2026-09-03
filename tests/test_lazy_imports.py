"""Regression tests proving lazy imports preserve every CLI route.

Each test runs a subprocess that imports or invokes a CLI route and asserts that
heavy, route-specific modules are NOT loaded. This catches accidental eager
imports that would regress startup performance.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HEAVY_MODULES_FOR_HELP = {
    "openai",
    "anthropic",
    "google.generativeai",
    "httpx",
    "uvicorn",
    "textual",
    "looplane.tui",
    "looplane.loop",
    "looplane.models",
    "looplane.gateway",
    "looplane.codex_oauth",
    "looplane.codex_backend",
    "looplane.claude_backend",
    "looplane.external_runner",
    "looplane.conversation_controller",
    "looplane.conversation",
    "looplane.conversation_websocket",
}

HEAVY_MODULES_FOR_CONFIG = {
    "openai",
    "anthropic",
    "google.generativeai",
    "httpx",
    "uvicorn",
    "textual",
    "looplane.tui",
    "looplane.loop",
    "looplane.models",
    "looplane.gateway",
    "looplane.codex_oauth",
    "looplane.codex_backend",
    "looplane.claude_backend",
    "looplane.external_runner",
    "looplane.conversation_controller",
    "looplane.conversation_websocket",
}

HEAVY_MODULES_FOR_CLI_IMPORT = {
    "openai",
    "anthropic",
    "google.generativeai",
    "httpx",
    "uvicorn",
    "textual",
    "looplane.tui",
    "looplane.loop",
    "looplane.models",
    "looplane.gateway",
    "looplane.codex_oauth",
    "looplane.codex_backend",
    "looplane.claude_backend",
    "looplane.external_runner",
    "looplane.conversation_controller",
    "looplane.conversation_websocket",
}


def _imported_modules(code: str) -> set[str]:
    """Run *code* in a fresh subprocess and return the set of loaded module names."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"{code}; import sys, json; print(json.dumps(sorted(sys.modules.keys())))",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": "",
            "HOME": "/tmp",
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src"),
        },
    )
    assert result.returncode == 0, f"subprocess failed:\n{result.stderr}"
    return set(json.loads(result.stdout.strip().splitlines()[-1]))


def test_import_cli_module_does_not_load_heavy_deps() -> None:
    """Importing looplane.cli must not eagerly load provider SDKs or TUI."""
    loaded = _imported_modules("import looplane.cli")
    leaked = HEAVY_MODULES_FOR_CLI_IMPORT & loaded
    assert not leaked, f"import looplane.cli eagerly loaded: {sorted(leaked)}"


def test_help_route_does_not_load_heavy_deps() -> None:
    """``looplane --help`` must not load provider SDKs, Textual, or gateway."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, json; "
                "from looplane.cli import app; "
                "from typer.testing import CliRunner; "
                "CliRunner().invoke(app, ['--help']); "
                "print(json.dumps(sorted(sys.modules.keys())))"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": "",
            "HOME": "/tmp",
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src"),
        },
    )
    assert result.returncode == 0, f"subprocess failed:\n{result.stderr}"
    loaded = set(json.loads(result.stdout.strip().splitlines()[-1]))
    leaked = HEAVY_MODULES_FOR_HELP & loaded
    assert not leaked, f"looplane --help eagerly loaded: {sorted(leaked)}"


def test_config_route_does_not_load_heavy_deps() -> None:
    """``looplane config`` must not load provider SDKs, Textual, or gateway."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, json, os; "
                "os.environ['XDG_CONFIG_HOME'] = '/tmp/looplane-test-cfg'; "
                "from looplane.cli import app; "
                "from typer.testing import CliRunner; "
                "CliRunner().invoke(app, ['config']); "
                "print(json.dumps(sorted(sys.modules.keys())))"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": "",
            "HOME": "/tmp",
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src"),
        },
    )
    assert result.returncode == 0, f"subprocess failed:\n{result.stderr}"
    loaded = set(json.loads(result.stdout.strip().splitlines()[-1]))
    leaked = HEAVY_MODULES_FOR_CONFIG & loaded
    assert not leaked, f"looplane config eagerly loaded: {sorted(leaked)}"
