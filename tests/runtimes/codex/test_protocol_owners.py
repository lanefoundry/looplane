"""Replay vendor frames against protocol owners without any process or host."""

from __future__ import annotations

import asyncio
import itertools
import subprocess
import sys

import pytest

from looplane.approvals import ApprovalDecision
from looplane.conversation_runtime import (
    ApprovalRequestedEvent,
    CompactionCompletedEvent,
    CompactionStartedEvent,
    ConversationProtocolError,
    TextDeltaEvent,
    ToolCompletedEvent,
    ToolOutputDeltaEvent,
    ToolStartedEvent,
    TurnCompletedEvent,
    TurnStartedEvent,
)
from looplane.runtimes.codex.approval_mapper import CodexApprovalMapper
from looplane.runtimes.codex.correlation import CodexCorrelation
from looplane.runtimes.codex.event_mapper import CodexEventMapper
from looplane.runtimes.codex.parsing import bounded_text, parse_frame
from looplane.runtimes.codex.session import CodexAppServerSession


@pytest.fixture
def owners(tmp_path):
    ids = itertools.count()
    events = []

    def new_id():
        return f"local-{next(ids)}"

    def emit(cls, **kwargs):
        events.append(cls(sequence=len(events), **kwargs))

    def bounded(value):
        return bounded_text(value, max_frame_bytes=256000)

    correlation = CodexCorrelation(new_id=new_id, stderr_tail=lambda: "diagnostic tail")
    correlation.native_thread_id = "thread"
    mapper = CodexEventMapper(
        correlation=correlation,
        emit=emit,
        bounded=bounded,
        new_id=new_id,
        stderr_tail=lambda: "diagnostic tail",
        working_directory=tmp_path,
    )
    approvals = CodexApprovalMapper(
        correlation=correlation,
        emit=emit,
        bounded=bounded,
        new_id=new_id,
        action_context=mapper.approval_context,
    )
    return correlation, mapper, approvals, events


def test_recorded_command_sequence_correlates_approval_output_and_terminal(owners):
    correlation, mapper, approvals, events = owners
    correlation.starting_turn = "local-turn"
    raw_frames = [
        b'{"method":"turn/started","params":{"turn":{"id":"native-turn"}}}',
        b'{"method":"item/started","params":{"threadId":"thread",'
        b'"turnId":"native-turn","item":{"id":"command","type":"commandExecution",'
        b'"command":"pwd","cwd":"/workspace"}}}',
        b'{"id":17,"method":"item/commandExecution/requestApproval",'
        b'"params":{"threadId":"thread","turnId":"native-turn","itemId":"command",'
        b'"availableDecisions":["accept","decline"]}}',
        b'{"method":"item/commandExecution/outputDelta","params":{"threadId":"thread",'
        b'"turnId":"native-turn","itemId":"command","delta":"/workspace"}}',
        b'{"method":"item/completed","params":{"threadId":"thread",'
        b'"turnId":"native-turn","item":{"id":"command","type":"commandExecution",'
        b'"status":"completed","exitCode":0,"aggregatedOutput":"/workspace"}}}',
        b'{"method":"item/agentMessage/delta","params":{"threadId":"thread",'
        b'"turnId":"native-turn","delta":"Finished."}}',
        b'{"method":"turn/completed","params":{"turn":{"id":"native-turn","status":"completed"}}}',
    ]
    for index, raw in enumerate(raw_frames, 1):
        frame = parse_frame(raw, frame_count=index, max_frames=7, max_frame_bytes=1024)
        if "id" in frame:
            approvals.handle_server_request(frame["method"], frame["id"], frame["params"])
        else:
            mapper.handle_notification(frame["method"], frame["params"])
    assert [type(event) for event in events] == [
        TurnStartedEvent,
        ToolStartedEvent,
        ApprovalRequestedEvent,
        ToolOutputDeltaEvent,
        ToolCompletedEvent,
        TextDeltaEvent,
        TurnCompletedEvent,
    ]
    assert [event.sequence for event in events] == list(range(7))
    assert {event.turn_id for event in events} == {"local-turn"}
    action_id = events[1].action_id
    assert events[2].approval.action_id == action_id
    assert events[3].action_id == events[4].action_id == action_id
    assert "Command: pwd" in events[2].approval.preview
    pending = approvals.pending[events[2].approval.request_id]
    assert pending.wire_id == 17
    assert approvals.approval_result(pending, ApprovalDecision.ALLOW_ONCE) == {"decision": "accept"}
    assert correlation.completed_turns == {"local-turn"}


def test_replacement_turn_preserves_interrupt_target_and_action_identity(owners):
    correlation, mapper, _, events = owners
    correlation.bind_turn("original", "local-turn")
    correlation.active_turn = "local-turn"
    mapper.handle_notification("turn/started", {"turn": {"id": "replacement"}})
    assert correlation.local_turns == {"local-turn": "original"}
    assert correlation.local_turn("replacement", context="item") == "local-turn"
    first = correlation.local_action("replacement", "item")
    assert correlation.local_action("replacement", "item") == first
    assert correlation.local_action("original", "item") != first
    assert len(events) == 1


@pytest.mark.parametrize("native,local", [("original", "other"), ("other", "local-turn")])
def test_strict_binding_still_rejects_rebinding(owners, native, local):
    correlation, _, _, _ = owners
    correlation.bind_turn("original", "local-turn")
    with pytest.raises(ConversationProtocolError, match="rebound"):
        correlation.bind_turn(native, local)
    assert correlation.native_turns == {"original": "local-turn"}
    assert correlation.local_turns == {"local-turn": "original"}


async def test_compaction_binding_and_terminal_have_one_owner(owners):
    correlation, mapper, _, events = owners
    correlation.starting_turn = "compact-local"
    correlation.compaction_turns.add("compact-local")
    correlation.compaction_start_future = asyncio.get_running_loop().create_future()
    mapper.handle_notification("turn/started", {"turn": {"id": "compact-native"}})
    assert correlation.compaction_start_future.result() == "compact-local"
    assert correlation.active_turn == "compact-local"
    mapper.handle_notification(
        "thread/compacted", {"threadId": "thread", "turnId": "compact-native"}
    )
    mapper.handle_notification(
        "turn/completed", {"turn": {"id": "compact-native", "status": "completed"}}
    )
    assert [type(event) for event in events] == [CompactionStartedEvent, CompactionCompletedEvent]
    assert correlation.active_turn is None
    with pytest.raises(ConversationProtocolError, match="duplicate terminal"):
        mapper.handle_notification(
            "turn/completed", {"turn": {"id": "compact-native", "status": "completed"}}
        )


@pytest.mark.parametrize(
    "method,params,message",
    [
        ("unknown/protocol", {}, "unsupported server notification"),
        (
            "item/agentMessage/delta",
            {"threadId": "foreign", "turnId": "native", "delta": "text"},
            "correlation is invalid",
        ),
        (
            "item/commandExecution/outputDelta",
            {"threadId": "thread", "turnId": "native", "itemId": "missing", "delta": "text"},
            "preceded tool start",
        ),
    ],
)
def test_unknown_and_out_of_order_frames_fail_closed(owners, method, params, message):
    correlation, mapper, _, events = owners
    correlation.bind_turn("native", "local")
    with pytest.raises(ConversationProtocolError, match=message):
        mapper.handle_notification(method, params)
    assert events == []


def test_approval_owner_rejects_duplicate_wire_id_and_foreign_thread(owners):
    correlation, _, approvals, events = owners
    correlation.bind_turn("native", "local")
    params = {"threadId": "thread", "turnId": "native", "itemId": "item"}
    method = "item/commandExecution/requestApproval"
    approvals.handle_server_request(method, 9, params)
    with pytest.raises(ConversationProtocolError, match="duplicate approval"):
        approvals.handle_server_request(method, 9, params)
    with pytest.raises(ConversationProtocolError, match="wrong thread"):
        approvals.handle_server_request(method, 10, {**params, "threadId": "foreign"})
    assert len(approvals.pending) == len(events) == 1
    assert approvals.wire_ids == {9}


def test_canonical_session_uses_explicit_id_port(tmp_path):
    session = CodexAppServerSession(working_directory=tmp_path, _new_id=lambda: "injected")
    assert session.correlation.local_action("native", "item") == "injected"
    assert session._native_actions is session.correlation.native_actions
    session._active_turn = "turn"
    assert session.correlation.active_turn == "turn"
    assert session._action_previews is session.event_mapper.action_previews


def test_canonical_host_and_protocol_import_without_legacy_facades():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; "
            "from looplane.runtimes.codex import conversation, session, correlation, "
            "event_mapper, approval_mapper; "
            "assert not {'looplane.codex_app_server', 'looplane.codex_conversation', "
            "'looplane.cli', 'looplane.tui'}.intersection(sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
