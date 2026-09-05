"""Terminal selectors feature owner."""

from __future__ import annotations

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import OptionList, Static
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

    class Cancelled(Message):
        def __init__(self, selector: InlineSelectorBlock) -> None:
            super().__init__()
            self.selector = selector

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True, show=False)]
    DEFAULT_CSS = """
    InlineSelectorBlock {
        height: auto; max-height: 18; margin: 0 0 1 0; padding: 1 1 0 1;
        border-top: solid $accent; background: transparent;
    }
    InlineSelectorBlock .selector-title {
        height: 1; color: $accent; text-style: bold;
    }
    InlineSelectorBlock .selector-description {
        height: auto; margin-bottom: 1; color: $text-muted;
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

    def compose(self) -> ComposeResult:
        yield Static(self.title, classes="selector-title", markup=False)
        yield Static(self.description, classes="selector-description", markup=False)
        yield InlineSelectorChoices(
            *(
                Option(self._prompt(index, highlighted=False), id=str(index))
                for index in range(len(self.options))
            ),
            classes="selector-options",
            compact=True,
        )
        yield Static(self.hint, classes="selector-hint", markup=False)

    def _prompt(self, index: int, *, highlighted: bool) -> str:
        option = self.options[index]
        pointer = "›" if highlighted else " "
        selected = " ✓" if option.selected else ""
        suffix = f" · {option.description}" if option.description else ""
        return f"{pointer} {index + 1}. {option.label}{selected}{suffix}"

    def on_mount(self) -> None:
        choices = self.query_one(".selector-options", OptionList)
        choices.highlighted = next(
            (index for index, option in enumerate(self.options) if option.selected),
            0,
        )
        self._sync_prompts(choices.highlighted)
        choices.focus()

    def set_options(self, options: tuple[InlineSelectorOption, ...]) -> None:
        """Swap choices in place (e.g. a background model-catalog refresh landing)."""

        if not options:
            raise ValueError("inline selector requires at least one option")
        choices = self.query_one(".selector-options", OptionList)
        highlighted = min(choices.highlighted or 0, len(options) - 1)
        self.options = options
        choices.clear_options()
        choices.add_options(
            [
                Option(self._prompt(index, highlighted=False), id=str(index))
                for index in range(len(options))
            ]
        )
        choices.highlighted = highlighted
        self._sync_prompts(highlighted)

    def _sync_prompts(self, highlighted: int | None) -> None:
        choices = self.query_one(".selector-options", OptionList)
        for index in range(len(self.options)):
            choices.replace_option_prompt_at_index(
                index,
                self._prompt(index, highlighted=index == highlighted),
            )

    @on(OptionList.OptionHighlighted, ".selector-options")
    def highlight_choice(self, event: OptionList.OptionHighlighted) -> None:
        self._sync_prompts(event.option_index)

    @on(OptionList.OptionSelected, ".selector-options")
    def choose(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is None:
            return
        index = int(event.option.id)
        self.post_message(self.Selected(self, self.options[index].value))

    def action_cancel(self) -> None:
        self.post_message(self.Cancelled(self))
