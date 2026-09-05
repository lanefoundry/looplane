"""Plugins command services."""

from __future__ import annotations

import json
from pathlib import Path

import typer


def plugin_list(repo: Path | None = None, json_output: bool = False) -> None:
    """List installed repository-local plugin manifests."""

    from looplane.plugins import PluginError, load_project_plugins

    project_root = repo or Path.cwd()
    try:
        plugins = load_project_plugins(project_root)
    except PluginError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if json_output:
        typer.echo(
            json.dumps(
                [
                    {
                        "name": plugin.name,
                        "description": plugin.description,
                        "discovery": plugin.discovery.model_dump(mode="json"),
                        "source": plugin.source,
                        "skills": [skill.path for skill in plugin.skills],
                        "hook_events": sorted(plugin.hooks),
                    }
                    for plugin in plugins
                ],
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return
    if not plugins:
        typer.echo("No repository plugins installed.")
        return
    for plugin in plugins:
        skills = ", ".join(skill.path for skill in plugin.skills) or "-"
        hooks = ", ".join(sorted(plugin.hooks)) or "-"
        description = f" - {plugin.description}" if plugin.description else ""
        typer.echo(f"{plugin.name}{description}")
        typer.echo(f"  source: {plugin.source}")
        typer.echo(f"  skills: {skills}")
        typer.echo(f"  hooks: {hooks}")
        if plugin.discovery.keywords:
            typer.echo(f"  keywords: {', '.join(plugin.discovery.keywords)}")
        if plugin.discovery.homepage:
            typer.echo(f"  homepage: {plugin.discovery.homepage}")
        if plugin.discovery.repository:
            typer.echo(f"  repository: {plugin.discovery.repository}")
        if plugin.discovery.license:
            typer.echo(f"  license: {plugin.discovery.license}")
        if plugin.discovery.author:
            typer.echo(f"  author: {plugin.discovery.author}")


def plugin_install(
    manifest: Path,
    repo: Path | None = None,
    name: str | None = None,
    overwrite: bool = False,
    json_output: bool = False,
) -> None:
    """Install a local plugin manifest and referenced skills into `.looplane/plugins`."""

    from looplane.plugins import PluginError, install_project_plugin_manifest

    project_root = repo or Path.cwd()
    try:
        plugin = install_project_plugin_manifest(
            manifest,
            project_root=project_root,
            name=name,
            overwrite=overwrite,
        )
    except PluginError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    payload = {
        "name": plugin.name,
        "description": plugin.description,
        "discovery": plugin.discovery.model_dump(mode="json"),
        "source": plugin.source,
        "skills": [skill.path for skill in plugin.skills],
        "hook_events": sorted(plugin.hooks),
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    typer.echo(f"Installed plugin {plugin.name} at {plugin.source}.")
