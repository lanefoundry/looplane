from __future__ import annotations

from pathlib import Path

import pytest

from looplane.claude_conversation import IsolatedClaudeConversation
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
    def __init__(self, root: Path, *, review_error: Exception | None = None) -> None:
        self.root_path = root
        self.workspace_path = root / "workspace"
        self.workspace_path.mkdir()
        self.source_snapshot_warning = None
        self.review_error = review_error
        self.closed = False

    async def review(self, **_kwargs) -> ReviewablePatch:
        if self.review_error is not None:
            raise self.review_error
        return ReviewablePatch(
            content="diff --git a/src/app.py b/src/app.py", changed_paths=("src/app.py",)
        )

    async def source_invariant_postcheck(self):
        raise AssertionError("conversation cleanup must not inspect the source worktree")

    async def aclose(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, events, **_kwargs) -> None:
        self.items = list(events)

    async def start(self) -> None:
        return None

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
        return None


async def test_file_change_gets_pca_audited_relative_path_and_diff(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = FakeWorkspace(tmp_path)
    events = (
        ToolStartedEvent(
            sequence=0,
            turn_id="turn",
            action_id="action",
            kind=RuntimeToolKind.FILE_CHANGE,
            tool_name="Edit",
            effect="modify",
            path=str(workspace.workspace_path / "src/app.py"),
        ),
        ToolCompletedEvent(
            sequence=1,
            turn_id="turn",
            action_id="action",
            status=RuntimeToolStatus.COMPLETED,
        ),
        TurnCompletedEvent(sequence=2, turn_id="turn", status=RuntimeTurnStatus.COMPLETED),
    )
    monkeypatch.setattr(
        "looplane.claude_conversation.ConversationWorkspace.create",
        lambda *_args, **_kwargs: _async_value(workspace),
    )
    monkeypatch.setattr(
        "looplane.claude_conversation.ClaudeAgentSession",
        lambda **_kwargs: FakeSession(events),
    )
    host = IsolatedClaudeConversation(tmp_path)
    await host.start()

    received = [event async for event in host.events()]

    assert received[0].path == "src/app.py"
    assert received[1].diff == "diff --git a/src/app.py b/src/app.py"
    assert received[-1].status == RuntimeTurnStatus.COMPLETED
    await host.aclose()
    assert workspace.closed


async def test_workspace_review_failure_turns_terminal_into_failure(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = FakeWorkspace(tmp_path, review_error=RuntimeError("unsafe patch"))
    events = (TurnCompletedEvent(sequence=0, turn_id="turn", status=RuntimeTurnStatus.COMPLETED),)
    monkeypatch.setattr(
        "looplane.claude_conversation.ConversationWorkspace.create",
        lambda *_args, **_kwargs: _async_value(workspace),
    )
    monkeypatch.setattr(
        "looplane.claude_conversation.ClaudeAgentSession",
        lambda **_kwargs: FakeSession(events),
    )
    host = IsolatedClaudeConversation(tmp_path)
    await host.start()

    received = [event async for event in host.events()]

    assert received[-1].status == RuntimeTurnStatus.FAILED
    assert "unsafe patch" in (received[-1].error or "")


async def test_unreported_patch_fails_terminal_audit(tmp_path: Path, monkeypatch) -> None:
    workspace = FakeWorkspace(tmp_path)
    events = (TurnCompletedEvent(sequence=0, turn_id="turn", status=RuntimeTurnStatus.COMPLETED),)
    monkeypatch.setattr(
        "looplane.claude_conversation.ConversationWorkspace.create",
        lambda *_args, **_kwargs: _async_value(workspace),
    )
    monkeypatch.setattr(
        "looplane.claude_conversation.ClaudeAgentSession",
        lambda **_kwargs: FakeSession(events),
    )
    host = IsolatedClaudeConversation(tmp_path)
    await host.start()

    received = [event async for event in host.events()]

    assert received[-1].status == RuntimeTurnStatus.FAILED
    assert "do not match" in (received[-1].error or "")


async def test_read_tool_never_receives_workspace_diff(tmp_path: Path, monkeypatch) -> None:
    workspace = FakeWorkspace(tmp_path)
    events = (
        ToolStartedEvent(
            sequence=0,
            turn_id="turn",
            action_id="read",
            kind=RuntimeToolKind.READ,
            tool_name="Read",
            effect="read",
            path=str(workspace.workspace_path / "src/app.py"),
        ),
        ToolCompletedEvent(
            sequence=1,
            turn_id="turn",
            action_id="read",
            status=RuntimeToolStatus.COMPLETED,
        ),
        TurnCompletedEvent(sequence=2, turn_id="turn", status=RuntimeTurnStatus.COMPLETED),
    )
    monkeypatch.setattr(
        "looplane.claude_conversation.ConversationWorkspace.create",
        lambda *_args, **_kwargs: _async_value(workspace),
    )
    monkeypatch.setattr(
        "looplane.claude_conversation.ClaudeAgentSession",
        lambda **_kwargs: FakeSession(events),
    )
    host = IsolatedClaudeConversation(tmp_path)
    await host.start()

    received = [event async for event in host.events()]

    assert received[1].diff is None
    assert received[-1].status == RuntimeTurnStatus.FAILED


async def test_session_constructor_failure_closes_workspace(tmp_path: Path, monkeypatch) -> None:
    workspace = FakeWorkspace(tmp_path)
    monkeypatch.setattr(
        "looplane.claude_conversation.ConversationWorkspace.create",
        lambda *_args, **_kwargs: _async_value(workspace),
    )
    monkeypatch.setattr(
        "looplane.claude_conversation.ClaudeAgentSession",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("bad session")),
    )

    with pytest.raises(ValueError, match="bad session"):
        await IsolatedClaudeConversation(tmp_path).start()

    assert workspace.closed


async def _async_value(value):
    return value
