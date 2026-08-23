from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from coding_agent.approvals import ApprovalDecision
from coding_agent.codex_conversation import IsolatedCodexConversation
from coding_agent.conversation_runtime import ApprovalRequestedEvent, TurnCompletedEvent


async def main() -> None:
    source = (
        Path(sys.argv[1]).resolve()
        if len(sys.argv) > 1
        else Path(__file__).resolve().parents[1]
    )
    session = IsolatedCodexConversation(source)
    await session.start()
    try:
        turn_id = await session.send_turn(
            "Reply with exactly PCA_SMOKE_OK. Do not inspect files and do not use tools."
        )
        text: list[str] = []
        terminal = None
        async for event in session.events():
            if event.turn_id != turn_id:
                raise RuntimeError("unexpected turn correlation")
            if event.event_type == "text_delta":
                text.append(event.text)
            elif isinstance(event, ApprovalRequestedEvent):
                await session.respond_approval(
                    event.approval.request_id, ApprovalDecision.DENY
                )
            elif isinstance(event, TurnCompletedEvent):
                terminal = event
                break
        print(
            json.dumps(
                {
                    "text": "".join(text),
                    "status": terminal.status.value if terminal else None,
                    "error": terminal.error if terminal else "missing terminal",
                },
                ensure_ascii=False,
            )
        )
    finally:
        await session.aclose()


if __name__ == "__main__":
    asyncio.run(main())
