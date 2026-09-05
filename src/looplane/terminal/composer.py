"""Terminal composer feature owner."""

from __future__ import annotations

from textual import events
from textual.message import Message
from textual.widgets import TextArea


class MessageComposer(TextArea):
    """Multiline composer with submit, command navigation, and history messages."""

    class Submitted(Message):
        def __init__(self, composer: MessageComposer) -> None:
            super().__init__()
            self.composer = composer
            self.text = composer.text

    class CommandNavigation(Message):
        def __init__(self, delta: int) -> None:
            super().__init__()
            self.delta = delta

    class CommandCompletion(Message):
        pass

    class HistoryNavigation(Message):
        def __init__(self, delta: int) -> None:
            super().__init__()
            self.delta = delta

    class TranscriptNavigation(Message):
        def __init__(self, delta: int) -> None:
            super().__init__()
            self.delta = delta

    def set_text(self, text: str) -> None:
        """Replace the draft and leave the cursor at its natural editing edge."""

        self.load_text(text)
        self.move_cursor(self.document.end)

    async def _on_key(self, event: events.Key) -> None:
        command_input = self.text.startswith("/") and "\n" not in self.text
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self))
            return
        if event.key in {"shift+enter", "ctrl+enter"}:
            event.stop()
            event.prevent_default()
            start, end = self.selection
            self.replace("\n", start, end, maintain_selection_offset=False)
            return
        if command_input and event.key in {"up", "down"}:
            event.stop()
            event.prevent_default()
            self.post_message(self.CommandNavigation(-1 if event.key == "up" else 1))
            return
        if command_input and event.key == "tab":
            event.stop()
            event.prevent_default()
            self.post_message(self.CommandCompletion())
            return
        if event.key in {"ctrl+p", "ctrl+n"}:
            event.stop()
            event.prevent_default()
            self.post_message(self.HistoryNavigation(-1 if event.key == "ctrl+p" else 1))
            return
        if event.key in {"pageup", "pagedown"}:
            event.stop()
            event.prevent_default()
            self.post_message(self.TranscriptNavigation(-1 if event.key == "pageup" else 1))
            return
        await super()._on_key(event)
