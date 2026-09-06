"""Tests for project_config — looplane.toml parsing and merging."""

from __future__ import annotations

from pathlib import Path

from looplane.project_config import (
    ProjectConfig,
    _build_config,
    _deep_merge,
    load_project_config,
)


class TestDeepMerge:
    def test_simple_override(self):
        base = {"a": 1, "b": 2}
        _deep_merge(base, {"b": 3, "c": 4})
        assert base == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self):
        base = {"x": {"a": 1, "b": 2}}
        _deep_merge(base, {"x": {"b": 3, "c": 4}})
        assert base == {"x": {"a": 1, "b": 3, "c": 4}}


class TestBuildConfig:
    def test_empty_config(self):
        cfg = _build_config({})
        assert cfg.instructions_path is None
        assert cfg.lsp_servers == ()
        assert cfg.verification == ()

    def test_project_instructions(self):
        cfg = _build_config({"project": {"instructions": "docs/agent.md"}})
        assert cfg.instructions_path == "docs/agent.md"

    def test_lsp_servers(self):
        cfg = _build_config(
            {
                "lsp": {
                    "python": {"command": ["pyright-langserver", "--stdio"]},
                    "typescript": {"command": "typescript-language-server"},
                }
            }
        )
        assert len(cfg.lsp_servers) == 2
        assert cfg.lsp_servers[0].name == "python"
        assert cfg.lsp_servers[0].command == ("pyright-langserver", "--stdio")

    def test_verification_tools(self):
        cfg = _build_config(
            {
                "tools": {
                    "verification": {
                        "lint": "ruff check {path}",
                        "test": "pytest {path} -x -q",
                    }
                }
            }
        )
        assert len(cfg.verification) == 2

    def test_web_config(self):
        cfg = _build_config(
            {
                "web": {
                    "allowed_domains": ["docs.python.org"],
                    "blocked_domains": ["*.internal.corp"],
                }
            }
        )
        assert cfg.web.allowed_domains == ("docs.python.org",)
        assert cfg.web.blocked_domains == ("*.internal.corp",)

    def test_permissions_config(self):
        cfg = _build_config(
            {
                "permissions": {
                    "auto_approve_read": True,
                    "auto_approve_shell": ["pytest", "ruff"],
                }
            }
        )
        assert cfg.permissions.auto_approve_shell == ("pytest", "ruff")


class TestLoadProjectConfig:
    def test_no_config_file(self, tmp_path: Path):
        cfg = load_project_config(tmp_path)
        assert isinstance(cfg, ProjectConfig)
        assert cfg.instructions_path is None

    def test_loads_workspace_config(self, tmp_path: Path):
        toml_content = b"""
[project]
instructions = "docs/agent.md"

[lsp.python]
command = ["pyright-langserver", "--stdio"]

[tools.verification]
lint = "ruff check ."
"""
        (tmp_path / "looplane.toml").write_bytes(toml_content)
        cfg = load_project_config(tmp_path)
        assert cfg.instructions_path == "docs/agent.md"
        assert len(cfg.lsp_servers) == 1
        assert cfg.lsp_servers[0].name == "python"
        assert len(cfg.verification) == 1
