"""Policy command services."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import typer

from looplane.cli_config import (
    CliConfig,
    default_cli_config_path,
    load_cli_config,
)
from looplane.commands.ports import CommandServices


def _dangerous_acceptance_path() -> Path:
    state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_root / "looplane" / "dangerous-mode-accepted"


def _enter_dangerous_mode(dangerous: bool, *, services: CommandServices):
    """Resolve the effective approval mode, gating --dangerous entry."""

    from looplane.permissions import ApprovalMode, DangerousModeError, plan_dangerous_mode_entry

    if not dangerous:
        return ApprovalMode.DEFAULT
    acceptance_path = _dangerous_acceptance_path()
    try:
        outcome = plan_dangerous_mode_entry(
            accepted=acceptance_path.exists(),
            env_acknowledged=os.environ.get("LOOPLANE_ACCEPT_DANGEROUS_MODE") == "1",
            is_tty=services.stdin_is_tty(),
            is_root=hasattr(os, "getuid") and os.getuid() == 0,
            sandboxed=os.environ.get("LOOPLANE_SANDBOX") == "1",
        )
    except DangerousModeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if outcome == "prompt":
        typer.confirm(
            "You are enabling --dangerous: read/modify actions run without approval "
            "prompts. Forbidden-operation rules still apply. Continue?",
            default=False,
            abort=True,
        )
        try:
            acceptance_path.parent.mkdir(parents=True, exist_ok=True)
            acceptance_path.write_text(f"accepted {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n")
        except OSError as exc:
            raise typer.BadParameter(f"could not record dangerous-mode acceptance: {exc}") from exc
    return ApprovalMode.DANGEROUS


def _direct_edit_dangerous_acceptance_path() -> Path:
    state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_root / "looplane" / "direct-edit-dangerous-mode-accepted"


def _confirm_direct_edit_with_dangerous_mode(
    *, edit_real_repo: bool, dangerous: bool, services: CommandServices
) -> None:
    """Require one extra acknowledgment when --edit-real-repo and --dangerous combine.

    --edit-real-repo alone still shows a diff preview for approval before every
    MODIFY tool call; --dangerous alone still only ever touches a disposable clone.
    Combined, MODIFY tool calls land on the real repository with no per-call review
    at all, so this compounds two independent risks and gets its own one-time gate.
    """

    if not (edit_real_repo and dangerous):
        return
    acceptance_path = _direct_edit_dangerous_acceptance_path()
    if acceptance_path.exists() or os.environ.get("LOOPLANE_ACCEPT_DANGEROUS_MODE") == "1":
        return
    if not services.stdin_is_tty():
        raise typer.BadParameter(
            "--edit-real-repo combined with --dangerous requires interactive "
            "acknowledgment once per machine (or LOOPLANE_ACCEPT_DANGEROUS_MODE=1)"
        )
    typer.confirm(
        "You are combining --edit-real-repo with --dangerous: modify actions will land "
        "directly on this repository's real working tree with NO diff shown for "
        "approval. Continue?",
        default=False,
        abort=True,
    )
    try:
        acceptance_path.parent.mkdir(parents=True, exist_ok=True)
        acceptance_path.write_text(f"accepted {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n")
    except OSError as exc:
        raise typer.BadParameter(
            f"could not record direct-edit-dangerous-mode acceptance: {exc}"
        ) from exc


def _permission_guard_from_config(
    *,
    config: CliConfig,
    repository: Path,
    deny_tool: list[str] | None = None,
    dangerous: bool = False,
    services: CommandServices,
):
    from looplane.permissions import ApprovalMode, PermissionGuard
    from looplane.policy_config import discover_policy_rules

    try:
        discovery = discover_policy_rules(
            repository=repository,
            user_deny_rules=config.deny_rules,
            user_allow_rules=config.allow_rules,
            extra_user_deny_rules=deny_tool or (),
        )
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    mode = _enter_dangerous_mode(dangerous, services=services)
    return (
        PermissionGuard(
            mode=mode,
            deny_rules=discovery.rules.deny_rules,
            allow_rules=discovery.rules.allow_rules,
        ),
        mode is ApprovalMode.DANGEROUS
        or bool(discovery.rules.deny_rules)
        or bool(discovery.rules.allow_rules),
    )


def _effective_sandbox_checks(requested: bool) -> bool:
    """Enable default verification sandboxing only where the local profile is reliable."""

    return requested and sys.platform.startswith("linux")


def policy_inspect(
    repository: Path | None = None, org_policy: Path | None = None, json_output: bool = False
) -> None:
    """Show user/org/project policy sources and effective precedence."""

    from looplane.policy_config import ProjectPolicyError, discover_policy_rules

    repository = repository or Path.cwd()
    config_path = default_cli_config_path()
    try:
        config = load_cli_config(config_path)
        discovery = discover_policy_rules(
            repository=repository,
            user_deny_rules=config.deny_rules,
            user_allow_rules=config.allow_rules,
            user_config_path=config_path,
            org_policy_path=org_policy,
        )
    except (ValueError, ProjectPolicyError) as exc:
        if json_output:
            typer.echo(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    diagnostics = {
        "ok": True,
        "repository": str(repository.resolve()),
        "precedence": list(discovery.source_precedence),
        "sources": {
            "user": {
                "path": str(discovery.user_config_path) if discovery.user_config_path else None,
                "exists": bool(discovery.user_config_path and discovery.user_config_path.exists()),
                "deny_rules": list(config.deny_rules),
                "allow_rules": list(config.allow_rules),
            },
            "org": {
                "path": str(discovery.org_policy_path) if discovery.org_policy_path else None,
                "exists": bool(discovery.org_policy_path and discovery.org_policy_path.exists()),
                "deny_rules": list(discovery.org_policy.deny_rules),
                "allow_rules": list(discovery.org_policy.allow_rules),
            },
            "project": {
                "path": str(discovery.project_policy_path),
                "exists": discovery.project_policy_path.exists(),
                "deny_rules": list(discovery.project_policy.deny_rules),
                "allow_rules": list(discovery.project_policy.allow_rules),
            },
        },
        "effective": {
            "deny_rules": [rule.raw_spec for rule in discovery.rules.deny_rules],
            "allow_rules": [rule.raw_spec for rule in discovery.rules.allow_rules],
        },
    }
    if json_output:
        typer.echo(json.dumps(diagnostics, ensure_ascii=False, indent=2, sort_keys=True))
        return

    typer.echo("Policy diagnostics")
    typer.echo(f"repository: {diagnostics['repository']}")
    typer.echo("precedence:")
    for index, source in enumerate(discovery.source_precedence, 1):
        typer.echo(f"  {index}. {source}")
    typer.echo("sources:")
    for name, source in diagnostics["sources"].items():
        assert isinstance(source, dict)
        typer.echo(f"  {name}: {source['path'] or 'not configured'}")
        typer.echo(f"    exists: {source['exists']}")
        typer.echo(f"    deny_rules: {json.dumps(source['deny_rules'], ensure_ascii=False)}")
        typer.echo(f"    allow_rules: {json.dumps(source['allow_rules'], ensure_ascii=False)}")
    typer.echo("effective:")
    effective = diagnostics["effective"]
    assert isinstance(effective, dict)
    typer.echo(f"  deny_rules: {json.dumps(effective['deny_rules'], ensure_ascii=False)}")
    typer.echo(f"  allow_rules: {json.dumps(effective['allow_rules'], ensure_ascii=False)}")
