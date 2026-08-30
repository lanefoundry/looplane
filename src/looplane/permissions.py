"""Deny-first permission layer for dangerous mode with forbidden-operation rules.

The evaluation order mirrors the cross-project consensus documented in
``.research/dangerous-mode-and-deny.md``: the critical command floor and
explicit deny rules are authoritative and always win, even when dangerous
mode would auto-approve the request. Dangerous mode itself only widens the
auto-approval ceiling (read + modify); execution still falls through to the
wrapped policy so host command execution keeps its own gate.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from looplane.approvals import (
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalRequest,
    ToolEffect,
)

_TIER_RANK: dict[ToolEffect, int] = {
    ToolEffect.READ: 0,
    ToolEffect.MODIFY: 1,
    ToolEffect.MODIFY_EXECUTE: 2,
    ToolEffect.EXECUTE: 2,
}


class ApprovalMode(StrEnum):
    """How far auto-approval reaches before the wrapped policy is consulted."""

    DEFAULT = "default"
    DANGEROUS = "dangerous"


class DangerousModeError(ValueError):
    """Raised when entering dangerous mode is refused or needs confirmation."""


# Dangerous mode auto-approves up to this tier; EXECUTE is deliberately excluded.
DANGEROUS_AUTO_ALLOW_MAX_RANK = _TIER_RANK[ToolEffect.MODIFY]

_COMPOUND_SPLIT = re.compile(r"(?:\|\||&&|[;|])")
_COMPOUND_OPERATORS = frozenset({"&&", "||", ";", "|"})
SUSPICIOUS_COMMAND_TIMEOUT_DENY_SECONDS = 120.0


class CommandPolicyAction(StrEnum):
    """Pure command-policy outcome before the approval UI is consulted."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class CommandPolicyResult:
    """Auditable command classification result."""

    action: CommandPolicyAction
    reason: str
    subject: str = ""


def command_segments(command: str) -> tuple[str, ...]:
    """Split a shell command line into individual pipeline/compound segments.

    This is a lexical best-effort splitter. ``shlex`` handles quoted shell
    text; malformed shell input falls back to the conservative legacy regex
    splitter so critical deny checks still inspect each visible segment.
    """

    return tuple(segment for segment, _operator in _split_command_line(command))


def _split_command_line(command: str) -> tuple[tuple[str, str | None], ...]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        tokens = tuple(lexer)
    except ValueError:
        return tuple(
            (segment.strip(), None) for segment in _COMPOUND_SPLIT.split(command) if segment.strip()
        )

    segments: list[tuple[str, str | None]] = []
    current: list[str] = []
    pending_operator: str | None = None
    for token in tokens:
        if token in _COMPOUND_OPERATORS:
            if current:
                segments.append((" ".join(current), pending_operator))
                current = []
            pending_operator = token
            continue
        current.append(token)
    if current:
        segments.append((" ".join(current), pending_operator))
    return tuple(segments)


CRITICAL_COMMAND_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"--no-preserve-root",
        r"\bsudo\s+(?:\S+\s+)*rm\b",
        r"\brm\s+(?:-{1,2}[a-z][a-z-]*\s+)*-\w*r\w*\s+(?:/|~|\$HOME)(?:\s|$)",
        r"\brm\s+[^;&|]{0,40}--recursive\s+(?:/|~|\$HOME)(?:\s|$)",
        r":\(\)\s*\{\s*:\s*\|\s*:\s*&",
        r"\bmkfs(?:\.[a-z0-9]+)?\b",
        r"\bdd\b[^;&|]*\bof=/dev/",
        r"\bshred\b",
        r"\bcryptsetup\b",
        r">\s*/etc/(?:passwd|shadow|sudoers)\b",
        r"\btee\s+[^;&|]*/etc/(?:passwd|shadow|sudoers)\b",
        r"\b(?:curl|wget)\b[^;&|]*\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b",
        r"\bbash\s+<\(",
        r"\beval\s+[^(]*\(\s*(?:curl|wget)",
        r"^\s*(?:sudo\s+)?(?:shutdown|reboot|poweroff|halt)(?:\s|$)",
        r"\bnc\b[^;&|]*\s-e\b",
        r"\bchmod\b[^;&|]*\s-[a-z]*r[a-z]*\s+[0-7]{3}\s+/(?:\s|$)",
    )
)


def find_critical_command_violation(segments: Sequence[str]) -> str | None:
    """Return the first critical pattern hit across compound-command segments.

    Cross-segment forms such as ``curl … | sh`` lose their pipe operator when
    split, so the re-joined whole command is checked alongside each segment.
    """

    candidates = (*segments, "| ".join(segments))
    for candidate in candidates:
        for pattern in CRITICAL_COMMAND_PATTERNS:
            if pattern.search(candidate):
                return f"{pattern.pattern} in {candidate!r}"
    return None


SUSPICIOUS_COMMAND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (reason, re.compile(pattern, re.IGNORECASE))
    for reason, pattern in (
        ("privilege escalation", r"^\s*(?:sudo|su)(?:\s|$)"),
        ("git remote write", r"^\s*git\s+(?:push|reset\s+--hard|clean\s+-[^\s]*f)"),
        ("network download", r"^\s*(?:curl|wget)\b"),
        ("shell interpreter", r"^\s*(?:sh|bash|zsh|fish|python|python3|ruby|perl)\s+-c\b"),
        ("package installation", r"^\s*(?:npm|pnpm|yarn|pip|pipx|brew)\s+.*\binstall\b"),
        ("permission ownership change", r"^\s*(?:chmod|chown|chgrp)\b"),
        ("archive extraction", r"^\s*(?:tar|unzip|7z)\b"),
        ("home or absolute path removal", r"^\s*rm\b.*(?:\s/|\s~|\s\$HOME)"),
        ("shell redirection", r"(?:^|\s)(?:>|>>|<)\s*\S+"),
    )
)


def classify_command_policy(
    command: str,
    *,
    timeout_seconds: float | None = None,
) -> CommandPolicyResult:
    """Classify one shell-shaped command as allow / ask / deny.

    The critical deny floor is intentionally checked before softer suspicious
    shapes. Suspicious commands require approval; when they also carry a large
    timeout budget, they are denied because a human prompt cannot bound the
    execution risk well enough.
    """

    command = command.strip()
    if not command:
        return CommandPolicyResult(CommandPolicyAction.DENY, "blank command")

    split = _split_command_line(command)
    segments = tuple(segment for segment, _operator in split)
    violation = find_critical_command_violation(segments)
    if violation is not None:
        return CommandPolicyResult(
            CommandPolicyAction.DENY,
            f"critical command floor: {violation}",
            command,
        )

    suspicious_reasons: list[str] = []
    if len(segments) > 1:
        suspicious_reasons.append("compound shell command")
    for segment, operator in split:
        if operator is not None:
            suspicious_reasons.append(f"shell operator {operator}")
        for reason, pattern in SUSPICIOUS_COMMAND_PATTERNS:
            if pattern.search(segment):
                suspicious_reasons.append(reason)

    if suspicious_reasons:
        unique_reasons = tuple(dict.fromkeys(suspicious_reasons))
        reason = "suspicious command shape: " + ", ".join(unique_reasons)
        if (
            timeout_seconds is not None
            and timeout_seconds > SUSPICIOUS_COMMAND_TIMEOUT_DENY_SECONDS
        ):
            return CommandPolicyResult(
                CommandPolicyAction.DENY,
                (
                    f"timeout-deny: {reason}; timeout_seconds={timeout_seconds:g} "
                    f"exceeds {SUSPICIOUS_COMMAND_TIMEOUT_DENY_SECONDS:g}"
                ),
                command,
            )
        return CommandPolicyResult(CommandPolicyAction.ASK, reason, command)

    return CommandPolicyResult(CommandPolicyAction.ALLOW, "no suspicious command shape", command)


class DenyRule:
    """One forbidden-operation rule parsed from ``Tool(content)`` syntax.

    Content forms follow the de-facto standard popularised by Claude Code:
    ``tool`` (whole tool), ``tool(prefix:*``, ``tool(with*wildcards)``, and
    ``tool(exact text)``. Only ``*`` is special; parens may be escaped with
    a backslash.
    """

    __slots__ = ("kind", "raw_spec", "tool_name", "value")
    invalid_label = "deny"

    def __init__(
        self,
        tool_name: str,
        kind: str,
        value: str | re.Pattern[str],
        *,
        raw_spec: str | None = None,
    ) -> None:
        self.tool_name = tool_name
        self.kind = kind
        self.value = value
        self.raw_spec = raw_spec or tool_name

    @classmethod
    def parse(cls, spec: str) -> Self:
        if not spec or spec != spec.strip() or "\x00" in spec:
            raise ValueError(f"invalid {cls.invalid_label} rule: {spec!r}")
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*)(?:\((.*)\))?", spec, re.DOTALL)
        if match is None:
            raise ValueError(f"invalid {cls.invalid_label} rule: {spec!r}")
        tool_name = match.group(1)
        raw = match.group(2)
        if raw is None:
            return cls(tool_name, "tool", "", raw_spec=spec)
        literal = _unescape(raw)
        if raw.endswith(":*") and not raw.endswith("\\:*"):
            return cls(tool_name, "prefix", literal[:-2], raw_spec=spec)
        if _has_unescaped_wildcard(raw):
            return cls(tool_name, "wildcard", _compile_wildcard(literal), raw_spec=spec)
        return cls(tool_name, "exact", literal, raw_spec=spec)

    def matches(self, tool_name: str, subjects: Sequence[str]) -> bool:
        if self.tool_name != tool_name:
            return False
        if self.kind == "tool":
            return True
        for subject in subjects:
            if self.kind == "exact":
                if subject == self.value:
                    return True
            elif self.kind == "prefix":
                if subject.startswith(str(self.value)):
                    return True
            elif isinstance(self.value, re.Pattern) and self.value.search(subject):
                return True
        return False


class AllowRule(DenyRule):
    """One positive operation rule parsed with the same syntax as deny rules."""

    invalid_label = "allow"


@dataclass(frozen=True)
class PermissionRuleSet:
    """Merged user/project policy sources in evaluation order."""

    deny_rules: tuple[DenyRule, ...] = ()
    allow_rules: tuple[AllowRule, ...] = ()


def merge_permission_rule_sources(
    *,
    user_deny_rules: Sequence[DenyRule] = (),
    org_deny_rules: Sequence[DenyRule] = (),
    project_deny_rules: Sequence[DenyRule] = (),
    user_allow_rules: Sequence[AllowRule] = (),
    org_allow_rules: Sequence[AllowRule] = (),
    project_allow_rules: Sequence[AllowRule] = (),
) -> PermissionRuleSet:
    """Merge user and project policy sources without changing evaluation policy."""

    return PermissionRuleSet(
        deny_rules=(*user_deny_rules, *org_deny_rules, *project_deny_rules),
        allow_rules=(*user_allow_rules, *org_allow_rules, *project_allow_rules),
    )


def _unescape(text: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character == "\\" and index + 1 < len(text):
            result.append(text[index + 1])
            index += 2
            continue
        result.append(character)
        index += 1
    return "".join(result)


def _has_unescaped_wildcard(text: str) -> bool:
    index = 0
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == "*":
            return True
        index += 1
    return False


def _compile_wildcard(literal: str) -> re.Pattern[str]:
    parts: list[str] = []
    for character in literal:
        if character == "*":
            parts.append(".*")
        else:
            parts.append(re.escape(character))
    return re.compile("".join(parts), re.DOTALL)


class PermissionGuard:
    """Deny-first pre-decision layer consulted before any approval policy."""

    def __init__(
        self,
        *,
        mode: ApprovalMode = ApprovalMode.DEFAULT,
        deny_rules: Sequence[DenyRule] = (),
        allow_rules: Sequence[AllowRule] = (),
    ) -> None:
        self.mode = ApprovalMode(mode)
        self.deny_rules = tuple(deny_rules)
        self.allow_rules = tuple(allow_rules)

    @staticmethod
    def request_subjects(request: ApprovalRequest) -> tuple[str, ...]:
        """Text candidates derived from the request itself."""

        parts: list[str] = []
        if request.command is not None:
            joined = " ".join(request.command.argv)
            parts.append(joined)
            parts.extend(command_segments(joined))
        if request.tool_call is not None:
            parts.extend(
                str(value)
                for value in request.tool_call.arguments.values()
                if isinstance(value, str)
            )
        return tuple(parts)

    def forbidden_reason(
        self,
        request: ApprovalRequest,
        subjects: Sequence[str] | None = None,
    ) -> str | None:
        """Return why this request is unconditionally forbidden, if it is."""

        resolved = self.request_subjects(request) if subjects is None else tuple(subjects)
        for rule in self.deny_rules:
            tool_name = request.tool_call.name if request.tool_call is not None else ""
            if rule.matches(tool_name, resolved):
                return f"deny rule {rule.tool_name} ({rule.kind})"
        if request.effect in {ToolEffect.EXECUTE, ToolEffect.MODIFY_EXECUTE}:
            classification = self.command_policy(request, resolved)
            if classification.action is CommandPolicyAction.DENY:
                return classification.reason
        return None

    def command_policy(
        self,
        request: ApprovalRequest,
        subjects: Sequence[str] | None = None,
    ) -> CommandPolicyResult:
        """Return the command policy classification for an execution request."""

        if request.effect not in {ToolEffect.EXECUTE, ToolEffect.MODIFY_EXECUTE}:
            return CommandPolicyResult(
                CommandPolicyAction.ALLOW,
                f"non-execute effect: {request.effect.value}",
            )
        resolved = self.request_subjects(request) if subjects is None else tuple(subjects)
        if not resolved:
            return CommandPolicyResult(CommandPolicyAction.DENY, "execute request has no command")

        timeout = request.command.timeout_seconds if request.command is not None else None
        results = tuple(
            classify_command_policy(subject, timeout_seconds=timeout)
            for subject in resolved
            if subject.strip()
        )
        if not results:
            return CommandPolicyResult(CommandPolicyAction.DENY, "execute request has no command")
        for result in results:
            if result.action is CommandPolicyAction.DENY:
                return result
        for result in results:
            if result.action is CommandPolicyAction.ASK:
                return result
        return results[0]

    def approval_policy_reason(
        self,
        request: ApprovalRequest,
        subjects: Sequence[str] | None = None,
    ) -> str:
        """Return an approval-visible policy reason for ASK-classified commands."""

        classification = self.command_policy(request, subjects)
        if classification.action is CommandPolicyAction.ASK:
            return classification.reason
        return ""

    def pre_decision(
        self,
        request: ApprovalRequest,
        subjects: Sequence[str] | None = None,
    ) -> ApprovalDecision | None:
        """Return DENY / ALLOW_ONCE without consulting the wrapped policy.

        None means "no opinion": continue with grant reuse, prompting, or the
        inner policy exactly as before.
        """

        reason = self.forbidden_reason(request, subjects)
        if reason is not None:
            return ApprovalDecision.DENY
        resolved = self.request_subjects(request) if subjects is None else tuple(subjects)
        tool_name = request.tool_call.name if request.tool_call is not None else ""
        for rule in self.allow_rules:
            if rule.matches(tool_name, resolved):
                return ApprovalDecision.ALLOW_ONCE
        if (
            self.mode is ApprovalMode.DANGEROUS
            and _TIER_RANK[request.effect] <= DANGEROUS_AUTO_ALLOW_MAX_RANK
        ):
            return ApprovalDecision.ALLOW_ONCE
        return None


class GuardedApprovalPolicy:
    """Wrap an :class:`ApprovalPolicy` so every decision passes the guard first."""

    def __init__(self, inner: ApprovalPolicy, guard: PermissionGuard) -> None:
        self._inner = inner
        self._guard = guard

    @property
    def guard(self) -> PermissionGuard:
        return self._guard

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        pre_decision = self._guard.pre_decision(request)
        if pre_decision is not None:
            return pre_decision
        policy_reason = self._guard.approval_policy_reason(request)
        if policy_reason and request.policy_reason != policy_reason:
            request = request.model_copy(update={"policy_reason": policy_reason})
        return await self._inner.decide(request)


def plan_dangerous_mode_entry(
    *,
    accepted: bool,
    env_acknowledged: bool,
    is_tty: bool,
    is_root: bool,
    sandboxed: bool,
) -> str:
    """Gate entry into dangerous mode the way upstream agents do.

    Returns ``"granted"`` when the run may start immediately and ``"prompt"``
    when an interactive confirmation dialog must be shown first. Raises
    :class:`DangerousModeError` when entry is refused outright.
    """

    if is_root and not sandboxed:
        raise DangerousModeError(
            "--dangerous cannot be used with root/sudo privileges; run inside a sandbox "
            "and set LOOPLANE_SANDBOX=1 to override"
        )
    if accepted or env_acknowledged:
        return "granted"
    if not is_tty:
        raise DangerousModeError(
            "--dangerous requires interactive confirmation; re-run inside a terminal "
            "or set LOOPLANE_ACCEPT_DANGEROUS_MODE=1 to acknowledge it non-interactively"
        )
    return "prompt"
