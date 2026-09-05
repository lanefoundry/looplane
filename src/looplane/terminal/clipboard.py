"""Selection and native clipboard helpers for the Textual interface."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from typing import Protocol

from textual.widgets import TextArea


class SelectionScreen(Protocol):
    def get_selected_text(self) -> str | None: ...


def selected_text_for_copy(focused: object, screen: SelectionScreen) -> str:
    """Prefer an editor selection, then fall back to the rendered transcript."""

    if isinstance(focused, TextArea) and focused.selected_text:
        return focused.selected_text
    return screen.get_selected_text() or ""


def copy_with_native_command(
    text: str,
    *,
    platform: str = sys.platform,
    environ: Mapping[str, str] = os.environ,
) -> bool:
    """Best-effort local clipboard copy for terminals that lack OSC 52 support."""

    if any(environ.get(name) for name in ("SSH_TTY", "SSH_CONNECTION", "MOSH_CONNECTION")):
        # A native command would copy on the remote host. Textual's OSC 52 path
        # instead targets the clipboard owned by the user's local terminal.
        return False

    commands: tuple[tuple[str, ...], ...]
    if platform == "darwin":
        commands = (("pbcopy",),)
    elif platform == "win32" or environ.get("WSL_DISTRO_NAME"):
        commands = (
            (
                "powershell.exe",
                "-NonInteractive",
                "-NoProfile",
                "-Command",
                "[Console]::InputEncoding = [Text.Encoding]::UTF8; "
                "Set-Clipboard -Value ([Console]::In.ReadToEnd())",
            ),
        )
    elif environ.get("WAYLAND_DISPLAY"):
        commands = (("wl-copy",), ("xclip", "-selection", "clipboard"))
    else:
        commands = (
            ("xclip", "-selection", "clipboard"),
            ("xsel", "--clipboard", "--input"),
        )

    for command in commands:
        if shutil.which(command[0]) is None:
            continue
        try:
            completed = subprocess.run(
                command,
                input=text,
                text=True,
                capture_output=True,
                timeout=1,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode == 0:
            return True
    return False
