"""Terminal scroll feature owner."""

from __future__ import annotations

from textual.containers import VerticalScroll
from textual.message import Message


class TranscriptScroll(VerticalScroll):
    """Transcript viewport that reports when the user returns to the live edge."""

    class PositionChanged(Message):
        pass

    def watch_scroll_y(self, old: float, new: float) -> None:
        super().watch_scroll_y(old, new)
        if self.is_mounted:
            self.post_message(self.PositionChanged())
