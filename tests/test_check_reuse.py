from pathlib import Path

import pytest
from test_loop_e2e import FIX_PATCH, make_task, read_events

from looplane.approvals import ApprovalDecision, ApprovalReason, CallbackApprovalPolicy
from looplane.contracts import (
    Limits,
    ModelTurn,
    RunStatus,
    ToolCall,
    ToolObservation,
    VerificationCommand,
)
from looplane.loop import AgentRunner
from looplane.models import ScriptedModel


@pytest.mark.asyncio
@pytest.mark.parametrize("change_after_check", [False, True])
async def test_manual_check_reused_only_for_unchanged_files(
    tiny_bug_repo: Path, tmp_path: Path, change_after_check: bool
) -> None:
    task = make_task(
        tiny_bug_repo,
        limits=Limits(max_steps=5, wall_time_seconds=30),
        verification=(VerificationCommand(name="diff", argv=("git", "diff", "--check")),),
    )
    turns = [
        ModelTurn(tool_calls=(ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),)),
        ModelTurn(tool_calls=(ToolCall(name="run_check", arguments={"name": "diff"}),)),
    ]
    if change_after_check:
        turns.append(
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        name="apply_patch",
                        arguments={
                            "patch": FIX_PATCH.replace(
                                "-    return left - right\n+    return left + right",
                                "-    return left + right\n+    return right + left",
                            )
                        },
                    ),
                )
            )
        )
    else:
        turns.append(ModelTurn(tool_calls=(ToolCall(name="git_diff", arguments={}),)))
    turns.append(ModelTurn(content="Done."))
    approvals = []

    def approve(request):
        approvals.append(request)
        return ApprovalDecision.ALLOW_ONCE

    result = await AgentRunner(
        task,
        ScriptedModel(turns),
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
        approval_policy=CallbackApprovalPolicy(approve),
    ).run()
    assert result.status == RunStatus.COMPLETED, result.model_dump()
    assert result.terminal_reason == "verified"
    assert len(result.verification) == 1 and result.verification[0].ok
    events = read_events(result)
    assert sum(e["event_type"] == "verification.reused" for e in events) == (
        0 if change_after_check else 1
    )
    assert sum(a.reason == ApprovalReason.FINAL_VERIFICATION for a in approvals) == (
        1 if change_after_check else 0
    )
    assert "git diff --check" in Path(result.artifacts["test_log"]).read_text()


@pytest.mark.asyncio
async def test_long_document_creation_completes_without_truncation_or_duplicate_check(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    content = (
        "# Introduction\n"
        + "\n".join(f"> Section {i}: full document content" for i in range(1, 338))
        + "\n"
    )
    task = make_task(
        tiny_bug_repo,
        limits=Limits(max_steps=4, wall_time_seconds=30),
        allowed_paths=("docs/**",),
        verification=(VerificationCommand(name="diff", argv=("git", "diff", "--check")),),
    )
    runner = AgentRunner(
        task,
        ScriptedModel(
            [
                ModelTurn(
                    tool_calls=(
                        ToolCall(
                            name="create_file",
                            arguments={
                                "path": "docs/introduction.md",
                                "content": content,
                            },
                        ),
                    )
                ),
                ModelTurn(tool_calls=(ToolCall(name="run_check", arguments={"name": "diff"}),)),
                ModelTurn(content="Created the complete introduction."),
            ]
        ),
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
    )
    result = await runner.run()
    assert result.status == RunStatus.COMPLETED, result.model_dump()
    assert result.terminal_reason == "verified"
    assert (runner.run_dir / "workspace/docs/introduction.md").read_text() == content
    assert "+> Section 337:" in Path(result.artifacts["patch"]).read_text()
    events = read_events(result)
    assert sum(e["event_type"] == "verification.reused" for e in events) == 1
    assert not any(e["event_type"] == "verification.started" for e in events)


@pytest.mark.asyncio
async def test_malformed_patch_rejected_before_approval_then_recovers(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    bad_call = ToolCall(
        name="apply_patch",
        arguments={
            "patch": (
                "diff --git a/docs/new.md b/docs/new.md\n"
                "--- /dev/null\n+++ b/docs/new.md\n@@ -0,0 +1,1 @@\n+first\n+second\n"
            )
        },
    )
    model = ScriptedModel(
        [
            ModelTurn(tool_calls=(bad_call,)),
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        name="create_file",
                        arguments={
                            "path": "docs/new.md",
                            "content": "first\nsecond\n",
                        },
                    ),
                )
            ),
            ModelTurn(content="Created both lines."),
        ]
    )
    approvals = []

    def approve(request):
        approvals.append(request)
        return ApprovalDecision.ALLOW_ONCE

    runner = AgentRunner(
        make_task(
            tiny_bug_repo,
            limits=Limits(max_steps=4, wall_time_seconds=30),
            allowed_paths=("docs/**",),
            verification=(VerificationCommand(name="diff", argv=("git", "diff", "--check")),),
        ),
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
        approval_policy=CallbackApprovalPolicy(approve),
    )
    result = await runner.run()
    assert result.status == RunStatus.COMPLETED, result.model_dump()
    assert not any(a.action_id == bad_call.tool_call_id for a in approvals)
    observations = [m for m in model.calls[1][0] if isinstance(m, ToolObservation)]
    assert any(not o.ok and "trailing" in (o.error or "") for o in observations)
    assert (runner.run_dir / "workspace/docs/new.md").read_text() == "first\nsecond\n"
