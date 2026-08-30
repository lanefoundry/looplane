from __future__ import annotations

import json
import sys

import pytest

from looplane.hooks import (
    HookError,
    HookEventName,
    HookRunner,
    load_project_hook_config,
    load_project_hook_runner,
)


def test_project_hook_config_loads_strict_exact_argv(tmp_path) -> None:
    hooks_dir = tmp_path / ".looplane"
    hooks_dir.mkdir()
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "pre_tool_use": [
                    {
                        "command": [sys.executable, "hook.py"],
                        "timeout_seconds": 2,
                        "tools": ["read_file"],
                    }
                ],
                "pre_compact": [{"command": [sys.executable, "compact.py"]}],
                "post_compact": [{"command": [sys.executable, "compact-done.py"]}],
            }
        ),
        encoding="utf-8",
    )

    config = load_project_hook_config(tmp_path)

    assert len(config.pre_tool_use) == 1
    assert config.pre_tool_use[0].command == (sys.executable, "hook.py")
    assert config.pre_tool_use[0].matches_tool("read_file") is True
    assert config.pre_tool_use[0].matches_tool("run_check") is False
    assert config.pre_compact[0].command == (sys.executable, "compact.py")
    assert config.post_compact[0].command == (sys.executable, "compact-done.py")


def test_project_hook_runner_is_disabled_without_explicit_env(tmp_path, monkeypatch) -> None:
    hooks_dir = tmp_path / ".looplane"
    hooks_dir.mkdir()
    (hooks_dir / "hooks.json").write_text(
        '{"pre_tool_use":[{"command":["false"]}]}',
        encoding="utf-8",
    )
    monkeypatch.delenv("LOOPLANE_ENABLE_PROJECT_HOOKS", raising=False)

    assert load_project_hook_runner(tmp_path).enabled is False

    monkeypatch.setenv("LOOPLANE_ENABLE_PROJECT_HOOKS", "1")
    assert load_project_hook_runner(tmp_path).enabled is True


def test_hook_runner_denies_from_json_output_and_ignores_allow(tmp_path) -> None:
    hook = tmp_path / "hook.py"
    hook.write_text(
        """
from __future__ import annotations

import json
import sys

payload = json.loads(sys.stdin.read())
if payload["payload"]["tool_call"]["name"] == "read_file":
    print(json.dumps({"decision": "deny", "reason": "blocked by hook"}))
else:
    print(json.dumps({"decision": "allow"}))
""".lstrip(),
        encoding="utf-8",
    )
    config = load_project_hook_config(tmp_path)
    assert config.pre_tool_use == ()
    (tmp_path / ".looplane").mkdir()
    (tmp_path / ".looplane" / "hooks.json").write_text(
        json.dumps({"pre_tool_use": [{"command": [sys.executable, str(hook)]}]}),
        encoding="utf-8",
    )
    runner = HookRunner(load_project_hook_config(tmp_path), cwd=tmp_path)

    denied = runner.run(
        HookEventName.PRE_TOOL_USE,
        {"tool_call": {"name": "read_file", "arguments": {"path": "src/app.py"}}},
    )
    allowed = runner.run(
        HookEventName.PRE_TOOL_USE,
        {"tool_call": {"name": "git_diff", "arguments": {}}},
    )

    assert denied is not None
    assert denied.decision == "deny"
    assert denied.reason == "blocked by hook"
    assert allowed is None


def test_hook_runner_fails_closed_on_nonzero_and_rejects_invalid_output(tmp_path) -> None:
    failing = tmp_path / "fail.py"
    failing.write_text("import sys\nprint('nope', file=sys.stderr)\nsys.exit(7)\n")
    invalid = tmp_path / "invalid.py"
    invalid.write_text("print('not json')\n")
    (tmp_path / ".looplane").mkdir()
    config_path = tmp_path / ".looplane" / "hooks.json"

    config_path.write_text(
        json.dumps({"pre_tool_use": [{"command": [sys.executable, str(failing)]}]}),
        encoding="utf-8",
    )
    denied = HookRunner(load_project_hook_config(tmp_path), cwd=tmp_path).run(
        HookEventName.PRE_TOOL_USE,
        {"tool_call": {"name": "read_file"}},
    )
    assert denied is not None
    assert denied.decision == "deny"
    assert "hook exited 7" in denied.reason

    config_path.write_text(
        json.dumps({"pre_tool_use": [{"command": [sys.executable, str(invalid)]}]}),
        encoding="utf-8",
    )
    with pytest.raises(HookError, match="valid JSON"):
        HookRunner(load_project_hook_config(tmp_path), cwd=tmp_path).run(
            HookEventName.PRE_TOOL_USE,
            {"tool_call": {"name": "read_file"}},
        )
