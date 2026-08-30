from __future__ import annotations

import json
import sys

import pytest

from rivumi.hooks import load_project_hook_config
from rivumi.plugins import PluginError, install_project_plugin_manifest, load_project_plugins
from rivumi.skills import load_project_skills


def test_project_plugin_manifest_packages_skills_and_hooks(tmp_path) -> None:
    plugins = tmp_path / ".rivumi" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "review.md").write_text(
        "---\nname: review\ndescription: Packaged review\n---\nCheck packaged risks.",
        encoding="utf-8",
    )
    (plugins / "local.json").write_text(
        json.dumps(
            {
                "name": "local",
                "description": "Local plugin package",
                "discovery": {
                    "keywords": ["review", "risk"],
                    "homepage": "https://example.com/rivumi/review",
                    "repository": "https://example.com/rivumi/review.git",
                    "license": "MIT",
                    "author": "Rivumi",
                },
                "skills": [{"path": "review.md"}],
                "hooks": {
                    "pre_tool_use": [
                        {
                            "command": [sys.executable, "hook.py"],
                            "tools": ["read_file"],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_project_plugins(tmp_path)
    skills = load_project_skills(tmp_path)
    hooks = load_project_hook_config(tmp_path)

    assert loaded[0].name == "local"
    assert loaded[0].discovery.keywords == ("review", "risk")
    assert loaded[0].discovery.homepage == "https://example.com/rivumi/review"
    assert loaded[0].discovery.repository == "https://example.com/rivumi/review.git"
    assert loaded[0].discovery.license == "MIT"
    assert loaded[0].discovery.author == "Rivumi"
    assert skills[0].name == "local.review"
    assert "Check packaged risks." in skills[0].body
    assert hooks.pre_tool_use[0].command == (sys.executable, "hook.py")
    assert hooks.pre_tool_use[0].matches_tool("read_file") is True


def test_project_plugin_rejects_skill_path_escape(tmp_path) -> None:
    plugins = tmp_path / ".rivumi" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "bad.json").write_text(
        json.dumps({"name": "bad", "skills": [{"path": "../outside.md"}]}),
        encoding="utf-8",
    )

    with pytest.raises(PluginError, match="manifest-relative"):
        load_project_plugins(tmp_path)


def test_project_plugin_rejects_unsafe_discovery_metadata(tmp_path) -> None:
    plugins = tmp_path / ".rivumi" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "bad.json").write_text(
        json.dumps(
            {
                "name": "bad",
                "discovery": {
                    "keywords": ["review", "review"],
                    "homepage": "file:///tmp/plugin",
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PluginError, match="project plugin manifest is invalid"):
        load_project_plugins(tmp_path)


def test_install_project_plugin_manifest_copies_local_package(tmp_path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "review.md").write_text(
        "---\nname: review\n---\nInstalled skill body.",
        encoding="utf-8",
    )
    manifest = package / "plugin.json"
    manifest.write_text(
        json.dumps(
            {
                "name": "review-pack",
                "description": "Review helpers",
                "discovery": {
                    "keywords": ["review"],
                    "homepage": "https://example.com/review-pack",
                    "license": "Apache-2.0",
                },
                "skills": [{"path": "review.md"}],
            }
        ),
        encoding="utf-8",
    )
    project = tmp_path / "repo"
    project.mkdir()

    plugin = install_project_plugin_manifest(manifest, project_root=project)

    assert plugin.name == "review-pack"
    assert plugin.discovery.homepage == "https://example.com/review-pack"
    assert (project / ".rivumi" / "plugins" / "review-pack.json").is_file()
    manifest_payload = json.loads(
        (project / ".rivumi" / "plugins" / "review-pack.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest_payload["discovery"]["keywords"] == ["review"]
    assert manifest_payload["discovery"]["license"] == "Apache-2.0"
    assert (project / ".rivumi" / "plugins" / "review.md").read_text(
        encoding="utf-8"
    ).endswith("Installed skill body.")
    loaded_skills = load_project_skills(project)
    assert loaded_skills[0].name == "review-pack.review"


def test_install_project_plugin_manifest_rejects_duplicate_without_overwrite(
    tmp_path,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "review.md").write_text("---\nname: review\n---\nBody.", encoding="utf-8")
    manifest = package / "plugin.json"
    manifest.write_text(
        json.dumps({"name": "review-pack", "skills": [{"path": "review.md"}]}),
        encoding="utf-8",
    )
    project = tmp_path / "repo"
    project.mkdir()

    install_project_plugin_manifest(manifest, project_root=project)
    with pytest.raises(PluginError, match="already installed"):
        install_project_plugin_manifest(manifest, project_root=project)
