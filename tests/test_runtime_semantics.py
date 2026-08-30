from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from looplane.approvals import ToolEffect
from looplane.runtime_semantics import (
    TASK_STATE_ADAPTER,
    BackgroundTaskState,
    ContextCheckpoint,
    ContextSummary,
    ContextTelemetry,
    ContextTelemetryAccuracy,
    ForegroundTaskState,
    PermissionDecision,
    PermissionMode,
    ProcessLocalGrant,
    ProposedChange,
    ProposedChangeKind,
    QueuedTaskState,
    RuntimeCapabilities,
    TaskStatus,
    decide_permission,
    history_summary_fallback_span,
    input_cache_hit_rate,
    should_apply_history_summary_fallback,
    should_auto_compact_context,
    should_inject_workspace_context_reminder,
    should_remind_context_pressure,
)


def telemetry(total: int, *, accuracy: str = "exact") -> ContextTelemetry:
    return ContextTelemetry(
        accuracy=accuracy,
        input_tokens=total - 2,
        output_tokens=2,
        total_tokens=total,
        context_window=100,
    )


def test_context_telemetry_distinguishes_exact_from_estimated_counts() -> None:
    exact = telemetry(12)
    estimated = telemetry(12, accuracy="estimated")

    assert exact.accuracy == ContextTelemetryAccuracy.EXACT
    assert estimated.accuracy == ContextTelemetryAccuracy.ESTIMATED

    with pytest.raises(ValidationError, match="must equal"):
        ContextTelemetry(
            accuracy="exact",
            input_tokens=8,
            output_tokens=2,
            total_tokens=11,
            context_window=100,
        )
    with pytest.raises(ValidationError, match="cannot exceed"):
        ContextTelemetry(
            accuracy="estimated",
            input_tokens=90,
            output_tokens=20,
            total_tokens=110,
            context_window=100,
        )


def test_input_cache_hit_rate_uses_cached_input_subset() -> None:
    assert input_cache_hit_rate(input_tokens=1_000, cached_input_tokens=250) == 0.25
    assert input_cache_hit_rate(input_tokens=0, cached_input_tokens=0) is None
    assert (
        ContextTelemetry(
            accuracy="exact",
            input_tokens=1_000,
            cached_input_tokens=400,
            output_tokens=100,
            total_tokens=1_100,
            context_window=2_000,
        ).input_cache_hit_rate
        == 0.4
    )

    with pytest.raises(ValueError, match="cannot exceed"):
        input_cache_hit_rate(input_tokens=100, cached_input_tokens=101)


def test_context_checkpoint_separates_summarized_and_retained_turns() -> None:
    checkpoint = ContextCheckpoint(
        checkpoint_id="cp-1",
        summary=ContextSummary(
            summary_id="summary-1",
            text="Implemented the parser and retained the failing test details.",
            source_turn_ids=("turn-1", "turn-2"),
        ),
        retained_turn_ids=("turn-3",),
        telemetry_before=telemetry(80),
        telemetry_after=telemetry(30, accuracy="estimated"),
    )

    assert checkpoint.summary.source_turn_ids == ("turn-1", "turn-2")
    assert checkpoint.retained_turn_ids == ("turn-3",)

    with pytest.raises(ValidationError, match="must be disjoint"):
        ContextCheckpoint(
            checkpoint_id="cp-2",
            summary=ContextSummary(
                summary_id="summary-2",
                text="Summary",
                source_turn_ids=("turn-1",),
            ),
            retained_turn_ids=("turn-1",),
            telemetry_before=telemetry(80),
            telemetry_after=telemetry(30),
        )
    with pytest.raises(ValidationError, match="cannot increase"):
        ContextCheckpoint(
            checkpoint_id="cp-3",
            summary=ContextSummary(
                summary_id="summary-3",
                text="Summary",
                source_turn_ids=("turn-1",),
            ),
            telemetry_before=telemetry(30),
            telemetry_after=telemetry(40),
        )


def test_auto_compaction_policy_requires_native_capability_and_window() -> None:
    capabilities = RuntimeCapabilities(native_compaction=True)

    assert should_auto_compact_context(telemetry(85), capabilities) is True
    assert should_auto_compact_context(telemetry(84), capabilities) is False
    assert (
        should_auto_compact_context(
            ContextTelemetry(
                accuracy="exact",
                input_tokens=90,
                output_tokens=0,
                total_tokens=90,
                context_window=None,
            ),
            capabilities,
        )
        is False
    )
    assert (
        should_auto_compact_context(telemetry(90), RuntimeCapabilities(native_compaction=False))
        is False
    )


def test_context_pressure_reminder_policy_uses_task_token_limit_only() -> None:
    assert should_remind_context_pressure(total_tokens=85, max_total_tokens=100) is True
    assert should_remind_context_pressure(total_tokens=84, max_total_tokens=100) is False
    assert should_remind_context_pressure(total_tokens=99, max_total_tokens=None) is False

    with pytest.raises(ValueError, match="trigger_ratio"):
        should_remind_context_pressure(
            total_tokens=1,
            max_total_tokens=100,
            trigger_ratio=0,
        )
    with pytest.raises(ValueError, match="negative"):
        should_remind_context_pressure(total_tokens=-1, max_total_tokens=100)


def test_history_summary_fallback_policy_requires_pressure_and_old_history() -> None:
    assert (
        should_apply_history_summary_fallback(
            total_tokens=85,
            max_total_tokens=100,
            message_count=8,
            already_applied=False,
        )
        is True
    )
    assert (
        should_apply_history_summary_fallback(
            total_tokens=84,
            max_total_tokens=100,
            message_count=8,
            already_applied=False,
        )
        is False
    )
    assert (
        should_apply_history_summary_fallback(
            total_tokens=85,
            max_total_tokens=100,
            message_count=5,
            already_applied=False,
        )
        is False
    )
    assert (
        should_apply_history_summary_fallback(
            total_tokens=85,
            max_total_tokens=100,
            message_count=8,
            already_applied=True,
        )
        is False
    )


def test_history_summary_fallback_span_keeps_seed_and_recent_tail() -> None:
    assert history_summary_fallback_span(message_count=8) == (2, 4)
    assert history_summary_fallback_span(message_count=7) is None

    with pytest.raises(ValueError, match="retained_tail_items"):
        history_summary_fallback_span(message_count=8, retained_tail_items=-1)


def test_workspace_context_reminder_policy_is_one_shot_after_compaction() -> None:
    assert (
        should_inject_workspace_context_reminder(
            compacted_context=True,
            already_injected=False,
        )
        is True
    )
    assert (
        should_inject_workspace_context_reminder(
            compacted_context=False,
            already_injected=False,
        )
        is False
    )
    assert (
        should_inject_workspace_context_reminder(
            compacted_context=True,
            already_injected=True,
        )
        is False
    )


@pytest.mark.parametrize(
    ("mode", "effect", "expected"),
    [
        (PermissionMode.ASK, ToolEffect.READ, PermissionDecision.ALLOW),
        (PermissionMode.ASK, ToolEffect.MODIFY, PermissionDecision.ASK),
        (PermissionMode.ASK, ToolEffect.MODIFY_EXECUTE, PermissionDecision.ASK),
        (PermissionMode.ASK, ToolEffect.EXECUTE, PermissionDecision.ASK),
        (PermissionMode.ACCEPT_EDITS, ToolEffect.MODIFY, PermissionDecision.ALLOW),
        (PermissionMode.ACCEPT_EDITS, ToolEffect.MODIFY_EXECUTE, PermissionDecision.ASK),
        (PermissionMode.ACCEPT_EDITS, ToolEffect.EXECUTE, PermissionDecision.ASK),
        (PermissionMode.READ_ONLY, ToolEffect.MODIFY, PermissionDecision.DENY),
        (PermissionMode.READ_ONLY, ToolEffect.MODIFY_EXECUTE, PermissionDecision.DENY),
        (PermissionMode.READ_ONLY, ToolEffect.EXECUTE, PermissionDecision.DENY),
    ],
)
def test_permission_modes_have_deterministic_effect_rules(
    mode: PermissionMode,
    effect: ToolEffect,
    expected: PermissionDecision,
) -> None:
    assert decide_permission(mode, effect, scope="tool:workspace") == expected


def test_process_local_grants_match_exact_scope_but_cannot_override_read_only() -> None:
    grant = ProcessLocalGrant(effect=ToolEffect.EXECUTE, scope="command:pytest")

    assert (
        decide_permission(
            PermissionMode.ASK,
            ToolEffect.EXECUTE,
            scope="command:pytest",
            grants={grant},
        )
        == PermissionDecision.ALLOW
    )
    assert (
        decide_permission(
            PermissionMode.ASK,
            ToolEffect.EXECUTE,
            scope="command:ruff",
            grants={grant},
        )
        == PermissionDecision.ASK
    )
    assert (
        decide_permission(
            PermissionMode.READ_ONLY,
            ToolEffect.EXECUTE,
            scope="command:pytest",
            grants={grant},
        )
        == PermissionDecision.DENY
    )

    with pytest.raises(ValidationError, match="must not be stored"):
        ProcessLocalGrant(effect=ToolEffect.READ, scope="workspace")


def test_proposed_change_diff_metadata_is_exact_and_bounded() -> None:
    diff = "--- a/app.py\n+++ b/app.py\n"
    change = ProposedChange(
        change_id="change-1",
        action_id="action-1",
        kind=ProposedChangeKind.UPDATE,
        paths=("app.py",),
        summary="Update app",
        unified_diff=diff,
        original_diff_bytes=len(diff.encode()),
    )
    assert not change.truncated

    truncated = change.model_copy(
        update={"original_diff_bytes": len(diff.encode()) + 20, "truncated": True}
    )
    assert truncated.truncated

    with pytest.raises(ValidationError, match="exactly describe"):
        ProposedChange(
            change_id="change-2",
            action_id="action-2",
            kind="update",
            paths=("app.py",),
            unified_diff=diff,
            original_diff_bytes=len(diff.encode()) + 1,
            truncated=False,
        )
    with pytest.raises(ValidationError, match="exactly a source and destination"):
        ProposedChange(
            change_id="change-3",
            action_id="action-3",
            kind="move",
            paths=("app.py",),
        )


def test_task_records_keep_queue_and_concurrent_execution_semantically_distinct() -> None:
    now = datetime.now(UTC)
    queued = TASK_STATE_ADAPTER.validate_python(
        {
            "lane": "queued",
            "task_id": "task-queued",
            "turn_id": "turn-2",
            "summary": "Follow up after the active turn",
            "queue_position": 1,
        }
    )
    foreground = ForegroundTaskState(
        task_id="task-active",
        turn_id="turn-1",
        summary="Active turn",
        status=TaskStatus.RUNNING,
        started_at=now,
    )
    background = BackgroundTaskState(
        task_id="task-background",
        turn_id="turn-bg",
        summary="Detached verification",
        status=TaskStatus.COMPLETED,
        started_at=now,
        completed_at=now + timedelta(seconds=2),
        result_summary="Passed",
    )

    assert isinstance(queued, QueuedTaskState)
    assert foreground.lane == "foreground"
    assert background.lane == "background"


def test_active_task_terminal_metadata_matches_status() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError, match="terminal metadata"):
        ForegroundTaskState(
            task_id="task",
            turn_id="turn",
            summary="Still running",
            status="running",
            started_at=now,
            completed_at=now,
        )
    with pytest.raises(ValidationError, match="require an error"):
        BackgroundTaskState(
            task_id="task",
            turn_id="turn",
            summary="Failed task",
            status="failed",
            started_at=now,
            completed_at=now + timedelta(seconds=1),
        )
    with pytest.raises(ValidationError, match="cannot precede"):
        BackgroundTaskState(
            task_id="task",
            turn_id="turn",
            summary="Bad timestamps",
            status="cancelled",
            started_at=now,
            completed_at=now - timedelta(seconds=1),
        )


def test_runtime_semantic_contracts_reject_unknown_provider_fields() -> None:
    with pytest.raises(ValidationError):
        ContextTelemetry(
            accuracy="exact",
            input_tokens=8,
            output_tokens=2,
            total_tokens=10,
            context_window=100,
            provider_usage={"vendor": "opaque"},
        )
