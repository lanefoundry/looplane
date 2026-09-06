"""ASGI WebSocket attach surface for live conversation runtime sessions."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from looplane.approvals import ApprovalDecision
from looplane.conversation_controller import (
    BackendTurnLimiter,
    ConversationController,
    ConversationEventSink,
)
from looplane.conversation_runtime import (
    ApprovalRequestedEvent,
    ConversationRuntimeEvent,
    ConversationRuntimeSession,
    RuntimeAttachment,
    RuntimeInjectedContext,
)
from looplane.hooks import HookRunner
from looplane.ide import (
    parse_ide_diagnostics,
    parse_ide_open_files,
    render_ide_diagnostics_context,
    render_ide_open_files_context,
)
from looplane.shared_workspace_context import SharedWorkspaceContext

ASGIScope = Mapping[str, Any]
ASGIReceive = Callable[[], Awaitable[dict[str, Any]]]
ASGISend = Callable[[dict[str, Any]], Awaitable[None]]

SessionFactory = Callable[[], ConversationRuntimeSession]


@dataclass
class _ManagedSession:
    conversation_id: str
    controller: ConversationController
    connected: bool = False
    disconnected_at: float | None = None


class ConversationWebSocketApp:
    """Pure-ASGI WebSocket bridge over per-conversation ``ConversationController`` instances.

    Each WebSocket connection is routed to an independent controller identified
    by a ``conversation_id`` query parameter.  When no id is supplied the server
    generates one automatically.

    Two construction modes are supported for backward compatibility:

    *   **Legacy single-session** – pass a pre-built session as the first
        positional argument.  Only one conversation is allowed.
    *   **Multi-session factory** – pass ``session_factory`` as a keyword
        argument.  A new session is created for every distinct conversation id.

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
        session: ConversationRuntimeSession | None = None,
        *,
        session_factory: SessionFactory | None = None,
        path: str = "/v1/conversation/attach",
        backend_limiter: BackendTurnLimiter | None = None,
        hook_runner: HookRunner | None = None,
        project_root: str | Path | None = None,
        shared_context: SharedWorkspaceContext | None = None,
        session_idle_timeout: float = 300.0,
    ) -> None:
        if not path.startswith("/"):
            raise ValueError("WebSocket path must be absolute")
        if session is not None and session_factory is not None:
            raise ValueError("pass session or session_factory, not both")
        if session is None and session_factory is None:
            raise ValueError("either session or session_factory is required")

        if session is not None:
            _captured = session
            _used = False

            def _one_shot_factory() -> ConversationRuntimeSession:
                nonlocal _used
                if _used:
                    raise RuntimeError("single-session app: only one conversation is supported")
                _used = True
                return _captured

            self._session_factory: SessionFactory = _one_shot_factory
        else:
            assert session_factory is not None
            self._session_factory = session_factory

        self._backend_limiter = backend_limiter or BackendTurnLimiter()
        self._hook_runner = hook_runner
        self.path = path
        self.project_root = Path(project_root).resolve(strict=False) if project_root else None
        self.shared_context = shared_context
        self._session_idle_timeout = session_idle_timeout
        self._sessions: dict[str, _ManagedSession] = {}
        self._sessions_lock = asyncio.Lock()

    # ── ASGI entry point ─────────────────────────────────────────

    async def __call__(self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        if scope.get("type") != "websocket":
            return
        if scope.get("path") != self.path:
            await send({"type": "websocket.close", "code": 1008})
            return

        conversation_id = _parse_conversation_id(scope)
        managed = await self._acquire_session(conversation_id)
        if managed is None:
            await send({"type": "websocket.accept"})
            await _send_error(send, "conversation already has an active connection")
            await send({"type": "websocket.close", "code": 1008})
            return

        is_resume = managed.controller._started
        await send({"type": "websocket.accept"})
        await self._send_session_context(send, conversation_id, resumed=is_resume)
        sink = _WebSocketEventSink(send)
        controller = managed.controller
        if not is_resume:
            self._inject_snapshot_warning(controller)
        try:
            while True:
                message = await receive()
                if message.get("type") == "websocket.disconnect":
                    return
                if message.get("type") != "websocket.receive":
                    continue
                payload = _parse_client_json(message)
                if payload.get("type") == "inject_items":
                    await self._handle_inject_items(payload, send, controller)
                    continue
                if payload.get("type") == "ide_context":
                    await self._handle_ide_context(payload, send, controller)
                    continue
                if payload.get("type") != "turn":
                    await _send_error(
                        send,
                        "expected turn, inject_items, or ide_context message",
                    )
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
                handle = controller.turn(
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
                            {
                                "type": "result",
                                "result": result.model_dump(mode="json"),
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    }
                )
        finally:
            await self._release_session(conversation_id)

    # ── Session lifecycle ────────────────────────────────────────

    async def _acquire_session(self, conversation_id: str) -> _ManagedSession | None:
        async with self._sessions_lock:
            await self._evict_idle_sessions_locked()
            if conversation_id in self._sessions:
                managed = self._sessions[conversation_id]
                if managed.connected:
                    return None
                managed.connected = True
                managed.disconnected_at = None
                return managed
            session = self._session_factory()
            controller = ConversationController(
                session,
                backend_limiter=self._backend_limiter,
                hook_runner=self._hook_runner,
            )
            managed = _ManagedSession(
                conversation_id=conversation_id,
                controller=controller,
                connected=True,
            )
            self._sessions[conversation_id] = managed
            return managed

    async def _release_session(self, conversation_id: str) -> None:
        async with self._sessions_lock:
            managed = self._sessions.get(conversation_id)
            if managed is not None:
                managed.connected = False
                managed.disconnected_at = time.monotonic()

    async def _evict_idle_sessions_locked(self) -> None:
        now = time.monotonic()
        expired = [
            cid
            for cid, m in self._sessions.items()
            if not m.connected
            and m.disconnected_at is not None
            and (now - m.disconnected_at) > self._session_idle_timeout
        ]
        for cid in expired:
            managed = self._sessions.pop(cid)
            await managed.controller.aclose()

    # ── Shared context ──────────────────────────────────────────

    async def _send_session_context(
        self, send: ASGISend, conversation_id: str, *, resumed: bool = False
    ) -> None:
        payload: dict[str, Any] = {
            "type": "session_context",
            "conversation_id": conversation_id,
            "resumed": resumed,
        }
        ctx = self.shared_context
        if ctx is not None:
            payload["base_sha"] = ctx.base_sha
            payload["source_was_dirty"] = ctx.source_was_dirty
            payload["context_version"] = ctx.version
            if ctx.source_snapshot_warning:
                payload["source_snapshot_warning"] = ctx.source_snapshot_warning
        await send(
            {
                "type": "websocket.send",
                "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            }
        )

    def _inject_snapshot_warning(self, controller: ConversationController) -> None:
        ctx = self.shared_context
        if ctx is None or not ctx.source_snapshot_warning:
            return
        controller.inject_items(
            (
                RuntimeInjectedContext(
                    source="workspace_snapshot",
                    content=ctx.source_snapshot_warning,
                ),
            )
        )

    # ── Message handlers ─────────────────────────────────────────

    async def _handle_inject_items(
        self,
        payload: Mapping[str, Any],
        send: ASGISend,
        controller: ConversationController,
    ) -> None:
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            await _send_error(send, "inject_items items must be a list")
            return
        try:
            items = tuple(RuntimeInjectedContext.model_validate(item) for item in raw_items)
            accepted = controller.inject_items(items)
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

    async def _handle_ide_context(
        self,
        payload: Mapping[str, Any],
        send: ASGISend,
        controller: ConversationController,
    ) -> None:
        if self.project_root is None:
            await _send_error(
                send,
                "ide_context requires server-configured project_root",
            )
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
            accepted = controller.inject_items(tuple(items))
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


def _parse_conversation_id(scope: ASGIScope) -> str:
    qs = scope.get("query_string", b"")
    if isinstance(qs, str):
        qs = qs.encode("latin-1")
    params = urllib.parse.parse_qs(qs.decode("latin-1"))
    ids = params.get("conversation_id", [])
    if ids and ids[0]:
        return ids[0]
    return uuid4().hex


def _parse_client_json(message: Mapping[str, Any]) -> dict[str, Any]:
    text = message.get("text")
    if not isinstance(text, str):
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_attachments(
    payload: Mapping[str, Any],
) -> tuple[RuntimeAttachment, ...]:
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
