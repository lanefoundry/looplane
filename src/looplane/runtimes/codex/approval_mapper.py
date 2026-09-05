"""Owned approval request correlation and canonical/wire conversion."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from looplane.approvals import ApprovalDecision, ToolEffect
from looplane.conversation_runtime import (
    ApprovalRequestedEvent,
    ConversationProtocolError,
    RuntimeApprovalKind,
    RuntimeApprovalRequest,
)
from looplane.conversation_runtime import RuntimeToolStatus as RuntimeToolStatus
from looplane.runtime_semantics import (
    ProposedChange,
)
from looplane.runtimes.codex import parsing as _codex_parsing
from looplane.runtimes.codex.correlation import CodexCorrelation
from looplane.runtimes.codex.event_mapper import EventEmitter

_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval": RuntimeApprovalKind.COMMAND,
    "item/fileChange/requestApproval": RuntimeApprovalKind.FILE_CHANGE,
    "item/permissions/requestApproval": RuntimeApprovalKind.PERMISSIONS,
}


@dataclass(frozen=True)
class PendingApproval:
    wire_id: int | str
    method: str
    turn_id: str
    requested_permissions: dict[str, Any] | None
    available: tuple[ApprovalDecision, ...]


def available_decisions(kind: RuntimeApprovalKind, raw: object) -> tuple[ApprovalDecision, ...]:
    if kind == RuntimeApprovalKind.PERMISSIONS:
        return (
            ApprovalDecision.ALLOW_ONCE,
            ApprovalDecision.ALLOW_SESSION,
            ApprovalDecision.DENY,
            ApprovalDecision.CANCEL,
        )
    mapping = {
        "accept": ApprovalDecision.ALLOW_ONCE,
        "acceptForSession": ApprovalDecision.ALLOW_SESSION,
        "decline": ApprovalDecision.DENY,
        "cancel": ApprovalDecision.CANCEL,
    }
    if raw is None:
        return tuple(mapping.values())
    if not isinstance(raw, list):
        raise ConversationProtocolError("availableDecisions must be a list")
    result: list[ApprovalDecision] = []
    for value in raw:
        if isinstance(value, str) and value in mapping and mapping[value] not in result:
            result.append(mapping[value])
    if not result:
        raise ConversationProtocolError("approval exposes no supported decision")
    return tuple(result)


def approval_result(
    method: str, requested_permissions: dict[str, Any] | None, decision: ApprovalDecision
) -> dict[str, Any]:
    if method == "item/permissions/requestApproval":
        permissions: dict[str, Any] = {}
        if decision in {ApprovalDecision.ALLOW_ONCE, ApprovalDecision.ALLOW_SESSION}:
            for key, value in (requested_permissions or {}).items():
                if key in {"network", "fileSystem"} and value is not None:
                    permissions[key] = value
        return {
            "permissions": permissions,
            "scope": ("session" if decision == ApprovalDecision.ALLOW_SESSION else "turn"),
            "strictAutoReview": True,
        }
    mapping = {
        ApprovalDecision.ALLOW_ONCE: "accept",
        ApprovalDecision.ALLOW_SESSION: "acceptForSession",
        ApprovalDecision.DENY: "decline",
        ApprovalDecision.CANCEL: "cancel",
    }
    return {"decision": mapping[decision]}


class CodexApprovalMapper:
    def __init__(
        self,
        *,
        correlation: CodexCorrelation,
        emit: EventEmitter,
        bounded: Callable[[str], str],
        new_id: Callable[[], str],
        action_context: Callable[[str], tuple[str, tuple[ProposedChange, ...], str | None]],
    ) -> None:
        self.correlation = correlation
        self.emit = emit
        self.bounded = bounded
        self.new_id = new_id
        self.action_context = action_context
        self.pending: dict[str, PendingApproval] = {}
        self.wire_ids: set[int | str] = set()

    def handle_server_request(self, method: str, wire_id: object, params: dict[str, Any]) -> None:
        if method not in _APPROVAL_METHODS:
            raise ConversationProtocolError(f"unsupported server request: {method}")
        if not isinstance(wire_id, (int, str)) or isinstance(wire_id, bool):
            raise ConversationProtocolError("server request has invalid id")
        if wire_id in self.wire_ids:
            raise ConversationProtocolError("duplicate approval request id")
        if params.get("threadId") != self.correlation.native_thread_id:
            raise ConversationProtocolError("approval request references the wrong thread")
        native_turn = params.get("turnId")
        native_item = params.get("itemId")
        if not _codex_parsing.safe_id(native_turn) or not _codex_parsing.safe_id(native_item):
            raise ConversationProtocolError("approval request has invalid correlation ids")
        local_turn = self.correlation.local_turn(native_turn, context=f"server request {method}")
        action_id = self.correlation.local_action(native_turn, native_item)
        kind = _APPROVAL_METHODS[method]
        available = self.available_decisions(kind, params.get("availableDecisions"))
        request_id = self.new_id()
        requested_permissions = (
            params.get("permissions") if kind == RuntimeApprovalKind.PERMISSIONS else None
        )
        if requested_permissions is not None and not isinstance(requested_permissions, dict):
            raise ConversationProtocolError("permissions request is malformed")
        pending = PendingApproval(
            wire_id=wire_id,
            method=method,
            turn_id=local_turn,
            requested_permissions=requested_permissions,
            available=available,
        )
        self.pending[request_id] = pending
        self.wire_ids.add(wire_id)
        fallback, changes, grant_scope = self.action_context(action_id)
        preview = self.approval_preview(
            kind,
            params,
            fallback=fallback,
        )
        approval = RuntimeApprovalRequest(
            request_id=request_id,
            turn_id=local_turn,
            action_id=action_id,
            kind=kind,
            effect=(
                ToolEffect.MODIFY if kind == RuntimeApprovalKind.FILE_CHANGE else ToolEffect.EXECUTE
            ),
            preview=preview,
            proposed_changes=changes,
            grant_scope=(grant_scope if kind == RuntimeApprovalKind.FILE_CHANGE else None),
            available_decisions=available,
        )
        self.emit(ApprovalRequestedEvent, turn_id=local_turn, approval=approval)

    def approval_preview(
        self,
        kind: RuntimeApprovalKind,
        params: dict[str, Any],
        *,
        fallback: str = "",
    ) -> str:
        if kind == RuntimeApprovalKind.COMMAND:
            labels = (
                ("Command", params.get("command")),
                ("Working directory", params.get("cwd")),
                ("Reason", params.get("reason")),
            )
        elif kind == RuntimeApprovalKind.FILE_CHANGE:
            labels = (
                ("Reason", params.get("reason")),
                ("Grant root", params.get("grantRoot")),
            )
        else:
            labels = (
                ("Reason", params.get("reason")),
                ("Working directory", params.get("cwd")),
            )
        details = [
            f"{label}: {value}" for label, value in labels if isinstance(value, str) and value
        ]
        if fallback:
            details.insert(0, fallback)
        if not details:
            details = [
                f"Action: {kind.value.replace('_', ' ')}",
                "Details: The runtime did not identify the command, files, or working directory.",
            ]
        return self.bounded("\n".join(details))[:16000]

    available_decisions = staticmethod(available_decisions)

    @staticmethod
    def approval_result(pending: PendingApproval, decision: ApprovalDecision) -> dict[str, Any]:
        return approval_result(pending.method, pending.requested_permissions, decision)
