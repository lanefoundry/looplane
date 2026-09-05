"""Terminal io command services."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer

from looplane.commands.ports import CommandServices

MIN_TUI_TERMINAL_HEIGHT = 16


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def _terminal_supports_tui(*, services: CommandServices) -> bool:
    return (
        services.stdin_is_tty()
        and sys.stdout.isatty()
        and os.environ.get("TERM", "").lower() != "dumb"
        and (os.environ.get("LOOPLANE_NO_TUI") or os.environ.get("PCA_NO_TUI")) != "1"
    )


def _terminal_size() -> os.terminal_size | None:
    """Return the active output terminal size when it can be queried."""

    try:
        return os.get_terminal_size(sys.stdout.fileno())
    except (AttributeError, OSError, ValueError):
        return None


def _validate_tui_terminal_size(*, services: CommandServices) -> None:
    """Keep a fixed-height full-screen layout out of unusably short terminals."""

    size = services.terminal_size()
    if size is not None and size.lines < MIN_TUI_TERMINAL_HEIGHT:
        raise typer.BadParameter(
            "terminal is too short for the interactive UI "
            f"({size.columns}x{size.lines}; at least {MIN_TUI_TERMINAL_HEIGHT} rows required). "
            "Resize the terminal or use --plain."
        )


def _show_context_header(*, repository: Path, provider: str, model: str) -> None:
    typer.secho("\nlooplane", bold=True, nl=False)
    typer.echo(f"  ·  {provider}/{model}  ·  {repository.name}")
