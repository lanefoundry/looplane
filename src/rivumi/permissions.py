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
from collections.abc import Sequence
from enum import StrEnum

from rivumi.approvals import (
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalRequest,
    ToolEffect,
)

_TIER_RANK: dict[ToolEffect, int] = {
    ToolEffect.READ: 0,
    ToolEffect.MODIFY: 1,
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


def command_segments(command: str) -> tuple[str, ...]:
    """Split a shell command line into individual pipeline/compound segments.

    This is a lexical best-effort splitter (quoting is not fully honoured);
    over-splitting only risks missing a match inside quotes, never inventing
    one.
    """

    return tuple(segment.strip() for segment in _COMPOUND_SPLIT.split(command) if segment.strip())


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


class DenyRule:
    """One forbidden-operation rule parsed from ``Tool(content)`` syntax.

    Content forms follow the de-facto standard popularised by Claude Code:
    ``tool`` (whole tool), ``tool(prefix:*``, ``tool(with*wildcards)``, and
    ``tool(exact text)``. Only ``*`` is special; parens may be escaped with
    a backslash.
    """

    __slots__ = ("kind", "tool_name", "value")

    def __init__(self, tool_name: str, kind: str, value: str | re.Pattern[str]) -> None:
        self.tool_name = tool_name
        self.kind = kind
        self.value = value

    @classmethod
    def parse(cls, spec: str) -> DenyRule:
        if not spec or spec != spec.strip() or "\x00" in spec:
            raise ValueError(f"invalid deny rule: {spec!r}")
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*)(?:\((.*)\))?", spec, re.DOTALL)
        if match is None:
            raise ValueError(f"invalid deny rule: {spec!r}")
        tool_name = match.group(1)
        raw = match.group(2)
        if raw is None:
            return cls(tool_name, "tool", "")
        literal = _unescape(raw)
        if raw.endswith(":*") and not raw.endswith("\\:*"):
            return cls(tool_name, "prefix", literal[:-2])
        if _has_unescaped_wildcard(raw):
            return cls(tool_name, "wildcard", _compile_wildcard(literal))
        return cls(tool_name, "exact", literal)

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
    ) -> None:
        self.mode = ApprovalMode(mode)
        self.deny_rules = tuple(deny_rules)

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
        if request.effect == ToolEffect.EXECUTE:
            violation = find_critical_command_violation(resolved)
            if violation is not None:
                return f"critical command floor: {violation}"
        for rule in self.deny_rules:
            tool_name = request.tool_call.name if request.tool_call is not None else ""
            if rule.matches(tool_name, resolved):
                return f"deny rule {rule.tool_name} ({rule.kind})"
        return None

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
            "and set RIVUMI_SANDBOX=1 to override"
        )
    if accepted or env_acknowledged:
        return "granted"
    if not is_tty:
        raise DangerousModeError(
            "--dangerous requires interactive confirmation; re-run inside a terminal "
            "or set RIVUMI_ACCEPT_DANGEROUS_MODE=1 to acknowledge it non-interactively"
        )
    return "prompt"
