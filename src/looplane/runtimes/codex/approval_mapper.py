"""Pure Codex approval decision and wire-result mapping."""

from __future__ import annotations

from typing import Any

from looplane.approvals import ApprovalDecision
from looplane.conversation_runtime import ConversationProtocolError, RuntimeApprovalKind


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
