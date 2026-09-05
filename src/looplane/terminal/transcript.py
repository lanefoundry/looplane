"""Terminal transcript feature owner."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from looplane.terminal.links import TranscriptMarkdown


class MessageBlock(Vertical):
    """One safe transcript turn using Claude Code's asymmetric hierarchy."""

    DEFAULT_CSS = """
    MessageBlock { height: auto; margin-bottom: 1; padding: 0; }
    MessageBlock.user {
        background: $boost; padding: 0 1; border-left: thick $accent;
    }
    MessageBlock .message-body { width: 1fr; height: auto; }
    MessageBlock Markdown { padding: 0; background: transparent; }
    MessageBlock .assistant-row { width: 100%; height: auto; }
    MessageBlock .assistant-glyph {
        width: 2; height: 1; color: $success; text-style: bold;
    }
    """

    def __init__(self, role: str, content: str) -> None:
        css_class = "user" if role in {"You", "Task"} else "assistant"
        super().__init__(classes=f"message {css_class}")
        self.role = role
        self.content = content

    def compose(self) -> ComposeResult:
        if self.has_class("user"):
            yield Static(self.content, classes="message-body", markup=False)
            return
        with Horizontal(classes="assistant-row"):
            yield Static("●", classes="assistant-glyph", markup=False)
            yield TranscriptMarkdown(self.content, classes="message-body", open_links=False)

    def append_content(self, text: str) -> None:
        self.content += text
        if self.query(".message-body"):
            body = self.query_one(".message-body")
            body.update(self.content)


class TimelineEntry(Vertical):
    """One high-level agent activity or result in the main timeline."""

    DEFAULT_CSS = """
    TimelineEntry { height: auto; margin-bottom: 1; padding-left: 1; }
    TimelineEntry .timeline-title { height: 1; text-style: bold; }
    TimelineEntry.failure .timeline-title { color: $error; }
    TimelineEntry .timeline-detail {
        height: auto; margin: 1 0 0 2; color: $text-muted;
    }
    """

    def __init__(
        self,
        title: str,
        detail: str | None = None,
        *,
        severity: str | None = None,
    ) -> None:
        classes = "timeline-entry" if severity is None else f"timeline-entry {severity}"
        super().__init__(classes=classes)
        self.title = title
        self.detail = detail

    def compose(self) -> ComposeResult:
        yield Static(f"• {self.title}", classes="timeline-title", markup=False)
        if self.detail:
            yield Static(self.detail, classes="timeline-detail", markup=False)
