from __future__ import annotations

import pytest
from pydantic import ValidationError

from looplane.approvals import ApprovalDecision, ToolEffect
from looplane.conversation_runtime import (
    CONVERSATION_RUNTIME_EVENT_ADAPTER,
    ActionPreviewUpdatedEvent,
    ApprovalRequestedEvent,
    CompactionCompletedEvent,
    CompactionStartedEvent,
    ContextUsageUpdatedEvent,
    RuntimeApprovalKind,
    RuntimeApprovalRequest,
    RuntimeCapabilities,
    RuntimeModelUpdatedEvent,
    RuntimeSkillsChangedEvent,
    RuntimeToolKind,
    TextDeltaEvent,
    ToolStartedEvent,
    TurnCompletedEvent,
)
from looplane.runtime_semantics import ContextTelemetry, ProposedChange


def test_event_contract_is_strict_and_discriminated() -> None:
    event = CONVERSATION_RUNTIME_EVENT_ADAPTER.validate_python(
        {"event_type": "text_delta", "sequence": 0, "turn_id": "turn", "text": "hi"}
    )
    assert isinstance(event, TextDeltaEvent)

    with pytest.raises(ValidationError):
        CONVERSATION_RUNTIME_EVENT_ADAPTER.validate_python(
            {
                "event_type": "text_delta",
                "sequence": 0,
                "turn_id": "turn",
                "text": "hi",
                "vendor_id": "must-not-cross-boundary",
            }
        )
    with pytest.raises(ValidationError):
        CONVERSATION_RUNTIME_EVENT_ADAPTER.validate_python(
            {"event_type": "vendor_magic", "sequence": 0, "turn_id": "turn"}
        )


def test_typed_tool_event_has_closed_renderer_fields() -> None:
    event = ToolStartedEvent(
        sequence=2,
        turn_id="turn",
        action_id="action",
        kind=RuntimeToolKind.COMMAND,
        tool_name="shell",
        effect=ToolEffect.EXECUTE,
        summary="pytest -q",
        path="/workspace",
    )
    assert event.kind == RuntimeToolKind.COMMAND
    assert event.summary == "pytest -q"
    assert not hasattr(event, "data")

    with pytest.raises(ValidationError):
        ToolStartedEvent(
            sequence=2,
            turn_id="turn",
            action_id="action",
            kind=RuntimeToolKind.COMMAND,
            tool_name="shell",
            effect=ToolEffect.EXECUTE,
            summary="x" * 16_001,
        )


def test_approval_is_inline_correlated_without_vendor_metadata() -> None:
    approval = RuntimeApprovalRequest(
        request_id="approval",
        turn_id="turn",
        action_id="action",
        kind=RuntimeApprovalKind.FILE_CHANGE,
        effect=ToolEffect.MODIFY,
        preview="edit src/example.py",
        available_decisions=(ApprovalDecision.ALLOW_ONCE, ApprovalDecision.DENY),
    )
    event = ApprovalRequestedEvent(
        sequence=1,
        turn_id="turn",
        approval=approval,
    )
    assert event.approval.action_id == "action"
    assert not hasattr(event.approval, "provider_metadata")

    with pytest.raises(ValidationError):
        ApprovalRequestedEvent(sequence=1, turn_id="other", approval=approval)


def test_approval_decisions_are_nonempty_and_unique() -> None:
    common = {
        "request_id": "approval",
        "turn_id": "turn",
        "action_id": "action",
        "kind": RuntimeApprovalKind.COMMAND,
        "effect": ToolEffect.EXECUTE,
    }
    with pytest.raises(ValidationError):
        RuntimeApprovalRequest(**common, available_decisions=())
    with pytest.raises(ValidationError):
        RuntimeApprovalRequest(
            **common,
            available_decisions=(ApprovalDecision.DENY, ApprovalDecision.DENY),
        )


def test_terminal_contract_requires_error_only_for_failure() -> None:
    with pytest.raises(ValidationError):
        TurnCompletedEvent(
            sequence=3,
            turn_id="turn",
            status="failed",
        )
    with pytest.raises(ValidationError):
        TurnCompletedEvent(
            sequence=3,
            turn_id="turn",
            status="completed",
            error="unexpected",
        )


def test_context_usage_event_carries_explicit_telemetry_accuracy() -> None:
    event = CONVERSATION_RUNTIME_EVENT_ADAPTER.validate_python(
        {
            "event_type": "context_usage_updated",
            "sequence": 4,
            "turn_id": "turn",
            "telemetry": {
                "accuracy": "estimated",
                "input_tokens": 40,
                "output_tokens": 2,
                "total_tokens": 42,
                "context_window": 100,
            },
        }
    )

    assert isinstance(event, ContextUsageUpdatedEvent)
    assert event.telemetry.accuracy == "estimated"


def test_runtime_model_event_is_bounded_and_provider_neutral() -> None:
    event = CONVERSATION_RUNTIME_EVENT_ADAPTER.validate_python(
        {
            "event_type": "runtime_model_updated",
            "sequence": 5,
            "turn_id": "turn",
            "model": "claude-opus-4-5",
        }
    )

    assert isinstance(event, RuntimeModelUpdatedEvent)
    assert event.model == "claude-opus-4-5"


def test_runtime_skills_changed_event_is_bounded_and_provider_neutral() -> None:
    event = CONVERSATION_RUNTIME_EVENT_ADAPTER.validate_python(
        {
            "event_type": "runtime_skills_changed",
            "sequence": 6,
            "turn_id": "turn",
            "source": "project skills",
            "skill_names": ("review", "test-writer"),
            "summary": "Runtime skill set changed.",
        }
    )

    assert isinstance(event, RuntimeSkillsChangedEvent)
    assert event.skill_names == ("review", "test-writer")
    assert event.source == "project skills"


def test_action_preview_and_approval_share_bounded_proposed_changes() -> None:
    change = ProposedChange(
        change_id="change",
        action_id="action",
        kind="update",
        paths=("src/example.py",),
        unified_diff="+new line\n",
        original_diff_bytes=10,
    )
    event = ActionPreviewUpdatedEvent(
        sequence=2,
        turn_id="turn",
        action_id="action",
        proposed_changes=(change,),
    )
    approval = RuntimeApprovalRequest(
        request_id="approval",
        turn_id="turn",
        action_id="action",
        kind=RuntimeApprovalKind.FILE_CHANGE,
        effect=ToolEffect.MODIFY,
        proposed_changes=(change,),
        grant_scope=" file-change:src/example.py ",
        available_decisions=(ApprovalDecision.ALLOW_SESSION, ApprovalDecision.DENY),
    )

    assert event.proposed_changes == approval.proposed_changes
    assert approval.grant_scope == "file-change:src/example.py"

    with pytest.raises(ValidationError, match="preview action"):
        ActionPreviewUpdatedEvent(
            sequence=2,
            turn_id="turn",
            action_id="other-action",
            proposed_changes=(change,),
        )
    with pytest.raises(ValidationError, match="approval action"):
        RuntimeApprovalRequest(
            request_id="approval",
            turn_id="turn",
            action_id="other-action",
            kind=RuntimeApprovalKind.FILE_CHANGE,
            effect=ToolEffect.MODIFY,
            proposed_changes=(change,),
            available_decisions=(ApprovalDecision.DENY,),
        )


def test_compaction_events_do_not_fabricate_native_checkpoint_data() -> None:
    started = CompactionStartedEvent(
        sequence=5,
        turn_id="compact-operation",
        guidance=" retain failing checks ",
    )
    completed = CompactionCompletedEvent(
        sequence=6,
        turn_id="compact-operation",
    )

    assert started.guidance == "retain failing checks"
    assert completed.checkpoint is None

    with pytest.raises(ValidationError, match="cannot be blank"):
        CompactionStartedEvent(
            sequence=5,
            turn_id="compact-operation",
            guidance="   ",
        )


def test_runtime_capabilities_are_conservative_by_default() -> None:
    capabilities = RuntimeCapabilities()

    assert capabilities == RuntimeCapabilities(
        token_usage=False,
        native_compaction=False,
        proposed_file_preview=False,
        structured_approvals=False,
        queued_submissions=False,
        steer_active_turn=False,
        background_task_management=False,
    )


def test_context_event_contract_remains_provider_neutral() -> None:
    with pytest.raises(ValidationError):
        ContextUsageUpdatedEvent(
            sequence=1,
            turn_id="turn",
            telemetry=ContextTelemetry(
                accuracy="exact",
                input_tokens=8,
                output_tokens=2,
                total_tokens=10,
                context_window=100,
            ),
            provider_thread_id="opaque",
        )
