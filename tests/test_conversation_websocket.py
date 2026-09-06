from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from looplane.conversation_runtime import RuntimeTurnStatus, TextDeltaEvent, TurnCompletedEvent
from looplane.conversation_websocket import ConversationWebSocketApp
from looplane.runtime_semantics import RuntimeCapabilities


class FakeSession:
    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.started = 0
        self.closed = 0
        self.turn_texts = []

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(native_compaction=False)

    async def start(self) -> None:
        self.started += 1

    async def send_turn(self, text: str) -> str:
        self.turn_texts.append(text)
        await self.queue.put(TextDeltaEvent(sequence=0, turn_id="turn-1", text=f"echo:{text}"))
        await self.queue.put(
            TurnCompletedEvent(
                sequence=1,
                turn_id="turn-1",
                status=RuntimeTurnStatus.COMPLETED,
            )
        )
        return "turn-1"

    async def compact_context(self, guidance=None) -> str:
        raise RuntimeError("unavailable")

    async def events(self):
        while True:
            yield await self.queue.get()

    async def respond_approval(self, request_id, decision) -> None:
        raise AssertionError("no approvals in this fixture")

    async def interrupt(self, turn_id: str) -> None:
        return None

    async def aclose(self) -> None:
        self.closed += 1


async def test_conversation_websocket_streams_turn_events_and_result() -> None:
    inbound = asyncio.Queue()
    outbound: list[dict] = []
    app = ConversationWebSocketApp(FakeSession())
    await inbound.put(
        {
            "type": "websocket.receive",
            "text": json.dumps({"type": "turn", "text": "hello"}),
        }
    )
    await inbound.put({"type": "websocket.disconnect"})

    async def receive():
        return await inbound.get()

    async def send(message):
        outbound.append(message)

    await app(
        {"type": "websocket", "path": "/v1/conversation/attach"},
        receive,
        send,
    )

    assert outbound[0] == {"type": "websocket.accept"}
    payloads = [
        json.loads(message["text"]) for message in outbound if message["type"] == "websocket.send"
    ]
    assert payloads[0]["type"] == "session_context"
    events = [p for p in payloads if p["type"] not in ("session_context",)]
    assert events[0]["type"] == "event"
    assert events[0]["event"]["event_type"] == "text_delta"
    assert events[0]["event"]["text"] == "echo:hello"
    assert events[-1]["type"] == "result"
    assert events[-1]["result"]["terminal_reason"] == "conversation_turn_completed"


async def test_conversation_websocket_accepts_injected_items_for_next_turn() -> None:
    inbound = asyncio.Queue()
    outbound: list[dict] = []
    session = FakeSession()
    app = ConversationWebSocketApp(session)
    await inbound.put(
        {
            "type": "websocket.receive",
            "text": json.dumps(
                {
                    "type": "inject_items",
                    "items": [{"source": "ide", "content": "active file: src/app.py"}],
                }
            ),
        }
    )
    await inbound.put(
        {
            "type": "websocket.receive",
            "text": json.dumps({"type": "turn", "text": "continue"}),
        }
    )
    await inbound.put({"type": "websocket.disconnect"})

    async def receive():
        return await inbound.get()

    async def send(message):
        outbound.append(message)

    await app(
        {"type": "websocket", "path": "/v1/conversation/attach"},
        receive,
        send,
    )

    payloads = [
        json.loads(message["text"]) for message in outbound if message["type"] == "websocket.send"
    ]
    non_ctx = [p for p in payloads if p["type"] != "session_context"]
    assert non_ctx[0] == {
        "type": "injected_items_accepted",
        "count": 1,
        "sources": ["ide"],
    }
    assert session.turn_texts[0].startswith("[app-server-injected-context-v1]")
    assert "[injected_context:ide]\nactive file: src/app.py" in session.turn_texts[0]


async def test_conversation_websocket_accepts_typed_ide_context_for_next_turn(
    tmp_path,
) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("x = 1\n", encoding="utf-8")
    inbound = asyncio.Queue()
    outbound: list[dict] = []
    session = FakeSession()
    app = ConversationWebSocketApp(session, project_root=tmp_path)
    await inbound.put(
        {
            "type": "websocket.receive",
            "text": json.dumps(
                {
                    "type": "ide_context",
                    "diagnostics": {
                        "uri": source.as_uri(),
                        "diagnostics": [
                            {
                                "range": {
                                    "start": {"line": 0, "character": 0},
                                    "end": {"line": 0, "character": 1},
                                },
                                "severity": 2,
                                "source": "pyright",
                                "message": "Unused expression",
                            }
                        ],
                    },
                    "open_files": {
                        "files": [
                            {
                                "uri": source.as_uri(),
                                "active": True,
                                "cursor": {"line": 0, "character": 4},
                            }
                        ]
                    },
                }
            ),
        }
    )
    await inbound.put(
        {
            "type": "websocket.receive",
            "text": json.dumps({"type": "turn", "text": "continue"}),
        }
    )
    await inbound.put({"type": "websocket.disconnect"})

    async def receive():
        return await inbound.get()

    async def send(message):
        outbound.append(message)

    await app(
        {"type": "websocket", "path": "/v1/conversation/attach"},
        receive,
        send,
    )

    payloads = [
        json.loads(message["text"]) for message in outbound if message["type"] == "websocket.send"
    ]
    non_ctx = [p for p in payloads if p["type"] != "session_context"]
    assert non_ctx[0] == {
        "type": "ide_context_accepted",
        "count": 2,
        "sources": ["ide_diagnostics", "ide_open_files"],
    }
    assert session.turn_texts[0].startswith("[app-server-injected-context-v1]")
    assert "[injected_context:ide_diagnostics]" in session.turn_texts[0]
    assert "src/app.py:1:1: warning [pyright]: Unused expression" in session.turn_texts[0]
    assert "[injected_context:ide_open_files]" in session.turn_texts[0]
    assert "src/app.py (active, cursor=1:5)" in session.turn_texts[0]


async def test_conversation_websocket_rejects_ide_context_without_project_root() -> None:
    inbound = asyncio.Queue()
    outbound: list[dict] = []
    app = ConversationWebSocketApp(FakeSession())
    await inbound.put(
        {
            "type": "websocket.receive",
            "text": json.dumps({"type": "ide_context", "diagnostics": {"diagnostics": []}}),
        }
    )
    await inbound.put({"type": "websocket.disconnect"})

    async def receive():
        return await inbound.get()

    async def send(message):
        outbound.append(message)

    await app(
        {"type": "websocket", "path": "/v1/conversation/attach"},
        receive,
        send,
    )

    payloads = [
        json.loads(message["text"]) for message in outbound if message["type"] == "websocket.send"
    ]
    non_ctx = [p for p in payloads if p["type"] != "session_context"]
    assert non_ctx[0] == {
        "type": "error",
        "message": "ide_context requires server-configured project_root",
    }


async def test_conversation_websocket_projects_turn_attachments() -> None:
    inbound = asyncio.Queue()
    outbound: list[dict] = []
    session = FakeSession()
    app = ConversationWebSocketApp(session)
    await inbound.put(
        {
            "type": "websocket.receive",
            "text": json.dumps(
                {
                    "type": "turn",
                    "text": "summarize",
                    "attachments": [
                        {
                            "name": "notes.txt",
                            "media_type": "text/plain",
                            "content": "attachment body",
                        }
                    ],
                }
            ),
        }
    )
    await inbound.put({"type": "websocket.disconnect"})

    async def receive():
        return await inbound.get()

    async def send(message):
        outbound.append(message)

    await app(
        {"type": "websocket", "path": "/v1/conversation/attach"},
        receive,
        send,
    )

    assert session.turn_texts[0].startswith("[app-server-attachments-v1]")
    assert "[attachment:notes.txt; media_type=text/plain]" in session.turn_texts[0]
    assert "attachment body" in session.turn_texts[0]


# ── Multi-session tests ──────────────────────────────────────────


async def _run_ws_connection(
    app: ConversationWebSocketApp,
    conversation_id: str | None,
    messages: list[dict],
) -> list[dict]:
    """Drive one complete WebSocket lifecycle through the ASGI app."""
    inbound: asyncio.Queue[dict] = asyncio.Queue()
    outbound: list[dict] = []
    for msg in messages:
        await inbound.put(msg)
    await inbound.put({"type": "websocket.disconnect"})

    async def receive():
        return await inbound.get()

    async def send(message):
        outbound.append(message)

    scope: dict[str, Any] = {
        "type": "websocket",
        "path": "/v1/conversation/attach",
    }
    if conversation_id is not None:
        scope["query_string"] = f"conversation_id={conversation_id}".encode()

    await app(scope, receive, send)
    return outbound


def _ws_payloads(outbound: list[dict]) -> list[dict]:
    return [json.loads(msg["text"]) for msg in outbound if msg.get("type") == "websocket.send"]


async def test_multi_session_factory_creates_independent_controllers() -> None:
    sessions: list[FakeSession] = []

    def factory():
        s = FakeSession()
        sessions.append(s)
        return s

    app = ConversationWebSocketApp(session_factory=factory)

    turn_msg = {
        "type": "websocket.receive",
        "text": json.dumps({"type": "turn", "text": "hello"}),
    }

    out_a, out_b = await asyncio.gather(
        _run_ws_connection(app, "conv-a", [turn_msg]),
        _run_ws_connection(app, "conv-b", [turn_msg]),
    )

    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]

    payloads_a = _ws_payloads(out_a)
    payloads_b = _ws_payloads(out_b)
    assert payloads_a[-1]["type"] == "result"
    assert payloads_b[-1]["type"] == "result"

    assert sessions[0].turn_texts[0].endswith("hello")
    assert sessions[1].turn_texts[0].endswith("hello")


async def test_multi_session_rejects_duplicate_connection() -> None:
    app = ConversationWebSocketApp(session_factory=FakeSession)

    blocker_connected = asyncio.Event()
    blocker_release = asyncio.Event()

    async def blocking_connection():
        inbound: asyncio.Queue[dict] = asyncio.Queue()
        outbound: list[dict] = []

        async def receive():
            if not blocker_connected.is_set():
                blocker_connected.set()
            return await inbound.get()

        async def send(message):
            outbound.append(message)

        scope = {
            "type": "websocket",
            "path": "/v1/conversation/attach",
            "query_string": b"conversation_id=shared",
        }

        async def run():
            await app(scope, receive, send)

        task = asyncio.create_task(run())
        await blocker_connected.wait()
        await blocker_release.wait()
        await inbound.put({"type": "websocket.disconnect"})
        await task
        return outbound

    blocker_task = asyncio.create_task(blocking_connection())

    await blocker_connected.wait()

    second_out = await _run_ws_connection(app, "shared", [])

    second_payloads = _ws_payloads(second_out)
    assert any(
        p.get("type") == "error" and "already has an active connection" in p.get("message", "")
        for p in second_payloads
    )

    blocker_release.set()
    await blocker_task


async def test_session_kept_for_resume_after_disconnect() -> None:
    app = ConversationWebSocketApp(session_factory=FakeSession)

    turn_msg = {
        "type": "websocket.receive",
        "text": json.dumps({"type": "turn", "text": "hi"}),
    }

    await _run_ws_connection(app, "resumable", [turn_msg])

    assert "resumable" in app._sessions
    managed = app._sessions["resumable"]
    assert managed.connected is False
    assert managed.disconnected_at is not None


async def test_single_session_backward_compat() -> None:
    session = FakeSession()
    app = ConversationWebSocketApp(session)

    turn_msg = {
        "type": "websocket.receive",
        "text": json.dumps({"type": "turn", "text": "compat"}),
    }

    out = await _run_ws_connection(app, None, [turn_msg])
    payloads = _ws_payloads(out)

    assert payloads[-1]["type"] == "result"
    assert session.turn_texts[0].endswith("compat")


async def test_auto_generated_conversation_id() -> None:
    sessions: list[FakeSession] = []

    def factory():
        s = FakeSession()
        sessions.append(s)
        return s

    app = ConversationWebSocketApp(session_factory=factory)

    turn_msg = {
        "type": "websocket.receive",
        "text": json.dumps({"type": "turn", "text": "auto-id"}),
    }

    out = await _run_ws_connection(app, None, [turn_msg])
    payloads = _ws_payloads(out)

    assert len(sessions) == 1
    assert payloads[-1]["type"] == "result"
    assert sessions[0].turn_texts[0].endswith("auto-id")


# ── Resume and idle eviction tests ───────────────────────────────


async def test_resume_reuses_existing_session() -> None:
    sessions: list[FakeSession] = []

    def factory():
        s = FakeSession()
        sessions.append(s)
        return s

    app = ConversationWebSocketApp(session_factory=factory)

    turn1 = {
        "type": "websocket.receive",
        "text": json.dumps({"type": "turn", "text": "first"}),
    }
    turn2 = {
        "type": "websocket.receive",
        "text": json.dumps({"type": "turn", "text": "second"}),
    }

    await _run_ws_connection(app, "resume-me", [turn1])
    out2 = await _run_ws_connection(app, "resume-me", [turn2])

    assert len(sessions) == 1
    assert len(sessions[0].turn_texts) == 2
    assert sessions[0].turn_texts[0].endswith("first")
    assert sessions[0].turn_texts[1].endswith("second")

    payloads2 = _ws_payloads(out2)
    ctx2 = payloads2[0]
    assert ctx2["type"] == "session_context"
    assert ctx2["resumed"] is True


async def test_idle_sessions_evicted_on_next_acquire() -> None:
    app = ConversationWebSocketApp(session_factory=FakeSession, session_idle_timeout=0.0)

    turn_msg = {
        "type": "websocket.receive",
        "text": json.dumps({"type": "turn", "text": "bye"}),
    }

    await _run_ws_connection(app, "will-expire", [turn_msg])

    assert "will-expire" in app._sessions

    await _run_ws_connection(app, "trigger-eviction", [turn_msg])

    assert "will-expire" not in app._sessions


# ── Shared context wiring tests ─────────────────────────────────


async def test_session_context_message_sent_on_connect() -> None:
    from looplane.shared_workspace_context import SharedWorkspaceContext

    ctx = SharedWorkspaceContext(
        source_repository=Path("/fake"),
        base_sha="a" * 40,
        source_was_dirty=True,
        source_snapshot_warning="repo is dirty",
        version="v1",
        created_at=0.0,
    )
    app = ConversationWebSocketApp(session_factory=FakeSession, shared_context=ctx)

    turn_msg = {
        "type": "websocket.receive",
        "text": json.dumps({"type": "turn", "text": "hi"}),
    }
    out = await _run_ws_connection(app, "ctx-test", [turn_msg])
    payloads = _ws_payloads(out)

    session_ctx = payloads[0]
    assert session_ctx["type"] == "session_context"
    assert session_ctx["conversation_id"] == "ctx-test"
    assert session_ctx["base_sha"] == "a" * 40
    assert session_ctx["source_was_dirty"] is True
    assert session_ctx["context_version"] == "v1"
    assert session_ctx["source_snapshot_warning"] == "repo is dirty"


async def test_session_context_without_shared_context() -> None:
    app = ConversationWebSocketApp(session_factory=FakeSession)

    out = await _run_ws_connection(app, "no-ctx", [])
    payloads = _ws_payloads(out)

    session_ctx = payloads[0]
    assert session_ctx["type"] == "session_context"
    assert session_ctx["conversation_id"] == "no-ctx"
    assert "base_sha" not in session_ctx


async def test_snapshot_warning_injected_into_first_turn() -> None:
    from looplane.shared_workspace_context import SharedWorkspaceContext

    ctx = SharedWorkspaceContext(
        source_repository=Path("/fake"),
        base_sha="b" * 40,
        source_was_dirty=True,
        source_snapshot_warning="Workspace has uncommitted changes.",
        version="v2",
        created_at=0.0,
    )
    sessions: list[FakeSession] = []

    def factory():
        s = FakeSession()
        sessions.append(s)
        return s

    app = ConversationWebSocketApp(session_factory=factory, shared_context=ctx)

    turn_msg = {
        "type": "websocket.receive",
        "text": json.dumps({"type": "turn", "text": "fix the bug"}),
    }
    await _run_ws_connection(app, "inject-test", [turn_msg])

    assert len(sessions) == 1
    turn_text = sessions[0].turn_texts[0]
    assert "Workspace has uncommitted changes." in turn_text
    assert "fix the bug" in turn_text


async def test_no_snapshot_warning_when_clean() -> None:
    from looplane.shared_workspace_context import SharedWorkspaceContext

    ctx = SharedWorkspaceContext(
        source_repository=Path("/fake"),
        base_sha="c" * 40,
        source_was_dirty=False,
        source_snapshot_warning=None,
        version="v3",
        created_at=0.0,
    )
    sessions: list[FakeSession] = []

    def factory():
        s = FakeSession()
        sessions.append(s)
        return s

    app = ConversationWebSocketApp(session_factory=factory, shared_context=ctx)

    turn_msg = {
        "type": "websocket.receive",
        "text": json.dumps({"type": "turn", "text": "hello"}),
    }
    await _run_ws_connection(app, "clean-test", [turn_msg])

    turn_text = sessions[0].turn_texts[0]
    assert turn_text.endswith("hello")
