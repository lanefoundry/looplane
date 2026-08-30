from __future__ import annotations

import asyncio
import json

from rivumi.conversation_runtime import RuntimeTurnStatus, TextDeltaEvent, TurnCompletedEvent
from rivumi.conversation_websocket import ConversationWebSocketApp
from rivumi.runtime_semantics import RuntimeCapabilities


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
        json.loads(message["text"])
        for message in outbound
        if message["type"] == "websocket.send"
    ]
    assert payloads[0]["type"] == "event"
    assert payloads[0]["event"]["event_type"] == "text_delta"
    assert payloads[0]["event"]["text"] == "echo:hello"
    assert payloads[-1]["type"] == "result"
    assert payloads[-1]["result"]["terminal_reason"] == "conversation_turn_completed"


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
        json.loads(message["text"])
        for message in outbound
        if message["type"] == "websocket.send"
    ]
    assert payloads[0] == {
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
        json.loads(message["text"])
        for message in outbound
        if message["type"] == "websocket.send"
    ]
    assert payloads[0] == {
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
        json.loads(message["text"])
        for message in outbound
        if message["type"] == "websocket.send"
    ]
    assert payloads[0] == {
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
