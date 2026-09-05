"""Safe clickable links for the Textual transcript."""

from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from textual.widgets import Markdown, Static


@dataclass(frozen=True)
class ResolvedTranscriptLink:
    """A URL approved for handoff to the terminal's system opener."""

    url: str
    repository_path: Path | None = None


_LINE_SUFFIX = re.compile(r":\d+(?::\d+)?$")


def resolve_transcript_link(repository: Path, href: str) -> ResolvedTranscriptLink | None:
    """Allow complete HTTP(S) URLs and existing files contained by the repository."""

    if any(ord(character) < 32 or ord(character) == 127 for character in href):
        return None
    try:
        parsed = urlsplit(href)
    except ValueError:
        return None
    if (
        parsed.scheme.lower() in {"http", "https"}
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
    ):
        return ResolvedTranscriptLink(href)

    reference = _LINE_SUFFIX.sub("", href)
    try:
        parsed = urlsplit(reference)
    except ValueError:
        return None
    if parsed.query:
        return None
    if parsed.scheme == "file":
        if parsed.hostname not in {None, "", "localhost"}:
            return None
        raw_path = unquote(parsed.path)
    elif not parsed.scheme:
        raw_path = unquote(parsed.path)
    else:
        return None
    if not raw_path:
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = repository / candidate
    try:
        repository = repository.resolve(strict=True)
        candidate = candidate.resolve(strict=True)
        candidate.relative_to(repository)
    except (OSError, RuntimeError, ValueError):
        return None
    if not candidate.is_file():
        return None
    return ResolvedTranscriptLink(candidate.as_uri(), candidate)


class TranscriptMarkdown(Markdown):
    """Assistant Markdown with bounded web and repository-file links."""

    def on_markdown_link_clicked(self, event: Markdown.LinkClicked) -> None:
        event.stop()
        resolved = resolve_transcript_link(self.app.repository, event.href)
        if resolved is None:
            with suppress(Exception):
                self.app.query_one("#status", Static).update(
                    "Link blocked · open only HTTP(S) URLs or files in this repository"
                )
            return
        self.app.open_url(resolved.url)
        if resolved.repository_path is not None:
            with suppress(Exception):
                relative_file = resolved.repository_path.relative_to(
                    self.app.repository.resolve()
                )
                self.app.query_one("#status", Static).update(f"Opening {relative_file}")
