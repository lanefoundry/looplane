"""Terminal selectors feature owner."""

from __future__ import annotations

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from looplane.terminal.types import InlineSelectorOption as InlineSelectorOption


class InlineSelectorChoices(OptionList):
    """Option list whose Escape belongs to its inline selector, not the app."""

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            parent = self.parent
            if isinstance(parent, InlineSelectorBlock):
                parent.action_cancel()
            return
        await super()._on_key(event)


class InlineSelectorBlock(Vertical):
    """Claude-style keyboard selector rendered inside the transcript."""

    class Selected(Message):
        def __init__(self, selector: InlineSelectorBlock, value: str) -> None:
            super().__init__()
            self.selector = selector
            self.value = value

    class SecondarySelected(Message):
        def __init__(self, selector: InlineSelectorBlock, value: str) -> None:
            super().__init__()
            self.selector = selector
            self.value = value

    class Cancelled(Message):
        def __init__(self, selector: InlineSelectorBlock) -> None:
            super().__init__()
            self.selector = selector

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True, show=False)]
    DEFAULT_CSS = """
    InlineSelectorBlock {
        height: auto; max-height: 20; margin: 0 0 1 0; padding: 1 1 0 1;
        border-top: solid $accent; background: transparent;
    }
    InlineSelectorBlock .selector-title {
        height: 1; color: $accent; text-style: bold;
    }
    InlineSelectorBlock .selector-description {
        height: auto; margin-bottom: 1; color: $text-muted;
    }
    InlineSelectorBlock .selector-search {
        height: 1; margin: 0 0 1 0; padding: 0 1;
        background: transparent; border: none;
        color: $text;
    }
    InlineSelectorBlock .selector-search:focus {
        border: none;
    }
    InlineSelectorBlock OptionList {
        height: auto; max-height: 10; padding: 0;
        background: transparent; border: none; scrollbar-size: 1 0;
    }
    InlineSelectorBlock OptionList > .option-list--option { padding: 0 1; }
    InlineSelectorBlock OptionList > .option-list--option-highlighted,
    InlineSelectorBlock OptionList:focus > .option-list--option-highlighted {
        background: transparent; color: $accent; text-style: bold;
    }
    InlineSelectorBlock .selector-hint {
        height: 1; margin-top: 1; color: $text-muted;
    }
    """

    def __init__(
        self,
        *,
        kind: str,
        title: str,
        description: str,
        options: tuple[InlineSelectorOption, ...],
        hint: str = "↑/↓ to move · Enter to select · Esc to cancel",
    ) -> None:
        super().__init__(classes=f"inline-selector {kind}")
        if not options:
            raise ValueError("inline selector requires at least one option")
        self.kind = kind
        self.title = title
        self.description = description
        self.options = options
        self.hint = hint
        self._all_options = options
        self._filtered_indices: list[int] = list(range(len(options)))

    def compose(self) -> ComposeResult:
        yield Static(self.title, classes="selector-title", markup=False)
        yield Static(self.description, classes="selector-description", markup=False)
        yield Input(placeholder="Type to filter…", classes="selector-search")
        yield InlineSelectorChoices(
            *(
                Option(self._prompt_filtered(i, i, highlighted=False), id=str(i))
                for i in range(len(self._all_options))
            ),
            classes="selector-options",
            compact=True,
        )
        yield Static(self.hint, classes="selector-hint", markup=False)

    def _prompt(self, index: int, *, highlighted: bool) -> str:
        """Format a prompt using the original full options list (legacy path)."""
        option = self.options[index]
        pointer = "›" if highlighted else " "
        selected = " ✓" if option.selected else ""
        suffix = f" · {option.description}" if option.description else ""
        return f"{pointer} {option.label}{selected}{suffix}"

    def _prompt_filtered(self, display_index: int, orig_index: int, *, highlighted: bool) -> str:
        """Format a prompt for the filtered view using an original-list index."""
        option = self._all_options[orig_index]
        pointer = "›" if highlighted else " "
        selected = " ✓" if option.selected else ""
        suffix = f" · {option.description}" if option.description else ""
        return f"{pointer} {option.label}{selected}{suffix}"

    def on_mount(self) -> None:
        choices = self.query_one(".selector-options", OptionList)
        initial = next(
            (idx for idx, option in enumerate(self._all_options) if option.selected),
            0,
        )
        choices.highlighted = initial
        self._sync_filtered_prompts(initial)
        # Focus the search Input so typing starts filtering immediately.
        self.query_one(".selector-search", Input).focus()

    def _secondary_select_highlighted(self) -> None:
        """Post a SecondarySelected for the currently highlighted option."""
        choices = self.query_one(".selector-options", OptionList)
        if choices.highlighted is None:
            return
        idx = choices.highlighted
        if idx < len(self._filtered_indices):
            orig = self._filtered_indices[idx]
            self.post_message(self.SecondarySelected(self, self._all_options[orig].value))

    async def _on_key(self, event: events.Key) -> None:
        """Forward navigation keys from the Input to the OptionList."""
        search = self.query_one(".selector-search", Input)
        if not search.has_focus:
            return
        if event.key in ("up", "down", "enter", "ctrl+f"):
            choices = self.query_one(".selector-options", OptionList)
            if event.key == "up":
                choices.action_cursor_up()
            elif event.key == "down":
                choices.action_cursor_down()
            elif event.key == "enter" and choices.highlighted is not None:
                choices.action_select()
            elif event.key == "ctrl+f":
                self._secondary_select_highlighted()
            event.prevent_default()
            event.stop()

    @on(Input.Changed, ".selector-search")
    def filter_options(self, event: Input.Changed) -> None:
        query = event.value.strip().lower()
        choices = self.query_one(".selector-options", OptionList)
        if not query:
            self._filtered_indices = list(range(len(self._all_options)))
        else:
            self._filtered_indices = [
                i
                for i, opt in enumerate(self._all_options)
                if query in opt.label.lower() or query in opt.value.lower()
            ]
        choices.clear_options()
        for pos, orig_idx in enumerate(self._filtered_indices):
            choices.add_option(
                Option(
                    self._prompt_filtered(pos, orig_idx, highlighted=False),
                    id=str(orig_idx),
                )
            )
        if self._filtered_indices:
            choices.highlighted = 0
            self._sync_filtered_prompts(0)

    def set_options(self, options: tuple[InlineSelectorOption, ...]) -> None:
        """Swap choices in place (e.g. a background model-catalog refresh landing)."""

        if not options:
            raise ValueError("inline selector requires at least one option")
        self._all_options = options
        self.options = options
        # Re-apply the current filter query against the new options.
        try:
            search = self.query_one(".selector-search", Input)
            query = search.value.strip().lower()
        except Exception:  # noqa: BLE001
            query = ""
        choices = self.query_one(".selector-options", OptionList)
        if not query:
            self._filtered_indices = list(range(len(self._all_options)))
        else:
            self._filtered_indices = [
                i
                for i, opt in enumerate(self._all_options)
                if query in opt.label.lower() or query in opt.value.lower()
            ]
        choices.clear_options()
        for pos, orig_idx in enumerate(self._filtered_indices):
            choices.add_option(
                Option(
                    self._prompt_filtered(pos, orig_idx, highlighted=False),
                    id=str(orig_idx),
                )
            )
        highlighted = min(choices.highlighted or 0, max(len(self._filtered_indices) - 1, 0))
        if self._filtered_indices:
            choices.highlighted = highlighted
            self._sync_filtered_prompts(highlighted)

    def _sync_prompts(self, highlighted: int | None) -> None:
        self._sync_filtered_prompts(highlighted)

    def _sync_filtered_prompts(self, highlighted: int | None) -> None:
        choices = self.query_one(".selector-options", OptionList)
        for pos, orig_idx in enumerate(self._filtered_indices):
            choices.replace_option_prompt_at_index(
                pos,
                self._prompt_filtered(pos, orig_idx, highlighted=pos == highlighted),
            )

    @on(OptionList.OptionHighlighted, ".selector-options")
    def highlight_choice(self, event: OptionList.OptionHighlighted) -> None:
        self._sync_filtered_prompts(event.option_index)

    @on(OptionList.OptionSelected, ".selector-options")
    def choose(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is None:
            return
        index = int(event.option.id)  # original index in _all_options
        self.post_message(self.Selected(self, self._all_options[index].value))

    def action_cancel(self) -> None:
        self.post_message(self.Cancelled(self))
