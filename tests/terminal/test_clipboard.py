from __future__ import annotations

import subprocess

from looplane.tui_clipboard import copy_with_native_command


def test_copy_with_native_command_uses_pbcopy_on_macos(monkeypatch) -> None:
    calls: list[tuple[tuple[str, ...], str]] = []

    monkeypatch.setattr("looplane.tui_clipboard.shutil.which", lambda command: command)

    def run(command, *, input, **_kwargs):
        calls.append((command, input))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("looplane.tui_clipboard.subprocess.run", run)

    assert copy_with_native_command("selected text", platform="darwin") is True
    assert calls == [(('pbcopy',), "selected text")]


def test_copy_with_native_command_falls_back_when_no_tool_exists(monkeypatch) -> None:
    monkeypatch.setattr("looplane.tui_clipboard.shutil.which", lambda _command: None)

    assert copy_with_native_command("selected text", platform="linux", environ={}) is False


def test_copy_with_native_command_skips_remote_host_clipboard(monkeypatch) -> None:
    commands: list[str] = []
    monkeypatch.setattr(
        "looplane.tui_clipboard.shutil.which",
        lambda command: commands.append(command) or command,
    )

    assert (
        copy_with_native_command(
            "selected text",
            platform="linux",
            environ={"SSH_CONNECTION": "client server"},
        )
        is False
    )
    assert commands == []
