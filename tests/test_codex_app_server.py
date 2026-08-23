from __future__ import annotations

import asyncio
import os
from pathlib import Path
from textwrap import dedent

import pytest

from rivumi.approvals import ApprovalDecision, ToolEffect
from rivumi.codex_app_server import CodexAppServerSession
from rivumi.conversation_runtime import (
    ActionPreviewUpdatedEvent,
    ApprovalRequestedEvent,
    ApprovalResolvedEvent,
    CompactionCompletedEvent,
    CompactionStartedEvent,
    ContextUsageUpdatedEvent,
    ConversationProtocolError,
    NoticeEvent,
    RuntimeToolKind,
    RuntimeToolStatus,
    RuntimeTurnStatus,
    TextDeltaEvent,
    ToolCompletedEvent,
    ToolOutputDeltaEvent,
    ToolStartedEvent,
    TurnCompletedEvent,
    TurnStartedEvent,
)
from rivumi.runtime_semantics import ContextTelemetryAccuracy, ProposedChangeKind


def _fake_codex(
    tmp_path: Path,
    behavior: str = "normal",
    *,
    expected_sandbox: str = "read-only",
) -> Path:
    executable = tmp_path / f"fake-codex-{behavior}"
    executable.write_text(
        dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            import sys
            import time

            behavior = __BEHAVIOR__
            assert "hooks.state={{}}" in sys.argv
            assert sys.argv.count("--disable") == 3
            assert {{"hooks", "plugins", "remote_plugin"}} <= set(sys.argv)
            thread_id = "vendor-thread-secret"
            turn_number = 0
            active_turn = None

            def emit(value):
                sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\\n")
                sys.stdout.flush()

            for line in sys.stdin:
                frame = json.loads(line)
                method = frame.get("method")
                request_id = frame.get("id")
                if method == "initialize":
                    capabilities = frame["params"]["capabilities"]
                    assert frame["params"]["clientInfo"] == {{
                        "name": "rivumi", "title": "Rivumi", "version": "0.1.0"
                    }}
                    assert capabilities["experimentalApi"] is True
                    assert (
                        "remoteControl/status/changed"
                        in capabilities["optOutNotificationMethods"]
                    )
                    assert (
                        "thread/tokenUsage/updated"
                        not in capabilities["optOutNotificationMethods"]
                    )
                    assert "turn/diff/updated" not in capabilities["optOutNotificationMethods"]
                    emit({{"id": request_id, "result": {{"userAgent": "fake"}}}})
                elif method == "initialized":
                    pass
                elif method == "thread/start":
                    assert frame["params"]["ephemeral"] is True
                    assert frame["params"]["sandbox"] == __SANDBOX__
                    assert frame["params"]["approvalPolicy"] == "untrusted"
                    emit({{"id": request_id, "result": {{
                        "thread": {{"id": thread_id}}, "model": "automatic"
                    }}}})
                    if behavior == "remote_status":
                        emit({{"method": "remoteControl/status/changed", "params": {{
                            "status": "disconnected"
                        }}}})
                elif method == "turn/start":
                    turn_number += 1
                    active_turn = f"vendor-turn-{{turn_number}}"
                    emit({{"id": request_id, "result": {{
                        "turn": {{"id": active_turn, "items": [], "status": "inProgress"}}
                    }}}})
                    emit({{"method": "turn/started", "params": {{
                        "threadId": thread_id,
                        "turn": {{"id": active_turn, "items": [], "status": "inProgress"}}
                    }}}})
                    if behavior == "warning":
                        emit({{"method": "warning", "params": {{
                            "threadId": thread_id, "message": "bounded warning"
                        }}}})
                    if behavior == "unknown":
                        emit({{"method": "future/unsafe", "params": {{}}}})
                    elif behavior == "oversize":
                        emit({{"method": "item/agentMessage/delta", "params": {{
                            "threadId": thread_id, "turnId": active_turn,
                            "itemId": "vendor-message", "delta": "x" * 10000
                        }}}})
                    elif turn_number == 1 and behavior != "interrupt":
                        emit({{"method": "item/agentMessage/delta", "params": {{
                            "threadId": thread_id, "turnId": active_turn,
                            "itemId": "vendor-message", "delta": "Working"
                        }}}})
                        item = {{
                            "type": "commandExecution", "id": "vendor-action-secret",
                            "command": "pytest -q", "cwd": os.getcwd(),
                            "status": "inProgress"
                        }}
                        emit({{"method": "item/started", "params": {{
                            "threadId": thread_id, "turnId": active_turn,
                            "startedAtMs": 1, "item": item
                        }}})
                        emit({{"method": "item/commandExecution/outputDelta", "params": {{
                            "threadId": thread_id, "turnId": active_turn,
                            "itemId": "vendor-action-secret", "delta": "one passed"
                        }}})
                        emit({{"method": "item/commandExecution/requestApproval",
                              "id": "vendor-approval-secret", "params": {{
                            "threadId": thread_id, "turnId": active_turn,
                            "itemId": "vendor-action-secret", "startedAtMs": 2,
                            "command": "pytest -q", "cwd": os.getcwd(),
                            "availableDecisions": ["accept", "decline", "cancel"]
                        }}})
                    elif behavior != "interrupt":
                        emit({{"method": "item/agentMessage/delta", "params": {{
                            "threadId": thread_id, "turnId": active_turn,
                            "itemId": "vendor-message-2", "delta": "Second answer"
                        }}}})
                        emit({{"method": "turn/completed", "params": {{
                            "threadId": thread_id,
                            "turn": {{"id": active_turn, "items": [], "status": "completed"}}
                        }}}})
                elif method == "thread/compact/start":
                    active_turn = "vendor-compact-turn"
                    emit({{"id": request_id, "result": {{}}}})
                    emit({{"method": "turn/started", "params": {{
                        "threadId": thread_id,
                        "turn": {{"id": active_turn, "items": [], "status": "inProgress"}}
                    }}}})
                    item = {{"type": "contextCompaction", "id": "vendor-compaction"}}
                    emit({{"method": "item/started", "params": {{
                        "threadId": thread_id, "turnId": active_turn,
                        "startedAtMs": 4, "item": item
                    }}})
                    emit({{"method": "item/completed", "params": {{
                        "threadId": thread_id, "turnId": active_turn,
                        "completedAtMs": 5, "item": item
                    }}})
                    emit({{"method": "thread/compacted", "params": {{
                        "threadId": thread_id, "turnId": active_turn
                    }}})
                    emit({{"method": "turn/completed", "params": {{
                        "threadId": thread_id,
                        "turn": {{"id": active_turn, "items": [], "status": "completed"}}
                    }}}})
                elif request_id == "vendor-approval-secret":
                    assert frame["result"] == {{"decision": "accept"}}
                    item = {{
                        "type": "commandExecution", "id": "vendor-action-secret",
                        "command": "pytest -q", "cwd": os.getcwd(),
                        "status": "completed", "exitCode": 0,
                        "aggregatedOutput": "one passed"
                    }}
                    emit({{"method": "item/completed", "params": {{
                        "threadId": thread_id, "turnId": active_turn,
                        "completedAtMs": 3, "item": item
                    }}})
                    emit({{"method": "turn/completed", "params": {{
                        "threadId": thread_id,
                        "turn": {{"id": active_turn, "items": [], "status": "completed"}}
                    }}}})
                elif method == "turn/interrupt":
                    assert frame["params"]["turnId"] == active_turn
                    emit({{"id": request_id, "result": {{}}}})
                    emit({{"method": "turn/completed", "params": {{
                        "threadId": thread_id,
                        "turn": {{"id": active_turn, "items": [], "status": "interrupted"}}
                    }}}})
                else:
                    raise AssertionError(f"unexpected frame: {{frame!r}}")
            """
        )
        .replace("{{", "{")
        .replace("}}", "}")
        .replace("__BEHAVIOR__", repr(behavior))
        .replace("__SANDBOX__", repr(expected_sandbox)),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def test_only_forwards_configured_allowed_mcp_bearer_credential(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        dedent(
            """\
            [mcp_servers.groundlane]
            url = "https://groundlane.example/mcp"
            bearer_token_env_var = "GROUNDLANE_AUTH_TOKEN"
            required = true

            [mcp_servers.unrelated]
            command = "unrelated"
            """
        ),
        encoding="utf-8",
    )
    host_env = {
        "PATH": os.environ["PATH"],
        "CODEX_HOME": str(codex_home),
        "GROUNDLANE_AUTH_TOKEN": "groundlane-test-token",
        "OPENAI_API_KEY": "must-not-be-forwarded",
        "UNRELATED_TOKEN": "must-not-be-forwarded",
    }
    session = CodexAppServerSession(working_directory=tmp_path, host_env=host_env)

    child_env = session._controlled_env()

    assert child_env["GROUNDLANE_AUTH_TOKEN"] == "groundlane-test-token"
    assert "OPENAI_API_KEY" not in child_env
    assert "UNRELATED_TOKEN" not in child_env
    assert session._mcp_configuration_args() == (
        "-c",
        "mcp_servers.groundlane.enabled=true",
        "-c",
        "mcp_servers.unrelated.enabled=false",
    )


def test_does_not_forward_bearer_credential_for_unallowed_mcp(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        dedent(
            """\
            [mcp_servers.other]
            bearer_token_env_var = "OTHER_AUTH_TOKEN"
            """
        ),
        encoding="utf-8",
    )
    session = CodexAppServerSession(
        working_directory=tmp_path,
        host_env={
            "PATH": os.environ["PATH"],
            "CODEX_HOME": str(codex_home),
            "OTHER_AUTH_TOKEN": "must-not-be-forwarded",
        },
    )

    assert "OTHER_AUTH_TOKEN" not in session._controlled_env()
    assert session._mcp_configuration_args() == (
        "-c",
        "mcp_servers.other.enabled=false",
    )


def test_rejects_mcp_name_that_cannot_be_safely_disabled(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[mcp_servers."unsafe.name"]\ncommand = "unsafe"\n', encoding="utf-8"
    )
    session = CodexAppServerSession(
        working_directory=tmp_path,
        host_env={"PATH": os.environ["PATH"], "CODEX_HOME": str(codex_home)},
    )

    with pytest.raises(ConversationProtocolError, match="TOML bare keys"):
        session._mcp_configuration_args()


async def test_workspace_write_is_explicitly_forwarded(tmp_path: Path) -> None:
    executable = _fake_codex(tmp_path, expected_sandbox="workspace-write")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = CodexAppServerSession(
        working_directory=workspace,
        runtime_workspace_roots=(tmp_path,),
        executable=executable,
        sandbox_mode="workspace-write",
    )
    await session.start()
    await session.aclose()


async def test_known_remote_control_status_notification_is_ignored(tmp_path: Path) -> None:
    executable = _fake_codex(tmp_path, behavior="remote_status")
    session = CodexAppServerSession(working_directory=tmp_path, executable=executable)
    await session.start()
    turn_id = await session.send_turn("hello")
    events = session.events()
    while True:
        event = await _next(events)
        if isinstance(event, TurnCompletedEvent):
            assert event.turn_id == turn_id
            assert event.status == RuntimeTurnStatus.COMPLETED
            break
        if isinstance(event, ApprovalRequestedEvent):
            await session.respond_approval(event.approval.request_id, ApprovalDecision.ALLOW_ONCE)
    await session.aclose()


def test_capabilities_report_exact_codex_semantics(tmp_path: Path) -> None:
    capabilities = CodexAppServerSession(working_directory=tmp_path).capabilities

    assert capabilities.token_usage is True
    assert capabilities.native_compaction is True
    assert capabilities.proposed_file_preview is True
    assert capabilities.structured_approvals is False
    assert capabilities.background_task_management is False


@pytest.mark.asyncio
async def test_token_usage_notification_emits_exact_context_telemetry(tmp_path: Path) -> None:
    session = CodexAppServerSession(working_directory=tmp_path)
    session._native_thread_id = "thread"
    session._bind_turn("native-turn", "local-turn")
    session._handle_notification(
        "thread/tokenUsage/updated",
        {
            "threadId": "thread",
            "turnId": "native-turn",
            "tokenUsage": {
                "last": {
                    "inputTokens": 12,
                    "cachedInputTokens": 4,
                    "outputTokens": 3,
                    "reasoningOutputTokens": 2,
                    "totalTokens": 15,
                },
                "total": {
                    "inputTokens": 30,
                    "cachedInputTokens": 8,
                    "outputTokens": 9,
                    "reasoningOutputTokens": 3,
                    "totalTokens": 39,
                },
                "modelContextWindow": 100,
            },
        },
    )

    event = await _next(session.events())
    assert isinstance(event, ContextUsageUpdatedEvent)
    assert event.turn_id == "local-turn"
    assert event.telemetry.accuracy == ContextTelemetryAccuracy.EXACT
    assert event.telemetry.input_tokens == 12
    assert event.telemetry.cached_input_tokens == 4
    assert event.telemetry.output_tokens == 3
    assert event.telemetry.reasoning_output_tokens == 2
    assert event.telemetry.total_tokens == 15
    assert event.telemetry.context_window == 100


@pytest.mark.asyncio
async def test_item_notification_without_thread_id_is_accepted(tmp_path: Path) -> None:
    session = CodexAppServerSession(working_directory=tmp_path)
    session._native_thread_id = "thread"
    session._bind_turn("native-turn", "local-turn")
    session._active_turn = "local-turn"
    session._handle_notification(
        "item/started",
        {
            "turnId": "native-turn",
            "item": {
                "type": "commandExecution",
                "id": "native-command",
                "command": "echo hi",
                "status": "inProgress",
            },
        },
    )
    started = await _next(session.events())
    assert isinstance(started, ToolStartedEvent)
    assert started.turn_id == "local-turn"


@pytest.mark.asyncio
async def test_foreign_thread_item_notification_fails_closed(tmp_path: Path) -> None:
    session = CodexAppServerSession(working_directory=tmp_path)
    session._native_thread_id = "thread"
    session._bind_turn("native-turn", "local-turn")
    session._active_turn = "local-turn"
    with pytest.raises(
        ConversationProtocolError, match="item/started notification correlation"
    ):
        session._handle_notification(
            "item/started",
            {
                "threadId": "other-thread",
                "turnId": "native-turn",
                "item": {
                    "type": "commandExecution",
                    "id": "native-command",
                    "status": "inProgress",
                },
            },
        )


@pytest.mark.asyncio
async def test_replacement_native_turn_on_item_is_adopted(tmp_path: Path) -> None:
    session = CodexAppServerSession(working_directory=tmp_path)
    session._native_thread_id = "thread"
    session._bind_turn("original-native-turn", "local-turn")
    session._active_turn = "local-turn"
    session._handle_notification(
        "item/started",
        {
            "threadId": "thread",
            "turnId": "replacement-native-turn",
            "item": {
                "type": "fileChange",
                "id": "replacement-file-change",
                "status": "inProgress",
                "changes": [{"path": "src/app.py", "kind": {"type": "update"}}],
            },
        },
    )
    started = await _next(session.events())
    assert isinstance(started, ToolStartedEvent)
    assert started.turn_id == "local-turn"
    assert session._native_turns["replacement-native-turn"] == "local-turn"
    assert session._local_turns["local-turn"] == "original-native-turn"

    session._handle_notification(
        "item/completed",
        {
            "threadId": "thread",
            "turnId": "replacement-native-turn",
            "item": {
                "type": "fileChange",
                "id": "replacement-file-change",
                "status": "completed",
            },
        },
    )
    completed = await _next(session.events())
    assert isinstance(completed, ToolCompletedEvent)
    assert completed.action_id == started.action_id


def test_incoherent_token_usage_is_dropped(tmp_path: Path) -> None:
    session = CodexAppServerSession(working_directory=tmp_path)
    session._native_thread_id = "thread"
    session._bind_turn("native-turn", "local-turn")

    # Observational telemetry must degrade instead of ending the conversation.
    session._handle_notification(
        "thread/tokenUsage/updated",
        {
            "threadId": "thread",
            "turnId": "native-turn",
            "tokenUsage": {
                "last": {
                    "inputTokens": 12,
                    "cachedInputTokens": 4,
                    "outputTokens": 3,
                    "reasoningOutputTokens": 2,
                    "totalTokens": 99,
                },
                "total": {},
                "modelContextWindow": 100,
            },
        },
    )
    assert session._event_queue.empty()


@pytest.mark.asyncio
async def test_mcp_tool_call_lifecycle_is_emitted_instead_of_rejected(tmp_path: Path) -> None:
    session = CodexAppServerSession(working_directory=tmp_path)
    session._native_thread_id = "thread"
    session._bind_turn("native-turn", "local-turn")
    session._active_turn = "local-turn"
    started_item = {
        "threadId": "thread",
        "turnId": "native-turn",
        "item": {
            "type": "mcpToolCall",
            "id": "native-mcp-call",
            "server": "groundlane",
            "tool": "web_search",
            "status": "inProgress",
        },
    }
    session._handle_notification("item/started", started_item)
    stream = session.events()
    started = await _next(stream)

    assert isinstance(started, ToolStartedEvent)
    assert started.kind == RuntimeToolKind.MCP
    assert started.tool_name == "groundlane/web_search"

    session._handle_notification(
        "item/mcpToolCall/progress",
        {
            "threadId": "thread",
            "turnId": "native-turn",
            "itemId": "native-mcp-call",
            "message": "Searching sources",
        },
    )
    progress = await _next(stream)
    assert isinstance(progress, ToolOutputDeltaEvent)
    assert progress.action_id == started.action_id

    session._handle_notification(
        "item/completed",
        {
            **started_item,
            "item": {**started_item["item"], "status": "completed"},
        },
    )
    completed = await _next(stream)
    assert isinstance(completed, ToolCompletedEvent)
    assert completed.action_id == started.action_id
    assert completed.status == RuntimeToolStatus.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["started", "interacted", "interrupted"])
async def test_subagent_activity_is_trace_metadata_not_a_blocking_tool(
    tmp_path: Path, kind: str
) -> None:
    session = CodexAppServerSession(working_directory=tmp_path)
    session._native_thread_id = "thread"
    session._bind_turn("native-turn", "local-turn")
    session._active_turn = "local-turn"
    item = {
        "type": "subAgentActivity",
        "id": "activity",
        "agentPath": "weather-research",
        "agentThreadId": "private-vendor-thread",
        "kind": kind,
    }

    session._handle_notification(
        "item/started",
        {"threadId": "thread", "turnId": "native-turn", "item": item},
    )
    session._handle_notification(
        "item/completed",
        {"threadId": "thread", "turnId": "native-turn", "item": item},
    )
    session._handle_notification(
        "turn/completed",
        {
            "threadId": "thread",
            "turn": {"id": "native-turn", "status": "completed"},
        },
    )

    completed = await _next(session.events())
    assert isinstance(completed, TurnCompletedEvent)
    assert completed.status == RuntimeTurnStatus.COMPLETED


@pytest.mark.asyncio
async def test_file_patch_update_is_correlated_and_attached_to_approval(tmp_path: Path) -> None:
    session = CodexAppServerSession(working_directory=tmp_path)
    session._native_thread_id = "thread"
    session._bind_turn("native-turn", "local-turn")
    session._active_turn = "local-turn"
    session._handle_notification(
        "item/started",
        {
            "threadId": "thread",
            "turnId": "native-turn",
            "item": {
                "type": "fileChange",
                "id": "native-file-change",
                "status": "inProgress",
                "changes": [{"path": "src/app.py", "kind": {"type": "update"}}],
            },
        },
    )
    stream = session.events()
    started = await _next(stream)
    assert isinstance(started, ToolStartedEvent)

    diff = "diff --git a/src/app.py b/src/app.py\n+new line\n" + "x" * 70_000
    session._handle_notification(
        "item/fileChange/patchUpdated",
        {
            "threadId": "thread",
            "turnId": "native-turn",
            "itemId": "native-file-change",
            "changes": [
                {
                    "path": "src/app.py",
                    "kind": {"type": "update"},
                    "diff": diff,
                }
            ],
        },
    )
    preview = await _next(stream)
    assert isinstance(preview, ActionPreviewUpdatedEvent)
    assert preview.action_id == started.action_id
    change = preview.proposed_changes[0]
    assert change.kind == ProposedChangeKind.UPDATE
    assert change.paths == ("src/app.py",)
    assert change.original_diff_bytes == len(diff.encode())
    assert change.truncated is True

    await session._handle_server_request(
        "item/fileChange/requestApproval",
        "native-approval",
        {
            "threadId": "thread",
            "turnId": "native-turn",
            "itemId": "native-file-change",
            "grantRoot": str(tmp_path),
        },
    )
    requested = await _next(stream)
    assert isinstance(requested, ApprovalRequestedEvent)
    assert requested.approval.proposed_changes == preview.proposed_changes
    scope = requested.approval.grant_scope
    assert scope is not None
    assert scope.startswith("file_change:")
    assert started.action_id not in scope
    assert len(scope) < 128
    assert str(tmp_path) not in scope
    later_action = "later-local-action"
    session._action_previews[later_action] = (
        change.model_copy(update={"action_id": later_action}),
    )
    assert session._file_change_grant_scope(later_action) == scope
    session._action_previews[later_action] = (
        change.model_copy(update={"action_id": later_action, "paths": ("src/other.py",)}),
    )
    assert session._file_change_grant_scope(later_action) != scope


def test_turn_diff_is_retained_without_false_action_correlation(tmp_path: Path) -> None:
    session = CodexAppServerSession(working_directory=tmp_path)
    session._native_thread_id = "thread"
    session._bind_turn("native-turn", "local-turn")

    session._handle_notification(
        "turn/diff/updated",
        {
            "threadId": "thread",
            "turnId": "native-turn",
            "diff": "aggregate diff",
        },
    )

    assert session._turn_diffs == {"local-turn": "aggregate diff"}


@pytest.mark.asyncio
async def test_native_compaction_delegates_and_emits_lifecycle(tmp_path: Path) -> None:
    executable = _fake_codex(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = CodexAppServerSession(
        working_directory=workspace, executable=executable, request_timeout_seconds=2
    )
    await session.start()
    stream = session.events()

    turn_id = await session.compact_context()
    started = await _next(stream)
    completed = await _next(stream)
    assert isinstance(started, CompactionStartedEvent)
    assert isinstance(completed, CompactionCompletedEvent)
    assert completed.checkpoint is None
    assert {started.turn_id, completed.turn_id} == {turn_id}

    for _ in range(10):
        if session._active_turn is None:
            break
        await asyncio.sleep(0)
    assert session._active_turn is None

    next_turn = await session.send_turn("continue after compaction")
    next_event = await _next(stream)
    assert isinstance(next_event, TurnStartedEvent)
    assert next_event.turn_id == next_turn

    with pytest.raises(ValueError, match="does not accept guidance"):
        await session.compact_context("keep test failures")
    await session.aclose()


async def test_warning_is_normalized_without_stopping_the_turn(tmp_path: Path) -> None:
    executable = _fake_codex(tmp_path, behavior="warning")
    session = CodexAppServerSession(working_directory=tmp_path, executable=executable)
    await session.start()
    await session.send_turn("hello")
    events = session.events()
    seen_warning = False
    while True:
        event = await _next(events)
        if isinstance(event, NoticeEvent):
            seen_warning = True
            assert event.text == "bounded warning"
        elif isinstance(event, ApprovalRequestedEvent):
            await session.respond_approval(event.approval.request_id, ApprovalDecision.ALLOW_ONCE)
        elif isinstance(event, TurnCompletedEvent):
            break
    assert seen_warning
    await session.aclose()


def test_warning_from_foreign_thread_is_dropped(tmp_path: Path) -> None:
    session = CodexAppServerSession(working_directory=tmp_path)
    session._native_thread_id = "owned-thread"
    session._active_turn = "local-turn"

    # A foreign-thread notice is observational; it must not end the conversation.
    session._handle_notification(
        "warning",
        {"threadId": "other-thread", "message": "do not misattribute me"},
    )
    assert session._event_queue.empty()


def test_malformed_turn_diff_is_dropped(tmp_path: Path) -> None:
    session = CodexAppServerSession(working_directory=tmp_path)
    session._native_thread_id = "thread"
    session._bind_turn("native-turn", "local-turn")

    # Display-only diff must degrade instead of ending the conversation.
    session._handle_notification(
        "turn/diff/updated",
        {"threadId": "thread", "turnId": "native-turn", "diff": 5},
    )
    assert session._event_queue.empty()


def test_malformed_warning_is_dropped(tmp_path: Path) -> None:
    session = CodexAppServerSession(working_directory=tmp_path)
    session._native_thread_id = "thread"
    session._active_turn = "local-turn"

    # A notice with a non-string body must degrade instead of ending the turn.
    session._handle_notification("warning", {"threadId": "thread", "message": 5})
    assert session._event_queue.empty()


def test_observational_degrade_keeps_critical_paths_fail_closed(tmp_path: Path) -> None:
    session = CodexAppServerSession(working_directory=tmp_path)
    session._native_thread_id = "thread"
    session._bind_turn("native-turn", "local-turn")
    session._active_turn = "local-turn"

    # Tool lifecycle must stay fail-closed even though observational frames degrade.
    with pytest.raises(ConversationProtocolError, match="item/started notification"):
        session._handle_notification(
            "item/started",
            {
                "threadId": "other-thread",
                "turnId": "native-turn",
                "item": {"type": "fileChange", "id": "x"},
            },
        )


def test_duplicate_tool_completion_and_unfinished_terminal_fail_closed(tmp_path: Path) -> None:
    session = CodexAppServerSession(working_directory=tmp_path)
    session._native_thread_id = "thread"
    session._bind_turn("native-turn", "local-turn")
    session._active_turn = "local-turn"
    started = {
        "threadId": "thread",
        "turnId": "native-turn",
        "item": {
            "type": "commandExecution",
            "id": "item",
            "command": "pytest -q",
            "cwd": str(tmp_path),
            "status": "inProgress",
        },
    }
    completed = {
        **started,
        "item": {**started["item"], "status": "completed", "exitCode": 0},
    }
    session._handle_notification("item/started", started)
    with pytest.raises(ConversationProtocolError, match="unfinished tool"):
        session._handle_notification(
            "turn/completed",
            {
                "threadId": "thread",
                "turn": {"id": "native-turn", "status": "completed"},
            },
        )
    session._handle_notification("item/completed", completed)
    with pytest.raises(ConversationProtocolError, match="before it started"):
        session._handle_notification("item/completed", completed)


def test_unknown_sandbox_mode_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sandbox_mode"):
        CodexAppServerSession(
            working_directory=tmp_path,
            sandbox_mode="danger-full-access",  # type: ignore[arg-type]
        )


def test_file_change_description_preserves_all_paths() -> None:
    description = CodexAppServerSession._tool_description(
        "fileChange",
        {
            "changes": [
                {"path": "src/app.py", "kind": "update"},
                {"path": "tests/test_app.py", "kind": "update"},
            ]
        },
    )

    assert description[4] == "src/app.py"
    assert description[5] == ("src/app.py", "tests/test_app.py")


@pytest.mark.asyncio
async def test_file_change_approval_uses_started_tool_context_when_request_is_sparse(
    tmp_path: Path,
) -> None:
    session = CodexAppServerSession(working_directory=tmp_path)
    session._native_thread_id = "thread"
    session._bind_turn("native-turn", "local-turn")
    session._active_turn = "local-turn"
    session._handle_notification(
        "item/started",
        {
            "threadId": "thread",
            "turnId": "native-turn",
            "item": {
                "type": "fileChange",
                "id": "vendor-file-change",
                "status": "inProgress",
                "changes": [
                    {"path": "src/app.py", "kind": "update"},
                    {"path": "tests/test_app.py", "kind": "update"},
                ],
            },
        },
    )
    stream = session.events()
    started = await _next(stream)
    assert isinstance(started, ToolStartedEvent)

    await session._handle_server_request(
        "item/fileChange/requestApproval",
        "vendor-approval",
        {
            "threadId": "thread",
            "turnId": "native-turn",
            "itemId": "vendor-file-change",
        },
    )
    requested = await _next(stream)

    assert isinstance(requested, ApprovalRequestedEvent)
    assert requested.approval.effect == ToolEffect.MODIFY
    assert "Action: Modify files" in requested.approval.preview
    assert f"Working directory: {tmp_path}" in requested.approval.preview
    assert "src/app.py" in requested.approval.preview
    assert "tests/test_app.py" in requested.approval.preview
    assert "vendor-file-change" not in requested.approval.preview
    assert "vendor-approval" not in requested.approval.preview


async def _next(stream):
    return await asyncio.wait_for(anext(stream), timeout=2)


@pytest.mark.asyncio
async def test_long_lived_session_streams_tools_approval_and_second_turn(
    tmp_path: Path,
) -> None:
    executable = _fake_codex(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = CodexAppServerSession(
        working_directory=workspace,
        executable=executable,
        request_timeout_seconds=2,
    )
    await session.start()
    stream = session.events()
    first_turn = await session.send_turn("inspect then test")

    started = await _next(stream)
    text = await _next(stream)
    tool = await _next(stream)
    output = await _next(stream)
    requested = await _next(stream)
    assert isinstance(started, TurnStartedEvent)
    assert isinstance(text, TextDeltaEvent) and text.text == "Working"
    assert isinstance(tool, ToolStartedEvent)
    assert tool.kind == RuntimeToolKind.COMMAND
    assert tool.effect == ToolEffect.EXECUTE
    assert tool.summary == "pytest -q"
    assert isinstance(output, ToolOutputDeltaEvent) and output.action_id == tool.action_id
    assert isinstance(requested, ApprovalRequestedEvent)
    assert requested.turn_id == first_turn
    assert requested.approval.action_id == tool.action_id
    rendered = requested.model_dump_json()
    assert "vendor-thread-secret" not in rendered
    assert "vendor-turn-1" not in rendered
    assert "vendor-action-secret" not in rendered
    assert "vendor-approval-secret" not in rendered

    await session.respond_approval(requested.approval.request_id, ApprovalDecision.ALLOW_ONCE)
    resolved = await _next(stream)
    completed_tool = await _next(stream)
    completed_turn = await _next(stream)
    assert isinstance(resolved, ApprovalResolvedEvent)
    assert isinstance(completed_tool, ToolCompletedEvent)
    assert completed_tool.output == "one passed"
    assert isinstance(completed_turn, TurnCompletedEvent)
    assert completed_turn.status == RuntimeTurnStatus.COMPLETED

    second_turn = await session.send_turn("what happened?")
    assert second_turn != first_turn
    assert isinstance(await _next(stream), TurnStartedEvent)
    second_text = await _next(stream)
    assert isinstance(second_text, TextDeltaEvent) and second_text.text == "Second answer"
    assert isinstance(await _next(stream), TurnCompletedEvent)
    await session.aclose()


@pytest.mark.asyncio
async def test_duplicate_or_stale_approval_response_fails_closed(tmp_path: Path) -> None:
    executable = _fake_codex(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = CodexAppServerSession(
        working_directory=workspace, executable=executable, request_timeout_seconds=2
    )
    await session.start()
    stream = session.events()
    await session.send_turn("run tests")
    requested = None
    while requested is None:
        event = await _next(stream)
        if isinstance(event, ApprovalRequestedEvent):
            requested = event
    await session.respond_approval(requested.approval.request_id, ApprovalDecision.ALLOW_ONCE)
    with pytest.raises(ConversationProtocolError, match="stale or duplicate"):
        await session.respond_approval(requested.approval.request_id, ApprovalDecision.ALLOW_ONCE)
    await session.aclose()


@pytest.mark.asyncio
async def test_unavailable_session_grant_is_rejected_without_wire_response(
    tmp_path: Path,
) -> None:
    executable = _fake_codex(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = CodexAppServerSession(
        working_directory=workspace, executable=executable, request_timeout_seconds=2
    )
    await session.start()
    stream = session.events()
    await session.send_turn("run tests")
    while True:
        event = await _next(stream)
        if isinstance(event, ApprovalRequestedEvent):
            break
    assert ApprovalDecision.ALLOW_SESSION not in event.approval.available_decisions
    with pytest.raises(ValueError, match="unavailable"):
        await session.respond_approval(event.approval.request_id, ApprovalDecision.ALLOW_SESSION)
    await session.respond_approval(event.approval.request_id, ApprovalDecision.ALLOW_ONCE)
    await session.aclose()


@pytest.mark.asyncio
async def test_interrupt_correlates_local_turn_and_emits_terminal(tmp_path: Path) -> None:
    executable = _fake_codex(tmp_path, "interrupt")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = CodexAppServerSession(
        working_directory=workspace, executable=executable, request_timeout_seconds=2
    )
    await session.start()
    stream = session.events()
    turn_id = await session.send_turn("wait")
    assert isinstance(await _next(stream), TurnStartedEvent)
    await session.interrupt(turn_id)
    terminal = await _next(stream)
    assert isinstance(terminal, TurnCompletedEvent)
    assert terminal.status == RuntimeTurnStatus.INTERRUPTED
    with pytest.raises(ConversationProtocolError, match="already terminal"):
        await session.interrupt(turn_id)
    await session.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["unknown", "oversize"])
async def test_unknown_or_oversized_frame_fails_closed(tmp_path: Path, behavior: str) -> None:
    executable = _fake_codex(tmp_path, behavior)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = CodexAppServerSession(
        working_directory=workspace,
        executable=executable,
        request_timeout_seconds=2,
        max_frame_bytes=2_000,
    )
    await session.start()
    stream = session.events()
    await session.send_turn("trigger protocol failure")
    assert isinstance(await _next(stream), TurnStartedEvent)
    with pytest.raises(ConversationProtocolError):
        await _next(stream)
    await session.aclose()
    assert session._process is not None and session._process.returncode is not None


@pytest.mark.asyncio
async def test_aclose_is_idempotent_and_reaps_child(tmp_path: Path) -> None:
    executable = _fake_codex(tmp_path, "interrupt")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = CodexAppServerSession(working_directory=workspace, executable=executable)
    await session.start()
    process = session._process
    assert process is not None and process.returncode is None
    await session.aclose()
    await session.aclose()
    assert process.returncode is not None


def test_rejects_symlink_working_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    symlink = tmp_path / "workspace-link"
    os.symlink(workspace, symlink)
    with pytest.raises(ValueError, match="symlink"):
        CodexAppServerSession(working_directory=symlink)


@pytest.mark.asyncio
async def test_unknown_turn_notification_is_dropped_with_diagnostics(
    tmp_path: Path, caplog
) -> None:
    import logging as _logging

    session = CodexAppServerSession(working_directory=tmp_path)
    session._native_thread_id = "thread"
    session._bind_turn("native-turn", "local-turn")

    with caplog.at_level(_logging.WARNING, logger="rivumi.codex_app_server"):
        session._handle_notification(
            "thread/tokenUsage/updated",
            {"threadId": "thread", "turnId": "rogue-turn", "tokenUsage": {}},
        )

    # Observational telemetry degrades instead of ending the conversation.
    assert session._event_queue.empty()
    # The offending native id still stays diagnosable in the log.
    warning = next(
        record for record in caplog.records if record.levelno == _logging.WARNING
    )
    assert "rogue-turn" in warning.getMessage()


@pytest.mark.asyncio
async def test_unknown_turn_diagnostics_include_retained_stderr(
    tmp_path: Path, caplog
) -> None:
    import logging as _logging

    session = CodexAppServerSession(working_directory=tmp_path)
    session._native_thread_id = "thread"
    session._bind_turn("native-turn", "local-turn")
    session._stderr_tail.append(b"codex panic: internal turn registry reset\n")

    with (
        caplog.at_level(_logging.WARNING, logger="rivumi.codex_app_server"),
        pytest.raises(ConversationProtocolError) as exc_info,
    ):
            session._handle_notification(
                "item/started",
                {
                    "threadId": "thread",
                    "turnId": "subagent-turn",
                    "item": {"id": "item-1", "type": "reasoning"},
                },
            )

    assert "item/started" in str(exc_info.value)
    warning = next(
        record for record in caplog.records if record.levelno == _logging.WARNING
    )
    text = warning.getMessage()
    assert "codex panic: internal turn registry reset" in text
    assert "'subagent-turn'" in text


@pytest.mark.asyncio
async def test_foreign_thread_notification_is_dropped(tmp_path: Path) -> None:
    session = CodexAppServerSession(working_directory=tmp_path)
    session._native_thread_id = "thread"

    # Observational telemetry degrades on a foreign thread instead of ending the
    # conversation.
    session._handle_notification(
        "thread/tokenUsage/updated",
        {"threadId": "other-thread", "turnId": "native-turn", "tokenUsage": {}},
    )
    assert session._event_queue.empty()


@pytest.mark.asyncio
async def test_server_initiated_turn_is_adopted_into_active_turn(tmp_path: Path) -> None:
    session = CodexAppServerSession(working_directory=tmp_path)
    session._native_thread_id = "thread"
    session._bind_turn("native-turn", "local-turn")
    session._active_turn = "local-turn"

    # Codex replaces its internal turn after a failed collab spawn.
    session._handle_notification(
        "turn/started",
        {"turn": {"id": "native-turn-replacement"}},
    )

    # The replacement native id now maps onto the active logical turn.
    assert (
        session._local_turn("native-turn-replacement", context="verify")
        == "local-turn"
    )
    # The original reverse binding is preserved for interrupts.
    assert session._local_turns["local-turn"] == "native-turn"


@pytest.mark.asyncio
async def test_server_initiated_completion_for_adopted_turn_terminates(
    tmp_path: Path,
) -> None:
    session = CodexAppServerSession(working_directory=tmp_path)
    session._native_thread_id = "thread"
    session._bind_turn("native-turn", "local-turn")
    session._active_turn = "local-turn"

    session._handle_notification(
        "turn/started", {"turn": {"id": "native-turn-replacement"}}
    )
    session._handle_notification(
        "turn/completed",
        {"turn": {"id": "native-turn-replacement", "status": "completed"}},
    )

    stream = session.events()
    while True:
        event = await _next(stream)
        if isinstance(event, TurnCompletedEvent):
            assert event.turn_id == "local-turn"
            assert event.status == RuntimeTurnStatus.COMPLETED
            break
