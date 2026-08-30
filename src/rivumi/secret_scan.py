"""Conservative secret-pattern checks for patches, terminal text, and exports."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SecretFinding:
    path: str
    line: int | None
    pattern: str

    def label(self) -> str:
        location = self.path
        if self.line is not None:
            location = f"{location}:{self.line}"
        return f"{location} ({self.pattern})"


_PATH_HEADER = re.compile(r"^\+\+\+ b/(.+)$")
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{20,}\b")
_ANTHROPIC_KEY = re.compile(r"\bsk-ant-[A-Za-z0-9][A-Za-z0-9_-]{20,}\b")
_GITHUB_TOKEN = re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")
_BEARER_TOKEN = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{24,}={0,2}\b", re.IGNORECASE)
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    \b
    (?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|secret|password)
    \b
    \s*[:=]\s*
    ["']?
    ([A-Za-z0-9._~+/\-]{24,}={0,2})
    """
)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", _PRIVATE_KEY),
    ("openai-api-key", _OPENAI_KEY),
    ("anthropic-api-key", _ANTHROPIC_KEY),
    ("github-token", _GITHUB_TOKEN),
    ("bearer-token", _BEARER_TOKEN),
    ("secret-assignment", _SECRET_ASSIGNMENT),
)


def scan_text_for_secrets(text: str, *, path: str = "<text>") -> tuple[SecretFinding, ...]:
    """Return secret-looking lines in arbitrary terminal/export text without echoing values."""

    findings: list[SecretFinding] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        for name, pattern in _PATTERNS:
            if pattern.search(line):
                findings.append(SecretFinding(path=path, line=line_number, pattern=name))
                break
    return tuple(findings)


def scan_file_for_secrets(
    path: str | Path,
    *,
    max_bytes: int = 1_000_000,
) -> tuple[SecretFinding, ...]:
    """Scan one bounded UTF-8-ish artifact file for secret-looking text."""

    artifact = Path(path)
    with artifact.open("rb") as handle:
        payload = handle.read(max_bytes + 1)
    if len(payload) > max_bytes:
        payload = payload[:max_bytes]
    return scan_text_for_secrets(payload.decode("utf-8", errors="replace"), path=str(artifact))


def redact_secrets(text: str, *, marker: str = "[REDACTED_SECRET]") -> str:
    """Redact known secret-looking values before terminal output or exported artifacts."""

    redacted = text
    for _name, pattern in _PATTERNS:
        redacted = pattern.sub(marker, redacted)
    return redacted


def scan_patch_for_secrets(patch: str) -> tuple[SecretFinding, ...]:
    """Return secret-looking additions in a unified diff without echoing values."""

    findings: list[SecretFinding] = []
    current_path = "<unknown>"
    new_line: int | None = None
    for raw_line in patch.splitlines():
        if match := _PATH_HEADER.match(raw_line):
            current_path = match.group(1)
            new_line = None
            continue
        if match := _HUNK_HEADER.match(raw_line):
            new_line = int(match.group(1))
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            content = raw_line[1:]
            for name, pattern in _PATTERNS:
                if pattern.search(content):
                    findings.append(
                        SecretFinding(path=current_path, line=new_line, pattern=name)
                    )
                    break
            if new_line is not None:
                new_line += 1
            continue
        if raw_line.startswith("-") and not raw_line.startswith("---"):
            continue
        if new_line is not None:
            new_line += 1
    return tuple(findings)
