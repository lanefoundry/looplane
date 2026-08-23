"""App-owned semantic transcript accumulator for post-exit scrollback.

The reducer mirrors exactly what the full-screen TUI finalizes into its
transcript -- user prompts, assistant messages, tool/action outcomes, and
high-level timeline notices -- while excluding transient chrome such as
spinners, composer widgets, selectors, and permission prompts.  After
``RivumiApp.run()`` returns, :meth:`TranscriptReducer.render` produces a
bounded plain-text transcript that the CLI prints into the terminal's
primary buffer so history survives in scrollback.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_EXPORT_ITEMS = 400
MAX_ITEM_CHARS = 1_600
MAX_DETAIL_CHARS = 800
MAX_EXPORT_CHARS = 48_000

_USER_ROLES = frozenset({"You", "Task"})
_ASSISTANT_ROLES = frozenset({"Assistant", "Agent"})


def _bounded(text: str, limit: int) -> str:
    collapsed = text.rstrip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


@dataclass
class TranscriptRow:
    kind: str
    title: str
    detail: str = ""


@dataclass
class TranscriptReducer:
    """Accumulates finalized semantic rows in submission order."""

    _rows: list[TranscriptRow] = field(default_factory=list)

    def reset(self) -> None:
        self._rows.clear()

    def __len__(self) -> int:
        return len(self._rows)

    def add_user(self, text: str) -> None:
        if not text.strip():
            return
        self._rows.append(TranscriptRow("user", "You", _bounded(text, MAX_ITEM_CHARS)))

    def add_assistant(self, text: str) -> None:
        if not text.strip():
            return
        self._rows.append(
            TranscriptRow("assistant", "Assistant", _bounded(text, MAX_ITEM_CHARS))
        )

    def add_tool(self, title: str, status: str, detail: str = "") -> None:
        glyph = {
            "completed": "[ok]",
            "failed": "[failed]",
            "cancelled": "[cancelled]",
            "denied": "[denied]",
        }.get(status, f"[{status or 'tool'}]")
        self._rows.append(
            TranscriptRow(
                "tool",
                f"{glyph} {_bounded(title, 160)}",
                _bounded(detail, MAX_DETAIL_CHARS) if detail else "",
            )
        )

    def add_notice(self, title: str, detail: str = "") -> None:
        self._rows.append(
            TranscriptRow(
                "notice",
                _bounded(title, 160),
                _bounded(detail, MAX_ITEM_CHARS) if detail else "",
            )
        )

    def render(
        self,
        *,
        conversation_id: str | None,
        resume_command: str | None,
    ) -> str:
        if not self._rows:
            return ""
        kept: list[TranscriptRow] = []
        used = 0
        # Keep the most recent rows within the global character budget.
        for row in reversed(self._rows[-MAX_EXPORT_ITEMS:]):
            size = len(row.title) + len(row.detail) + 2
            if used + size > MAX_EXPORT_CHARS:
                break
            kept.append(row)
            used += size
        kept.reverse()

        lines: list[str] = []
        header = "Rivumi session"
        if conversation_id:
            header += f" · conversation {conversation_id}"
        lines.append(header)
        if resume_command:
            lines.append(f"Resume with: {resume_command}")
        lines.append("")
        for row in kept:
            if row.kind == "user":
                lines.append(f"You › {row.detail}")
            elif row.kind == "assistant":
                lines.append(f"Assistant › {row.detail}")
            elif row.kind == "tool":
                lines.append(row.title)
                if row.detail:
                    lines.extend(
                        f"    {line}" for line in row.detail.splitlines() or [""]
                    )
            else:
                lines.append(f"· {row.title}")
                if row.detail:
                    lines.extend(
                        f"    {line}" for line in row.detail.splitlines() or [""]
                    )
        return "\n".join(lines).rstrip() + "\n"
