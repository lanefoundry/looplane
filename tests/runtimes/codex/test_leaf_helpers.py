"""Characterize Slice 1.1 wire values and compatibility dispatch boundaries."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

import looplane.codex_app_server as facade
from looplane.approvals import ApprovalDecision, ToolEffect
from looplane.conversation_runtime import (
    ConversationProtocolError,
    RuntimeApprovalKind,
    RuntimeToolKind,
    RuntimeToolStatus,
)
from looplane.runtimes.codex import approval_mapper, parsing, tool_mapper


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        (1, False),
        (True, False),
        (b"id", False),
        ("", False),
        ("x" * 256, True),
        ("x" * 257, False),
        ("a\x00b", False),
        ("\n", True),
        ("\u754c" * 256, True),
    ],
)
def test_safe_id_preserves_character_bound_and_nul_rule(value, expected):
    assert parsing.safe_id(value) is expected


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5, 6, 7, 8])
def test_bounded_text_preserves_utf8_boundaries(limit):
    text = "a\u754c\u754cb"
    assert parsing.bounded_text(text, max_frame_bytes=limit) == (
        text.encode()[:limit].decode(errors="ignore")
    )


def test_preview_diff_preserves_separate_fixed_byte_limit():
    assert parsing.preview_diff(None) == (None, None, False)
    assert parsing.preview_diff("") == ("", 0, False)
    assert parsing.preview_diff("x" * 64000) == ("x" * 64000, 64000, False)
    assert parsing.preview_diff("\u754c" * 21334) == ("\u754c" * 21333, 64002, True)


@pytest.mark.parametrize("raw", [b"{}\n", b'{"id":1,"result":{}}\n'])
def test_frame_at_exact_bounds(raw):
    result = parsing.parse_frame(raw, frame_count=2, max_frames=2, max_frame_bytes=len(raw))
    assert isinstance(result, dict)


@pytest.mark.parametrize(
    ("raw", "count", "limit", "message", "cause"),
    [
        (b"{}\n", 3, 3, "exceeded protocol bounds", None),
        (b"{}\n", 2, 2, "exceeded protocol bounds", None),
        (b"{", 2, 3, "invalid JSON", ValueError),
        (b"\xff", 2, 3, "invalid JSON", UnicodeDecodeError),
        (b"[]", 2, 3, "must be an object", None),
        (b"1", 2, 3, "must be an object", None),
    ],
)
def test_frame_errors_keep_messages_and_causes(raw, count, limit, message, cause):
    with pytest.raises(ConversationProtocolError, match=message) as error:
        parsing.parse_frame(raw, frame_count=count, max_frames=2, max_frame_bytes=limit)
    if cause is None:
        assert error.value.__cause__ is None
    else:
        assert isinstance(error.value.__cause__, cause)


def test_bounds_are_checked_before_json_decode():
    with pytest.raises(ConversationProtocolError, match="exceeded protocol bounds"):
        parsing.parse_frame(b"invalid", frame_count=2, max_frames=1, max_frame_bytes=20)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("completed", RuntimeToolStatus.COMPLETED),
        ("failed", RuntimeToolStatus.FAILED),
        ("declined", RuntimeToolStatus.DECLINED),
        ("interrupted", RuntimeToolStatus.INTERRUPTED),
    ],
)
def test_tool_status(raw, expected):
    assert tool_mapper.tool_status(raw) is expected


@pytest.mark.parametrize("raw", [None, "inProgress", "cancelled", 1])
def test_unknown_tool_status_still_fails_closed(raw):
    with pytest.raises(ConversationProtocolError, match="tool completed with an invalid status"):
        tool_mapper.tool_status(raw)


def test_unhashable_tool_status_keeps_existing_exception():
    with pytest.raises(TypeError):
        tool_mapper.tool_status([])


def test_command_and_file_descriptions_keep_bounds_order_and_counts():
    assert tool_mapper.tool_description(
        "commandExecution", {"command": "x" * 16001, "cwd": "p" * 4097}
    ) == (
        RuntimeToolKind.COMMAND,
        "shell",
        ToolEffect.EXECUTE,
        "x" * 16000,
        "p" * 4096,
        (),
    )
    assert tool_mapper.tool_description(
        "fileChange",
        {
            "changes": [{"path": "b"}, {"path": "a"}, {"path": "b"}, {}, None],
        },
    ) == (
        RuntimeToolKind.FILE_CHANGE,
        "file_change",
        ToolEffect.MODIFY,
        "5 file change(s)",
        "b",
        ("b", "a"),
    )
    description = tool_mapper.tool_description(
        "fileChange",
        {
            "changes": [{"path": "p" * 4097}, {"path": "p" * 4096 + "q"}],
        },
    )
    assert description[5] == ("p" * 4096,)


@pytest.mark.parametrize(
    ("kind", "item", "expected_kind", "name"),
    [
        (
            "mcpToolCall",
            {"server": "server", "tool": "t" * 256},
            RuntimeToolKind.MCP,
            ("server/" + "t" * 256)[:256],
        ),
        ("dynamicToolCall", {"tool": "t" * 257}, RuntimeToolKind.MCP, "t" * 256),
        ("collabAgentToolCall", {}, RuntimeToolKind.AGENT, "agent"),
        ("webSearch", {}, RuntimeToolKind.WEB, "web_search"),
        ("unknown", {}, RuntimeToolKind.WEB, "web_search"),
    ],
)
def test_other_tool_descriptions(kind, item, expected_kind, name):
    assert tool_mapper.tool_description(kind, item) == (
        expected_kind,
        name,
        ToolEffect.EXECUTE,
        "",
        None,
        (),
    )


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("commandExecution", "command item has no command"),
        ("fileChange", "file change item is malformed"),
        ("mcpToolCall", "MCP item is malformed"),
        ("dynamicToolCall", "dynamic tool item is malformed"),
    ],
)
def test_malformed_descriptions(kind, message):
    with pytest.raises(ConversationProtocolError, match=message):
        tool_mapper.tool_description(kind, {})


@pytest.mark.parametrize("decision", list(ApprovalDecision))
def test_wire_decisions_and_permission_filtering(decision):
    wire = {
        ApprovalDecision.ALLOW_ONCE: "accept",
        ApprovalDecision.ALLOW_SESSION: "acceptForSession",
        ApprovalDecision.DENY: "decline",
        ApprovalDecision.CANCEL: "cancel",
    }
    requested = {"network": {"enabled": True}, "fileSystem": None, "unexpected": True}
    assert approval_mapper.approval_result(
        "item/commandExecution/requestApproval", requested, decision
    ) == {"decision": wire[decision]}
    result = approval_mapper.approval_result(
        "item/permissions/requestApproval", requested, decision
    )
    allowed = decision in {ApprovalDecision.ALLOW_ONCE, ApprovalDecision.ALLOW_SESSION}
    assert result == {
        "permissions": {"network": {"enabled": True}} if allowed else {},
        "scope": "session" if decision == ApprovalDecision.ALLOW_SESSION else "turn",
        "strictAutoReview": True,
    }
    if allowed:
        assert result["permissions"]["network"] is requested["network"]
    assert "unexpected" in requested


def test_available_decisions_preserve_defaults_order_deduplication_and_permissions():
    expected = (
        ApprovalDecision.ALLOW_ONCE,
        ApprovalDecision.ALLOW_SESSION,
        ApprovalDecision.DENY,
        ApprovalDecision.CANCEL,
    )
    assert approval_mapper.available_decisions(RuntimeApprovalKind.COMMAND, None) == expected
    assert approval_mapper.available_decisions(RuntimeApprovalKind.PERMISSIONS, {}) == expected
    assert approval_mapper.available_decisions(
        RuntimeApprovalKind.FILE_CHANGE, ["cancel", {}, "accept", "cancel", "unknown", "decline"]
    ) == (
        ApprovalDecision.CANCEL,
        ApprovalDecision.ALLOW_ONCE,
        ApprovalDecision.DENY,
    )


@pytest.mark.parametrize("raw", [{}, "accept", [], ["unknown", {}]])
def test_unsupported_available_decisions(raw):
    message = "no supported decision" if isinstance(raw, list) else "must be a list"
    with pytest.raises(ConversationProtocolError, match=message):
        approval_mapper.available_decisions(RuntimeApprovalKind.COMMAND, raw)


def test_static_compatibility_names_are_exact_leaf_function_objects():
    session = facade.CodexAppServerSession
    assert session._safe_id is parsing.safe_id
    assert session._preview_diff is parsing.preview_diff
    assert session._tool_status is tool_mapper.tool_status
    assert session._tool_description is tool_mapper.tool_description
    assert session._available_decisions is approval_mapper.available_decisions
    assert facade.ConversationProtocolError is ConversationProtocolError
    assert facade.RuntimeToolStatus is RuntimeToolStatus
    assert facade.ApprovalDecision is ApprovalDecision
    assert facade._PendingApproval.__module__ == "looplane.codex_app_server"


def test_session_bounds_remain_dynamic_and_patchable(tmp_path, monkeypatch):
    session = facade.CodexAppServerSession(working_directory=tmp_path, max_frame_bytes=3)
    assert session._bounded("\u754c\u754c") == "\u754c"
    session.max_frame_bytes = 2
    assert session._bounded("\u754c") == ""
    calls = []

    def patched(value):
        calls.append(value)
        return "patched"

    monkeypatch.setattr(session, "_bounded", patched)
    assert session._tool_completion_summary("fileChange", {"status": "completed"}) == "patched"
    assert (
        session._tool_completion_output("commandExecution", {"aggregatedOutput": "output"})
        == "patched"
    )
    assert (
        session._tool_completion_diff(
            "fileChange",
            {
                "changes": [{"diff": "first"}, None, {"diff": "second"}],
            },
        )
        == "patched"
    )
    assert calls == ["completed", "output", "first\nsecond"]
    assert session._tool_completion_summary("commandExecution", {"exitCode": True}) == "exit True"
    assert session._tool_completion_summary("commandExecution", {}) == "command finished"
    assert session._tool_completion_summary("fileChange", {}) == "tool finished"
    assert session._tool_completion_output("fileChange", {"aggregatedOutput": "ignored"}) is None
    assert session._tool_completion_diff("fileChange", {"changes": [{"diff": ""}]}) is None
    assert calls == ["completed", "output", "first\nsecond"]


def test_completion_character_caps_after_patched_bounding(tmp_path, monkeypatch):
    session = facade.CodexAppServerSession(working_directory=tmp_path)
    monkeypatch.setattr(session, "_bounded", lambda value: "x" * 65000)
    assert len(session._tool_completion_summary("fileChange", {"status": "ok"})) == 16000
    assert (
        len(session._tool_completion_output("commandExecution", {"aggregatedOutput": "x"})) == 64000
    )
    assert len(session._tool_completion_diff("fileChange", {"changes": [{"diff": "x"}]})) == 64000


def test_pending_approval_compatibility_and_facade_uuid_patch(tmp_path, monkeypatch):
    session = facade.CodexAppServerSession(working_directory=tmp_path)
    pending = facade._PendingApproval(
        1,
        "item/permissions/requestApproval",
        "turn",
        {"network": True},
        (ApprovalDecision.ALLOW_ONCE,),
    )
    assert session._approval_result(pending, ApprovalDecision.ALLOW_ONCE) == {
        "permissions": {"network": True},
        "scope": "turn",
        "strictAutoReview": True,
    }
    monkeypatch.setattr(facade, "uuid4", lambda: SimpleNamespace(hex="patched-id"))
    assert session._local_action("native-turn", "native-item") == "patched-id"


async def test_reader_keeps_count_and_failure_ownership(tmp_path):
    import asyncio

    session = facade.CodexAppServerSession(working_directory=tmp_path, max_frames=1)
    reader = asyncio.StreamReader()
    reader.feed_data(b"{}\n{}\n")
    reader.feed_eof()
    session._process = SimpleNamespace(stdout=reader, returncode=0)
    frames = []

    async def receive(frame):
        frames.append(frame)

    session._handle_frame = receive
    await session._reader_loop()
    assert frames == [{}]
    assert session._frame_count == 2
    assert isinstance(session._fatal, ConversationProtocolError)
    assert str(session._fatal) == "app-server output exceeded protocol bounds"


def test_facade_json_module_monkeypatch_still_reaches_parser(monkeypatch):
    monkeypatch.setattr(facade.json, "loads", lambda raw: {"patched": True})
    assert parsing.parse_frame(b"{}", frame_count=1, max_frames=1, max_frame_bytes=2) == {
        "patched": True
    }


def test_leaf_imports_do_not_load_compatibility_facade_or_product_entrypoints():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; "
            "from looplane.runtimes.codex import parsing, tool_mapper, approval_mapper; "
            "assert not {'looplane.codex_app_server', 'looplane.cli', 'looplane.tui'} "
            ".intersection(sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
