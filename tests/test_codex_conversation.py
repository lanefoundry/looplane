from __future__ import annotations

from pathlib import Path

import pytest

from looplane.codex_conversation import IsolatedCodexConversation
from looplane.conversation_runtime import (
    RuntimeToolKind,
    RuntimeToolStatus,
    RuntimeTurnStatus,
    ToolCompletedEvent,
    ToolStartedEvent,
    TurnCompletedEvent,
)
from looplane.tools import ReviewablePatch


class FakeWorkspace:
    def __init__(self, root: Path, *, changed_paths=("src/app.py",)) -> None:
        self.root_path = root
        self.workspace_path = root / "workspace"
        self.workspace_path.mkdir()
        self.source_snapshot_warning = None
        self.changed_paths = changed_paths
        self.closed = False

    async def review(self, **_kwargs) -> ReviewablePatch:
        return ReviewablePatch(content="diff", changed_paths=self.changed_paths)

    async def source_invariant_postcheck(self):
        raise AssertionError("conversation cleanup must not inspect the source worktree")

    async def aclose(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, events, **_kwargs) -> None:
        self.items = list(events)
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def events(self):
        for event in self.items:
            yield event

    async def send_turn(self, _text):
        return "turn"

    async def respond_approval(self, *_args):
        return None

    async def interrupt(self, *_args):
        return None

    async def aclose(self) -> None:
        self.closed = True


async def test_completed_turn_is_audited_against_actual_patch(tmp_path: Path, monkeypatch) -> None:
    workspace = FakeWorkspace(tmp_path)
    events = (
        ToolStartedEvent(
            sequence=0,
            turn_id="turn",
            action_id="action",
            kind=RuntimeToolKind.FILE_CHANGE,
            tool_name="Update",
            effect="modify",
            path=str(workspace.workspace_path / "src/app.py"),
        ),
        ToolCompletedEvent(
            sequence=1,
            turn_id="turn",
            action_id="action",
            status=RuntimeToolStatus.COMPLETED,
            diff="diff",
        ),
        TurnCompletedEvent(
            sequence=2,
            turn_id="turn",
            status=RuntimeTurnStatus.COMPLETED,
        ),
    )
    monkeypatch.setattr(
        "looplane.codex_conversation.ConversationWorkspace.create",
        lambda *_args, **_kwargs: _async_value(workspace),
    )
    monkeypatch.setattr(
        "looplane.codex_conversation.CodexAppServerSession",
        lambda **_kwargs: FakeSession(events),
    )
    host = IsolatedCodexConversation(tmp_path)
    await host.start()
    received = [event async for event in host.events()]
    assert received[-1].status == RuntimeTurnStatus.COMPLETED
    await host.aclose()
    assert workspace.closed


async def test_multi_file_change_accounts_for_every_audited_path(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = FakeWorkspace(tmp_path, changed_paths=("src/app.py", "tests/test_app.py"))
    events = (
        ToolStartedEvent(
            sequence=0,
            turn_id="turn",
            action_id="action",
            kind=RuntimeToolKind.FILE_CHANGE,
            tool_name="Update",
            effect="modify",
            path=str(workspace.workspace_path / "src/app.py"),
            paths=(
                str(workspace.workspace_path / "src/app.py"),
                str(workspace.workspace_path / "tests/test_app.py"),
            ),
        ),
        ToolCompletedEvent(
            sequence=1,
            turn_id="turn",
            action_id="action",
            status=RuntimeToolStatus.COMPLETED,
            diff="diff",
        ),
        TurnCompletedEvent(sequence=2, turn_id="turn", status=RuntimeTurnStatus.COMPLETED),
    )
    monkeypatch.setattr(
        "looplane.codex_conversation.ConversationWorkspace.create",
        lambda *_args, **_kwargs: _async_value(workspace),
    )
    monkeypatch.setattr(
        "looplane.codex_conversation.CodexAppServerSession",
        lambda **_kwargs: FakeSession(events),
    )
    host = IsolatedCodexConversation(tmp_path)
    await host.start()

    received = [event async for event in host.events()]

    assert received[0].paths == ("src/app.py", "tests/test_app.py")
    assert received[-1].status == RuntimeTurnStatus.COMPLETED


async def test_command_workspace_cwd_is_not_treated_as_a_changed_path(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = FakeWorkspace(tmp_path, changed_paths=())
    events = (
        ToolStartedEvent(
            sequence=0,
            turn_id="turn",
            action_id="command",
            kind=RuntimeToolKind.COMMAND,
            tool_name="shell",
            effect="execute",
            path=str(workspace.workspace_path),
            summary="git status --short",
        ),
        ToolCompletedEvent(
            sequence=1,
            turn_id="turn",
            action_id="command",
            status=RuntimeToolStatus.COMPLETED,
            output="",
        ),
        TurnCompletedEvent(sequence=2, turn_id="turn", status=RuntimeTurnStatus.COMPLETED),
    )
    monkeypatch.setattr(
        "looplane.codex_conversation.ConversationWorkspace.create",
        lambda *_args, **_kwargs: _async_value(workspace),
    )
    monkeypatch.setattr(
        "looplane.codex_conversation.CodexAppServerSession",
        lambda **_kwargs: FakeSession(events),
    )
    host = IsolatedCodexConversation(tmp_path)
    await host.start()

    received = [event async for event in host.events()]

    assert received[0].kind == RuntimeToolKind.COMMAND
    assert received[0].path == str(workspace.workspace_path)
    assert received[-1].status == RuntimeTurnStatus.COMPLETED


async def test_unreported_patch_fails_terminal_audit(tmp_path: Path, monkeypatch) -> None:
    workspace = FakeWorkspace(tmp_path, changed_paths=("forbidden.py",))
    events = (
        TurnCompletedEvent(
            sequence=0,
            turn_id="turn",
            status=RuntimeTurnStatus.COMPLETED,
        ),
    )
    monkeypatch.setattr(
        "looplane.codex_conversation.ConversationWorkspace.create",
        lambda *_args, **_kwargs: _async_value(workspace),
    )
    monkeypatch.setattr(
        "looplane.codex_conversation.CodexAppServerSession",
        lambda **_kwargs: FakeSession(events),
    )
    host = IsolatedCodexConversation(tmp_path)
    await host.start()
    received = [event async for event in host.events()]
    assert received[-1].status == RuntimeTurnStatus.FAILED
    assert "do not match" in (received[-1].error or "")


async def test_session_constructor_failure_closes_workspace(tmp_path: Path, monkeypatch) -> None:
    workspace = FakeWorkspace(tmp_path)
    monkeypatch.setattr(
        "looplane.codex_conversation.ConversationWorkspace.create",
        lambda *_args, **_kwargs: _async_value(workspace),
    )
    monkeypatch.setattr(
        "looplane.codex_conversation.CodexAppServerSession",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("bad session")),
    )

    with pytest.raises(ValueError, match="bad session"):
        await IsolatedCodexConversation(tmp_path).start()

    assert workspace.closed


async def _async_value(value):
    return value
