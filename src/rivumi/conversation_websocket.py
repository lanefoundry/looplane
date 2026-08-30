"""ASGI WebSocket attach surface for live conversation runtime sessions."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from rivumi.approvals import ApprovalDecision
from rivumi.conversation_controller import (
    BackendTurnLimiter,
    ConversationController,
    ConversationEventSink,
)
from rivumi.conversation_runtime import (
    ApprovalRequestedEvent,
    ConversationRuntimeEvent,
    ConversationRuntimeSession,
    RuntimeAttachment,
    RuntimeInjectedContext,
)
from rivumi.hooks import HookRunner
from rivumi.ide import (
    parse_ide_diagnostics,
    parse_ide_open_files,
    render_ide_diagnostics_context,
    render_ide_open_files_context,
)

ASGIScope = Mapping[str, Any]
ASGIReceive = Callable[[], Awaitable[dict[str, Any]]]
ASGISend = Callable[[dict[str, Any]], Awaitable[None]]


class ConversationWebSocketApp:
    """Small pure-ASGI WebSocket bridge over ``ConversationController``.

    Client messages:
    - ``{"type":"turn","text":"...","attachments":[...]}``
    - ``{"type":"inject_items","items":[{"source":"ide","content":"..."}]}``
    - ``{"type":"ide_context","diagnostics":{...},"open_files":{...}}``
    - ``{"type":"approval","request_id":"...","decision":"allow_once"}``

    Server messages:
    - ``{"type":"event","event": <ConversationRuntimeEvent>}``
    - ``{"type":"result","result": <RunResult>}``
    - ``{"type":"error","message":"..."}``
    """

    def __init__(
        self,
        session: ConversationRuntimeSession,
        *,
        path: str = "/v1/conversation/attach",
        backend_limiter: BackendTurnLimiter | None = None,
        hook_runner: HookRunner | None = None,
        project_root: str | Path | None = None,
    ) -> None:
        if not path.startswith("/"):
            raise ValueError("WebSocket path must be absolute")
        self.controller = ConversationController(
            session,
            backend_limiter=backend_limiter or BackendTurnLimiter(),
            hook_runner=hook_runner,
        )
        self.path = path
        self.project_root = Path(project_root).resolve(strict=False) if project_root else None

    async def __call__(self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        if scope.get("type") != "websocket":
            return
        if scope.get("path") != self.path:
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({"type": "websocket.accept"})
        sink = _WebSocketEventSink(send)
        try:
            while True:
                message = await receive()
                if message.get("type") == "websocket.disconnect":
                    return
                if message.get("type") != "websocket.receive":
                    continue
                payload = _parse_client_json(message)
                if payload.get("type") == "inject_items":
                    await self._handle_inject_items(payload, send)
                    continue
                if payload.get("type") == "ide_context":
                    await self._handle_ide_context(payload, send)
                    continue
                if payload.get("type") != "turn":
                    await _send_error(send, "expected turn, inject_items, or ide_context message")
                    continue
                text = payload.get("text")
                if not isinstance(text, str):
                    await _send_error(send, "turn text must be a string")
                    continue
                try:
                    attachments = _parse_attachments(payload)
                except ValueError as exc:
                    await _send_error(send, str(exc))
                    continue
                handle = self.controller.turn(
                    text,
                    event_sink=sink,
                    approval_callback=lambda event: _receive_approval(receive, event),
                    attachments=attachments,
                )
                result = await handle.run()
                await send(
                    {
                        "type": "websocket.send",
                        "text": json.dumps(
                            {"type": "result", "result": result.model_dump(mode="json")},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    }
                )
        finally:
            await self.controller.aclose()

    async def _handle_inject_items(self, payload: Mapping[str, Any], send: ASGISend) -> None:
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            await _send_error(send, "inject_items items must be a list")
            return
        try:
            items = tuple(RuntimeInjectedContext.model_validate(item) for item in raw_items)
            accepted = self.controller.inject_items(items)
        except ValueError as exc:
            await _send_error(send, str(exc))
            return
        await send(
            {
                "type": "websocket.send",
                "text": json.dumps(
                    {
                        "type": "injected_items_accepted",
                        "count": len(accepted),
                        "sources": [item.source for item in accepted],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        )

    async def _handle_ide_context(self, payload: Mapping[str, Any], send: ASGISend) -> None:
        if self.project_root is None:
            await _send_error(send, "ide_context requires server-configured project_root")
            return
        items: list[RuntimeInjectedContext] = []
        try:
            if "diagnostics" in payload:
                diagnostics = parse_ide_diagnostics(
                    payload["diagnostics"],
                    project_root=self.project_root,
                )
                content = render_ide_diagnostics_context(
                    diagnostics,
                    project_root=self.project_root,
                )
                if content:
                    items.append(RuntimeInjectedContext(source="ide_diagnostics", content=content))
            if "open_files" in payload:
                open_files = parse_ide_open_files(
                    payload["open_files"],
                    project_root=self.project_root,
                )
                content = render_ide_open_files_context(
                    open_files,
                    project_root=self.project_root,
                )
                if content:
                    items.append(RuntimeInjectedContext(source="ide_open_files", content=content))
            if not items:
                raise ValueError("ide_context requires non-empty diagnostics or open_files context")
            accepted = self.controller.inject_items(tuple(items))
        except ValueError as exc:
            await _send_error(send, str(exc))
            return
        await send(
            {
                "type": "websocket.send",
                "text": json.dumps(
                    {
                        "type": "ide_context_accepted",
                        "count": len(accepted),
                        "sources": [item.source for item in accepted],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        )


class _WebSocketEventSink(ConversationEventSink):
    def __init__(self, send: ASGISend) -> None:
        self._send = send

    async def emit(self, event: ConversationRuntimeEvent) -> None:
        await self._send(
            {
                "type": "websocket.send",
                "text": json.dumps(
                    {"type": "event", "event": event.model_dump(mode="json")},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        )


def _parse_client_json(message: Mapping[str, Any]) -> dict[str, Any]:
    text = message.get("text")
    if not isinstance(text, str):
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_attachments(payload: Mapping[str, Any]) -> tuple[RuntimeAttachment, ...]:
    raw_attachments = payload.get("attachments", [])
    if raw_attachments is None:
        return ()
    if not isinstance(raw_attachments, list):
        raise ValueError("turn attachments must be a list")
    if len(raw_attachments) > 16:
        raise ValueError("at most 16 attachments can be supplied for one turn")
    return tuple(RuntimeAttachment.model_validate(item) for item in raw_attachments)


async def _receive_approval(
    receive: ASGIReceive,
    event: ApprovalRequestedEvent,
) -> ApprovalDecision:
    while True:
        message = await receive()
        if message.get("type") == "websocket.disconnect":
            return ApprovalDecision.CANCEL
        payload = _parse_client_json(message)
        if payload.get("type") != "approval":
            continue
        if payload.get("request_id") != event.approval.request_id:
            continue
        try:
            return ApprovalDecision(payload.get("decision"))
        except ValueError:
            return ApprovalDecision.CANCEL


async def _send_error(send: ASGISend, message: str) -> None:
    await send(
        {
            "type": "websocket.send",
            "text": json.dumps(
                {"type": "error", "message": message},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
    )
