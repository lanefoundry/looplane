"""Repository-local plugin package manifests."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import Field, field_validator

from looplane.contracts import ContractModel

PROJECT_PLUGINS_DIR = Path(".looplane") / "plugins"
MAX_PLUGIN_MANIFEST_BYTES = 64 * 1024
MAX_PLUGINS = 16
MAX_PLUGIN_SKILLS = 16
MAX_PLUGIN_KEYWORDS = 16
_SAFE_PLUGIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SAFE_PLUGIN_KEYWORD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class PluginError(ValueError):
    """Raised when a project plugin package is unsafe or malformed."""


class PluginSkillRef(ContractModel):
    path: str = Field(min_length=1, max_length=256)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        value = value.strip().replace("\\", "/")
        if not value or "\x00" in value or value.startswith(("/", "../")) or "/../" in value:
            raise ValueError("plugin skill path must be manifest-relative")
        if not value.endswith(".md"):
            raise ValueError("plugin skill path must point to markdown")
        return value


class PluginDiscoveryMetadata(ContractModel):
    keywords: tuple[str, ...] = Field(default=(), max_length=MAX_PLUGIN_KEYWORDS)
    homepage: str | None = Field(default=None, max_length=2048)
    repository: str | None = Field(default=None, max_length=2048)
    license: str | None = Field(default=None, max_length=128)
    author: str | None = Field(default=None, max_length=256)

    @field_validator("keywords")
    @classmethod
    def validate_keywords(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for keyword in value:
            keyword = keyword.strip()
            if not _SAFE_PLUGIN_KEYWORD.fullmatch(keyword):
                raise ValueError("plugin discovery keyword is invalid")
            normalized.append(keyword)
        if len(set(normalized)) != len(normalized):
            raise ValueError("plugin discovery keywords cannot contain duplicates")
        return tuple(normalized)

    @field_validator("homepage", "repository")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        parsed = urlparse(value)
        if (
            not value
            or "\x00" in value
            or parsed.scheme not in {"http", "https"}
            or not parsed.netloc
        ):
            raise ValueError("plugin discovery URL must be http(s)")
        return value

    @field_validator("license", "author")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("plugin discovery text cannot be blank or contain NUL")
        return value


class ProjectPlugin(ContractModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=400)
    skills: tuple[PluginSkillRef, ...] = Field(default=(), max_length=MAX_PLUGIN_SKILLS)
    hooks: dict[str, Any] = Field(default_factory=dict)
    discovery: PluginDiscoveryMetadata = Field(default_factory=PluginDiscoveryMetadata)
    source: str = Field(min_length=1, max_length=512)
    directory: Path

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not _SAFE_PLUGIN_NAME.fullmatch(value):
            raise ValueError("invalid plugin name")
        return value


def load_project_plugins(project_root: Path) -> tuple[ProjectPlugin, ...]:
    """Load `.looplane/plugins/*.json` manifests without executing plugin code."""

    directory = project_root / PROJECT_PLUGINS_DIR
    if not directory.exists():
        return ()
    if directory.is_symlink() or not directory.is_dir():
        raise PluginError("project plugins path must be a directory")
    plugins: list[ProjectPlugin] = []
    for path in sorted(directory.glob("*.json")):
        if len(plugins) >= MAX_PLUGINS:
            break
        if path.is_symlink() or not path.is_file():
            raise PluginError(f"project plugin manifest must be a regular file: {path.name}")
        if path.stat().st_size > MAX_PLUGIN_MANIFEST_BYTES:
            raise PluginError(f"project plugin manifest exceeds size limit: {path.name}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PluginError(f"project plugin manifest must be UTF-8 JSON: {path.name}") from exc
        if not isinstance(value, dict):
            raise PluginError(f"project plugin manifest must be an object: {path.name}")
        value.setdefault("name", path.stem)
        value["source"] = path.relative_to(project_root).as_posix()
        value["directory"] = path.parent
        try:
            plugin = ProjectPlugin.model_validate(value)
        except ValueError as exc:
            raise PluginError(f"project plugin manifest is invalid: {path.name}: {exc}") from exc
        _validate_plugin_skill_paths(plugin, project_root)
        plugins.append(plugin)
    return tuple(plugins)


def install_project_plugin_manifest(
    manifest_path: Path,
    *,
    project_root: Path,
    name: str | None = None,
    overwrite: bool = False,
) -> ProjectPlugin:
    """Install one local plugin manifest plus referenced skills into a project."""

    source = manifest_path.resolve(strict=False)
    if source.is_symlink() or not source.is_file():
        raise PluginError("plugin manifest must be a regular file")
    if source.stat().st_size > MAX_PLUGIN_MANIFEST_BYTES:
        raise PluginError("plugin manifest exceeds size limit")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginError("plugin manifest must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise PluginError("plugin manifest must be an object")

    install_name = name or value.get("name") or source.stem
    value = {**value, "name": install_name}
    package_root = source.parent
    preview = _plugin_from_manifest_value(
        value,
        source=source.name,
        directory=package_root,
        error_label=source.name,
    )
    _validate_plugin_skill_paths(preview, package_root)

    destination = project_root / PROJECT_PLUGINS_DIR
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or not destination.is_dir():
        raise PluginError("project plugins path must be a directory")
    manifest_destination = destination / f"{preview.name}.json"
    if manifest_destination.exists() and not overwrite:
        raise PluginError(f"plugin already installed: {preview.name}")

    for skill in preview.skills:
        source_skill = package_root / skill.path
        target_skill = destination / skill.path
        if target_skill.exists() and not overwrite:
            raise PluginError(f"plugin skill already exists: {skill.path}")
        target_skill.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_skill, target_skill)

    manifest_payload = dict(value)
    manifest_payload.pop("source", None)
    manifest_payload.pop("directory", None)
    manifest_destination.write_text(
        json.dumps(manifest_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    installed = load_project_plugins(project_root)
    for plugin in installed:
        if plugin.name == preview.name:
            return plugin
    raise PluginError(f"installed plugin could not be loaded: {preview.name}")


def _plugin_from_manifest_value(
    value: dict[str, Any],
    *,
    source: str,
    directory: Path,
    error_label: str,
) -> ProjectPlugin:
    value = dict(value)
    value.setdefault("name", Path(source).stem)
    value["source"] = source
    value["directory"] = directory
    try:
        return ProjectPlugin.model_validate(value)
    except ValueError as exc:
        raise PluginError(f"project plugin manifest is invalid: {error_label}: {exc}") from exc


def _validate_plugin_skill_paths(plugin: ProjectPlugin, project_root: Path) -> None:
    root = project_root.resolve(strict=True)
    for skill in plugin.skills:
        path = (plugin.directory / skill.path).resolve(strict=False)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise PluginError("plugin skill path must stay inside repository") from exc
        if path.is_symlink():
            raise PluginError("plugin skill path cannot be a symlink")
        if not path.is_file():
            raise PluginError(f"plugin skill file is missing: {skill.path}")
