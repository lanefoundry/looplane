"""Terminal tool widgets feature owner."""

from __future__ import annotations

from collections.abc import Callable

from rich.syntax import Syntax
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Collapsible, Static
from textual.widgets._collapsible import CollapsibleTitle


class ToolActionBlock(Vertical):
    """One tool action whose lifecycle is updated in place."""

    DEFAULT_CSS = """
    ToolActionBlock {
        layout: horizontal; height: auto; margin-bottom: 1; padding-left: 1;
    }
    ToolActionBlock .tool-glyph {
        width: 2; height: 1; color: $text-muted;
    }
    ToolActionBlock.running .tool-glyph,
    ToolActionBlock.waiting .tool-glyph { color: $warning; text-style: bold; }
    ToolActionBlock.completed .tool-glyph { color: $success; }
    ToolActionBlock.failed .tool-glyph,
    ToolActionBlock.cancelled .tool-glyph { color: $error; }
    ToolActionBlock .tool-content { width: 1fr; height: auto; }
    ToolActionBlock .tool-title { height: 1; text-style: bold; }
    ToolActionBlock .tool-detail {
        height: auto; max-height: 14; color: $text-muted; overflow-y: auto;
    }
    ToolActionBlock.verbose .tool-detail { max-height: 100vh; }
    """

    _COLLAPSED_DETAIL_LINES = 18
    _COLLAPSED_DETAIL_CHARS = 4_000

    def __init__(
        self,
        action_id: str,
        title: str,
        *,
        detail: str | None = None,
        detail_kind: str = "plain",
        collapsed_detail: str | None = None,
    ) -> None:
        super().__init__(classes="tool-action queued")
        self.action_id = action_id
        self.title = title
        self.detail = detail or ""
        self.detail_kind = detail_kind
        self.collapsed_detail = collapsed_detail
        self.verbose = False
        self.status = "queued"
        self.group: ToolGroupBlock | None = None

    def compose(self) -> ComposeResult:
        yield Static(self._glyph(self.status), classes="tool-glyph", markup=False)
        with Vertical(classes="tool-content"):
            yield Static(self.title, classes="tool-title", markup=False)
            yield Static(
                self._render_detail(self._visible_detail()),
                classes="tool-detail",
                markup=False,
            )

    def set_state(
        self,
        status: str,
        *,
        detail: str | None = None,
        detail_kind: str | None = None,
        collapsed_detail: str | None = None,
    ) -> None:
        self.status = status
        classes = ["tool-action", status]
        if self.has_class("verbose"):
            classes.append("verbose")
        self.set_classes(" ".join(classes))
        if detail is not None:
            self.detail = detail
        if detail_kind is not None:
            self.detail_kind = detail_kind
        if collapsed_detail is not None:
            self.collapsed_detail = collapsed_detail
        if self.query(".tool-glyph"):
            self.query_one(".tool-glyph", Static).update(self._glyph(status))
        if (detail is not None or collapsed_detail is not None) and self.query(".tool-detail"):
            detail_widget = self.query_one(".tool-detail", Static)
            visible_detail = self._visible_detail()
            detail_widget.update(self._render_detail(visible_detail))
            detail_widget.display = bool(visible_detail)
        if self.group is not None:
            self.group.action_updated()

    def set_title(self, title: str) -> None:
        self.title = title
        if self.query(".tool-title"):
            self.query_one(".tool-title", Static).update(title)

    def set_verbose(self, verbose: bool) -> None:
        self.verbose = verbose
        self.set_class(verbose, "verbose")
        if self.query(".tool-detail"):
            visible_detail = self._visible_detail()
            detail_widget = self.query_one(".tool-detail", Static)
            detail_widget.update(self._render_detail(visible_detail))
            detail_widget.display = bool(visible_detail)

    def _visible_detail(self) -> str:
        if self.verbose:
            return self.detail
        if self.collapsed_detail is not None:
            return self.collapsed_detail
        if self.detail_kind != "diff":
            return self.detail
        lines = self.detail.splitlines()
        preview = "\n".join(lines[: self._COLLAPSED_DETAIL_LINES])
        if len(preview) > self._COLLAPSED_DETAIL_CHARS:
            preview = preview[: self._COLLAPSED_DETAIL_CHARS]
        if preview != self.detail:
            preview = preview.rstrip() + "\n… Ctrl+O to expand"
        return preview

    def _render_detail(self, detail: str) -> str | Syntax:
        if self.detail_kind == "diff" and detail:
            return Syntax(
                detail,
                "diff",
                theme="ansi_dark",
                word_wrap=True,
                background_color="default",
            )
        return detail

    @staticmethod
    def _glyph(status: str) -> str:
        return {
            "queued": "○",
            "running": "●",
            "waiting": "?",
            "completed": "✓",
            "failed": "×",
            "denied": "−",
            "cancelled": "×",
        }.get(status, "•")


class ToolGroupBlock(Collapsible):
    """Keyboard-expandable group for consecutive read/search actions."""

    DEFAULT_CSS = """
    ToolGroupBlock { height: auto; margin-bottom: 1; padding-left: 1; }
    ToolGroupBlock > CollapsibleTitle { color: $text-muted; }
    ToolGroupBlock > CollapsibleTitle:focus { color: $accent; text-style: bold; }
    ToolGroupBlock > Contents { padding-left: 1; }
    """

    def __init__(
        self,
        first_action: ToolActionBlock,
        *,
        is_verbose: Callable[[], bool] = lambda: False,
    ) -> None:
        super().__init__(
            first_action,
            title="Exploring 1 item",
            collapsed=False,
            classes="tool-group",
        )
        self.actions: list[ToolActionBlock] = [first_action]
        first_action.group = self
        self._user_toggled = False
        self._is_verbose = is_verbose

    @on(CollapsibleTitle.Toggle)
    def _record_user_toggle(self, event: CollapsibleTitle.Toggle) -> None:
        # Once the user toggles manually, auto-expand/collapse stops overriding
        # this group for its lifetime; streaming updates only refresh the title.
        # Decorated (not naming-convention) handler: Textual dispatches naming
        # handlers once per MRO class, which would double-toggle with an override.
        self._user_toggled = True

    def add_action(self, action: ToolActionBlock) -> None:
        self.actions.append(action)
        action.group = self
        if not self._user_toggled:
            self.collapsed = False
        self._refresh_title()
        if self.query(Collapsible.Contents):
            self.query_one(Collapsible.Contents).mount(action)
        else:
            self._contents_list.append(action)

    def set_verbose(self, verbose: bool) -> None:
        """Global verbose toggle: latch user intent and force the requested state."""
        self._user_toggled = True
        self.collapsed = not verbose

    def action_updated(self) -> None:
        self._refresh_title()
        terminal = {"completed", "failed", "denied", "cancelled"}
        if (
            not self._user_toggled
            and not self._is_verbose()
            and self.actions
            and all(action.status in terminal for action in self.actions)
        ):
            self.collapsed = True

    def _refresh_title(self) -> None:
        done = sum(action.status == "completed" for action in self.actions)
        noun = "item" if len(self.actions) == 1 else "items"
        verb = "Explored" if done == len(self.actions) and self.actions else "Exploring"
        self.title = f"{verb} {len(self.actions)} {noun}"
