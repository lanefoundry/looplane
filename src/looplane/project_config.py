"""Structured project configuration from looplane.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_CONFIG_FILENAME = "looplane.toml"
_USER_CONFIG_DIR = Path.home() / ".config" / "looplane"
_USER_CONFIG_FILE = _USER_CONFIG_DIR / "config.toml"


@dataclass
class LspServerConfig:
    name: str
    command: tuple[str, ...]


@dataclass
class VerificationToolConfig:
    name: str
    command: str


@dataclass
class WebConfig:
    allowed_domains: tuple[str, ...] = ()
    blocked_domains: tuple[str, ...] = ()


@dataclass
class BackgroundConfig:
    max_processes: int = 3
    auto_cleanup: bool = True


@dataclass
class PermissionsConfig:
    auto_approve_read: bool = True
    auto_approve_shell: tuple[str, ...] = ()


@dataclass
class ProjectConfig:
    instructions_path: str | None = None
    instructions_text: str | None = None
    lsp_servers: tuple[LspServerConfig, ...] = ()
    verification: tuple[VerificationToolConfig, ...] = ()
    web: WebConfig = field(default_factory=WebConfig)
    background: BackgroundConfig = field(default_factory=BackgroundConfig)
    permissions: PermissionsConfig = field(default_factory=PermissionsConfig)
    raw: dict[str, Any] = field(default_factory=dict)


def load_project_config(workspace: Path) -> ProjectConfig:
    workspace_config = workspace / _CONFIG_FILENAME
    user_config = _USER_CONFIG_FILE

    merged: dict[str, Any] = {}
    if user_config.is_file():
        merged.update(_parse_toml(user_config))
    if workspace_config.is_file():
        _deep_merge(merged, _parse_toml(workspace_config))

    if not merged:
        return ProjectConfig()

    return _build_config(merged)


def _parse_toml(path: Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _build_config(raw: dict[str, Any]) -> ProjectConfig:
    project = raw.get("project", {})
    instructions_path = project.get("instructions")

    lsp_section = raw.get("lsp", {})
    lsp_servers = []
    for name, cfg in lsp_section.items():
        if isinstance(cfg, dict) and "command" in cfg:
            cmd = cfg["command"]
            if isinstance(cmd, list):
                lsp_servers.append(LspServerConfig(name=name, command=tuple(cmd)))
            elif isinstance(cmd, str):
                lsp_servers.append(LspServerConfig(name=name, command=(cmd,)))

    tools = raw.get("tools", {})
    verification_section = tools.get("verification", {})
    verifications = []
    for name, cmd in verification_section.items():
        if isinstance(cmd, str):
            verifications.append(VerificationToolConfig(name=name, command=cmd))

    web_section = raw.get("web", {})
    web = WebConfig(
        allowed_domains=tuple(web_section.get("allowed_domains", ())),
        blocked_domains=tuple(web_section.get("blocked_domains", ())),
    )

    bg_section = tools.get("background", {})
    background = BackgroundConfig(
        max_processes=bg_section.get("max_processes", 3),
        auto_cleanup=bg_section.get("auto_cleanup", True),
    )

    perm_section = raw.get("permissions", {})
    permissions = PermissionsConfig(
        auto_approve_read=perm_section.get("auto_approve_read", True),
        auto_approve_shell=tuple(perm_section.get("auto_approve_shell", ())),
    )

    instructions_text = None
    if instructions_path:
        instructions_text = instructions_path

    return ProjectConfig(
        instructions_path=instructions_path,
        instructions_text=instructions_text,
        lsp_servers=tuple(lsp_servers),
        verification=tuple(verifications),
        web=web,
        background=background,
        permissions=permissions,
        raw=raw,
    )
