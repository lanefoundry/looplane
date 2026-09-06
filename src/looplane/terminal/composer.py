"""Terminal composer feature owner."""

from __future__ import annotations

from textual import events
from textual.message import Message
from textual.widgets import TextArea

from looplane.attachment_manager import Attachment


class MessageComposer(TextArea):
    """Multiline composer with submit, command navigation, and history messages."""

    class Submitted(Message):
        def __init__(self, composer: MessageComposer) -> None:
            super().__init__()
            self.composer = composer
            self.text = composer.text
            self.attachments: tuple[Attachment, ...] = tuple(composer._pending_attachments)

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

    class AttachmentsChanged(Message):
        """Posted when the pending attachment list changes."""

        def __init__(self, count: int) -> None:
            super().__init__()
            self.count = count

    class ClipboardImagePaste(Message):
        """Request the app to read an image from the clipboard."""

        pass

    class FileDropped(Message):
        """A file path was pasted (likely via terminal drag-and-drop)."""

        def __init__(self, paths: tuple[str, ...]) -> None:
            super().__init__()
            self.paths = paths

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._pending_attachments: list[Attachment] = []
        self._session_attachments: list[Attachment] = []

    @property
    def pending_attachments(self) -> tuple[Attachment, ...]:
        return tuple(self._pending_attachments)

    @property
    def session_attachments(self) -> tuple[Attachment, ...]:
        return tuple(self._session_attachments)

    @property
    def all_attachments(self) -> tuple[Attachment, ...]:
        return (*self._session_attachments, *self._pending_attachments)

    def add_attachment(self, attachment: Attachment) -> None:
        self._pending_attachments.append(attachment)
        self.post_message(self.AttachmentsChanged(len(self._pending_attachments)))

    def add_session_attachment(self, attachment: Attachment) -> None:
        self._session_attachments.append(attachment)
        self.post_message(self.AttachmentsChanged(len(self._pending_attachments)))

    def remove_attachment(self, name: str) -> bool:
        for lst in (self._pending_attachments, self._session_attachments):
            for i, att in enumerate(lst):
                if att.name == name:
                    lst.pop(i)
                    self.post_message(self.AttachmentsChanged(len(self._pending_attachments)))
                    return True
        return False

    def clear_pending_attachments(self) -> None:
        self._pending_attachments.clear()
        self.post_message(self.AttachmentsChanged(0))

    def clear_all_attachments(self) -> None:
        self._pending_attachments.clear()
        self._session_attachments.clear()
        self.post_message(self.AttachmentsChanged(0))

    def set_text(self, text: str) -> None:
        """Replace the draft and leave the cursor at its natural editing edge."""

        self.load_text(text)
        self.move_cursor(self.document.end)

    def _on_paste(self, event: events.Paste) -> None:
        """Intercept terminal paste to detect drag-and-dropped file paths."""
        text = event.text
        if not text or not text.strip():
            return
        lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
        file_paths: list[str] = []
        for line in lines:
            cleaned = line.strip("'\"")
            if not cleaned:
                continue
            try:
                from pathlib import Path as _P

                p = _P(cleaned).expanduser()
                if p.is_file():
                    file_paths.append(str(p))
                    continue
            except (ValueError, RuntimeError, OSError):
                pass
        if file_paths:
            event.stop()
            event.prevent_default()
            self.post_message(self.FileDropped(tuple(file_paths)))
        # If not file paths, let Textual's default paste handling run

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
        if event.key == "up" and self.cursor_location[0] == 0:
            event.stop()
            event.prevent_default()
            self.post_message(self.HistoryNavigation(-1))
            return
        if event.key == "down" and self.cursor_location[0] >= self.document.line_count - 1:
            event.stop()
            event.prevent_default()
            self.post_message(self.HistoryNavigation(1))
            return
        if event.key in {"pageup", "pagedown"}:
            event.stop()
            event.prevent_default()
            self.post_message(self.TranscriptNavigation(-1 if event.key == "pageup" else 1))
            return
        if event.key == "ctrl+v":
            self.post_message(self.ClipboardImagePaste())
            return
        await super()._on_key(event)
