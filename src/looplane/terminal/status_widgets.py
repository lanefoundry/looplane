"""Terminal status widgets feature owner."""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic
from typing import Any

from rich.text import Text
from textual.widgets import Static

from looplane.terminal.status import format_token_count as format_token_count
from looplane.terminal.types import LoadingPhase as LoadingPhase


class RuntimeLoadingIndicator(Static):
    """Non-interactive looplane swimming indicator."""

    _FRAMES = (
        "[🦦≋≋≋] ",
        "[≋🦦≋≋] ",
        "[≋≋🦦≋] ",
        "[≋≋≋🦦] ",
        "[≋≋🦦≋] ",
        "[≋🦦≋≋] ",
    )
    _STATIC_FRAME = "[≋🦦≋≋] "
    _CADENCE = 0.20

    def __init__(self, *, id: str) -> None:
        super().__init__("", id=id, markup=False)
        self.phase: LoadingPhase | None = None
        self._phase_started_at = monotonic()

    def set_phase(self, phase: LoadingPhase | None) -> None:
        if phase != self.phase:
            self.phase = phase
            self._phase_started_at = monotonic()
        self.display = phase is not None
        if phase is None or self.app.animation_level == "none":
            self.auto_refresh = None
        else:
            self.auto_refresh = self._CADENCE
        self.refresh()

    def render(self) -> Text:
        if self.phase is None:
            return Text("")
        if self.app.animation_level == "none":
            return Text(self._STATIC_FRAME)
        frame = int((monotonic() - self._phase_started_at) / self._CADENCE) % len(self._FRAMES)
        return Text(self._FRAMES[frame])


class RuntimeMetrics(Static):
    """Persistent turn metrics: tokens, context pressure, elapsed time."""

    _CONTEXT_WARNING_PERCENT = 70.0
    _CONTEXT_CRITICAL_PERCENT = 90.0

    def __init__(
        self, *, id: str, token_formatter: Callable[[int], str] = format_token_count,
    ) -> None:
        super().__init__("", id=id, markup=False)
        self._token_formatter = token_formatter

    def set_metrics(
        self,
        *,
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        context_percent: float | None = None,
        elapsed_seconds: float | None = None,
        stream_output_tokens: int | None = None,
        running_tools: int | None = None,
        queued_prompts: int | None = None,
    ) -> None:
        text = Text()
        if model:
            text.append(model, style="dim")
        if running_tools:
            if text.plain:
                text.append(" · ", style="dim")
            text.append(f"⚙{running_tools}", style="dim")
        if queued_prompts:
            if text.plain:
                text.append(" · ", style="dim")
            text.append(f"☰{queued_prompts} queued", style="dim")
        if stream_output_tokens is not None:
            if text.plain:
                text.append(" · ", style="dim")
            text.append(f"↓~{self._token_formatter(stream_output_tokens)}", style="dim")
        elif input_tokens is not None:
            if text.plain:
                text.append(" · ", style="dim")
            text.append(f"↑{self._token_formatter(input_tokens)}", style="dim")
            text.append(f" ↓{self._token_formatter(output_tokens or 0)}", style="dim")
        if context_percent is not None:
            if text.plain:
                text.append(" · ", style="dim")
            if context_percent >= self._CONTEXT_CRITICAL_PERCENT:
                style = "bold red"
            elif context_percent >= self._CONTEXT_WARNING_PERCENT:
                style = "yellow"
            else:
                style = "dim"
            text.append(f"ctx {context_percent:.0f}%", style=style)
        if elapsed_seconds is not None:
            if text.plain:
                text.append(" · ", style="dim")
            text.append(f"{elapsed_seconds:.0f}s", style="dim")
        self.update(text)


class RuntimeStatus(Static):
    """Status text with a restrained Claude-style loading glimmer."""

    _CADENCE = RuntimeLoadingIndicator._CADENCE
    _GLIMMER_FRAMES = len(RuntimeLoadingIndicator._FRAMES)
    _GLIMMER_WIDTH = 3
    _ELAPSED_DELAY = 16.0

    def __init__(self, content: str = "", *, id: str) -> None:
        self.loading_phase: LoadingPhase | None = None
        self.loading_label: str | None = None
        self._loading_started_at = monotonic()
        super().__init__(content, id=id, markup=False)

    def update(self, content: Any = "") -> None:
        self.loading_phase = None
        self.loading_label = None
        self.auto_refresh = None
        super().update(content)

    def set_loading(self, label: str | None, phase: LoadingPhase | None) -> None:
        if phase != self.loading_phase or label != self.loading_label:
            self._loading_started_at = monotonic()
        self.loading_phase = phase
        self.loading_label = label
        if label is None or phase is None or self.app.animation_level == "none":
            self.auto_refresh = None
        else:
            self.auto_refresh = self._CADENCE
        self.refresh()

    def render(self) -> Text:
        if self.loading_label is None or self.loading_phase is None:
            return super().render()

        label = self.loading_label
        text = Text(label, style="dim")
        elapsed = monotonic() - self._loading_started_at
        if self.app.animation_level != "none" and label:
            frame = int(elapsed / self._CADENCE) % self._GLIMMER_FRAMES
            center = round(frame * (len(label) - 1) / (self._GLIMMER_FRAMES - 1))
            radius = self._GLIMMER_WIDTH // 2
            start = max(0, center - radius)
            end = min(len(label), center + radius + 1)
            primary = self.app.get_css_variables().get("primary", "")
            text.stylize(f"not dim bold {primary}".strip(), start, end)
        if elapsed >= self._ELAPSED_DELAY:
            text.append(f" ({int(elapsed)}s", style="dim")
            text.append(" · esc to interrupt)", style="dim")
        else:
            text.append(" (esc to interrupt)", style="dim")
        return text
