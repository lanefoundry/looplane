"""Project policy discovery for repository-local permission rules."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from rivumi.cli_config import (
    MAX_ALLOW_RULE_CHARS,
    MAX_ALLOW_RULES,
    MAX_CONFIG_BYTES,
    MAX_DENY_RULE_CHARS,
    MAX_DENY_RULES,
)
from rivumi.permissions import (
    AllowRule,
    DenyRule,
    PermissionRuleSet,
    merge_permission_rule_sources,
)

PROJECT_POLICY_RELATIVE_PATH = Path(".rivumi") / "policy.json"


class ProjectPolicyError(ValueError):
    """Raised when a discovered project policy is unsafe or invalid."""


class ProjectPolicyConfig(BaseModel):
    """Strict, repository-local permission policy schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deny_rules: tuple[str, ...] = ()
    allow_rules: tuple[str, ...] = ()

    @field_validator("deny_rules")
    @classmethod
    def validate_deny_rules(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_DENY_RULES:
            raise ValueError(f"deny_rules cannot contain more than {MAX_DENY_RULES} entries")
        normalized: list[str] = []
        for rule in value:
            rule = _normalized_rule(rule)
            if len(rule) > MAX_DENY_RULE_CHARS:
                raise ValueError(
                    f"deny_rules entries cannot exceed {MAX_DENY_RULE_CHARS} characters"
                )
            DenyRule.parse(rule)
            normalized.append(rule)
        return tuple(normalized)

    @field_validator("allow_rules")
    @classmethod
    def validate_allow_rules(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_ALLOW_RULES:
            raise ValueError(f"allow_rules cannot contain more than {MAX_ALLOW_RULES} entries")
        normalized: list[str] = []
        for rule in value:
            rule = _normalized_rule(rule)
            if len(rule) > MAX_ALLOW_RULE_CHARS:
                raise ValueError(
                    f"allow_rules entries cannot exceed {MAX_ALLOW_RULE_CHARS} characters"
                )
            AllowRule.parse(rule)
            normalized.append(rule)
        return tuple(normalized)


@dataclass(frozen=True)
class PolicyDiscovery:
    """Discovered user/project policy sources and effective rule order."""

    user_config_path: Path | None
    org_policy_path: Path | None
    org_policy: ProjectPolicyConfig
    project_policy_path: Path
    project_policy: ProjectPolicyConfig
    rules: PermissionRuleSet

    @property
    def source_precedence(self) -> tuple[str, ...]:
        """Human-readable precedence, from highest authority to lowest."""

        return (
            "critical command floor",
            "user deny_rules",
            "org deny_rules",
            "project deny_rules",
            "user allow_rules",
            "org allow_rules",
            "project allow_rules",
        )


def project_policy_path(repository: Path) -> Path:
    """Return the conventional project policy path for ``repository``."""

    return repository / PROJECT_POLICY_RELATIVE_PATH


def load_project_policy_config(repository: Path) -> ProjectPolicyConfig:
    """Load ``.rivumi/policy.json`` or return an empty policy when absent.

    A present but invalid policy fails closed: callers should treat
    :class:`ProjectPolicyError` as a startup-blocking configuration error.
    """

    path = project_policy_path(repository)
    if not path.exists():
        return ProjectPolicyConfig()
    return _load_policy_file(path, label="project")


def load_org_policy_config(path: Path | None = None) -> tuple[Path | None, ProjectPolicyConfig]:
    """Load optional org-level policy from an explicit path or ``RIVUMI_ORG_POLICY``."""

    configured = str(path) if path is not None else os.environ.get("RIVUMI_ORG_POLICY")
    if not configured:
        return None, ProjectPolicyConfig()
    resolved = Path(configured).expanduser()
    return resolved, _load_policy_file(resolved, label="org")


def _load_policy_file(path: Path, *, label: str) -> ProjectPolicyConfig:
    if path.is_symlink() or not path.is_file():
        raise ProjectPolicyError(f"{label} policy must be a regular file: {path}")
    with path.open("rb") as file:
        payload = file.read(MAX_CONFIG_BYTES + 1)
    if len(payload) > MAX_CONFIG_BYTES:
        raise ProjectPolicyError(f"{label} policy exceeds 64 KiB: {path}")
    try:
        value = json.loads(payload)
    except UnicodeDecodeError as exc:
        raise ProjectPolicyError(f"{label} policy is not valid UTF-8 JSON: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ProjectPolicyError(f"{label} policy is not valid JSON: {path}") from exc
    try:
        return ProjectPolicyConfig.model_validate(value)
    except ValueError as exc:
        raise ProjectPolicyError(f"{label} policy is invalid: {path}: {exc}") from exc


def discover_policy_rules(
    *,
    repository: Path,
    user_deny_rules: Sequence[str] = (),
    user_allow_rules: Sequence[str] = (),
    user_config_path: Path | None = None,
    extra_user_deny_rules: Sequence[str] = (),
    org_policy_path: Path | None = None,
) -> PolicyDiscovery:
    """Discover user/project policy sources and merge with explicit precedence."""

    discovered_org_policy_path, org_policy = load_org_policy_config(org_policy_path)
    project_policy = load_project_policy_config(repository)
    rules = merge_permission_rule_sources(
        user_deny_rules=tuple(
            DenyRule.parse(spec) for spec in (*user_deny_rules, *extra_user_deny_rules)
        ),
        org_deny_rules=tuple(DenyRule.parse(spec) for spec in org_policy.deny_rules),
        project_deny_rules=tuple(DenyRule.parse(spec) for spec in project_policy.deny_rules),
        user_allow_rules=tuple(AllowRule.parse(spec) for spec in user_allow_rules),
        org_allow_rules=tuple(AllowRule.parse(spec) for spec in org_policy.allow_rules),
        project_allow_rules=tuple(AllowRule.parse(spec) for spec in project_policy.allow_rules),
    )
    return PolicyDiscovery(
        user_config_path=user_config_path,
        org_policy_path=discovered_org_policy_path,
        org_policy=org_policy,
        project_policy_path=project_policy_path(repository),
        project_policy=project_policy,
        rules=rules,
    )


def _normalized_rule(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("policy rules cannot be blank")
    if "\x00" in value:
        raise ValueError("policy rules cannot contain NUL")
    return value
