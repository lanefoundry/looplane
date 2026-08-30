from __future__ import annotations

import asyncio
import json
import socket

import uvicorn
import websockets

from rivumi.conversation_runtime import RuntimeTurnStatus, TextDeltaEvent, TurnCompletedEvent
from rivumi.conversation_websocket import ConversationWebSocketApp
from rivumi.runtime_semantics import RuntimeCapabilities


class SmokeSession:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[object] = asyncio.Queue()

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(native_compaction=False)

    async def start(self) -> None:
        return None

    async def send_turn(self, text: str) -> str:
        await self.queue.put(TextDeltaEvent(sequence=0, turn_id="turn-1", text=f"echo:{text}"))
        await self.queue.put(
            TurnCompletedEvent(
                sequence=1,
                turn_id="turn-1",
                status=RuntimeTurnStatus.COMPLETED,
            )
        )
        return "turn-1"

    async def compact_context(self, guidance: str | None = None) -> str:
        raise RuntimeError("native compaction is unavailable")

    async def events(self):
        while True:
            yield await self.queue.get()

    async def respond_approval(self, request_id: str, decision: str) -> None:
        raise AssertionError("approval requests are not part of this smoke")

    async def interrupt(self, turn_id: str) -> None:
        return None

    async def aclose(self) -> None:
        return None


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _run_smoke() -> None:
    port = _unused_loopback_port()
    config = uvicorn.Config(
        ConversationWebSocketApp(SmokeSession()),
        host="127.0.0.1",
        port=port,
        lifespan="off",
        log_level="warning",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        if not server.started:
            raise RuntimeError("conversation WebSocket smoke server did not start")

        async with websockets.connect(
            f"ws://127.0.0.1:{port}/v1/conversation/attach",
            open_timeout=5,
        ) as websocket:
            await websocket.send(json.dumps({"type": "turn", "text": "smoke"}))
            messages: list[dict[str, object]] = []
            for _ in range(4):
                messages.append(json.loads(await asyncio.wait_for(websocket.recv(), timeout=5)))
                if messages[-1].get("type") == "result":
                    break

        event = next(message for message in messages if message.get("type") == "event")
        result = next(message for message in messages if message.get("type") == "result")
        assert event["event"]["text"] == "echo:smoke"
        assert result["result"]["terminal_reason"] == "conversation_turn_completed"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


def main() -> None:
    asyncio.run(_run_smoke())


if __name__ == "__main__":
    main()
