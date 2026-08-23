from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import run_git

from rivumi.approvals import (
    ApprovalDecision,
    ApprovalRequest,
    CallbackApprovalPolicy,
    HeadlessApprovalPolicy,
    ToolEffect,
)
from rivumi.contracts import (
    Limits,
    ModelCapabilities,
    ModelProtocol,
    ModelTurn,
    RunStatus,
    TaskContract,
    ToolCall,
    VerificationCommand,
)
from rivumi.loop import AgentRunner
from rivumi.models import ScriptedModel
from rivumi.session import SessionStore

FIX_PATCH = """\
diff --git a/src/tiny_python_bug/calculator.py b/src/tiny_python_bug/calculator.py
--- a/src/tiny_python_bug/calculator.py
+++ b/src/tiny_python_bug/calculator.py
@@ -1,3 +1,3 @@
 def add(left: int, right: int) -> int:
     \"\"\"Return the sum of two integers.\"\"\"
-    return left - right
+    return left + right
"""

NOTE_PATCH = """\
diff --git a/src/tiny_python_bug/agent_note.txt b/src/tiny_python_bug/agent_note.txt
new file mode 100644
--- /dev/null
+++ b/src/tiny_python_bug/agent_note.txt
@@ -0,0 +1 @@
+verified by the coding agent
"""


def task_for(repository: Path) -> TaskContract:
    return TaskContract(
        task_id="interactive-test",
        repository=repository,
        instruction="Fix the calculator.",
        allowed_paths=("src/**",),
        verification=(VerificationCommand(name="tests", argv=("pytest", "-q")),),
        limits=Limits(max_steps=6, wall_time_seconds=30),
        base_sha=run_git(repository, "rev-parse", "HEAD"),
    )


@pytest.mark.asyncio
async def test_interactive_policy_approves_patch_and_final_verification(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    requests: list[ApprovalRequest] = []

    def approve(request: ApprovalRequest) -> ApprovalDecision:
        requests.append(request)
        return ApprovalDecision.ALLOW_ONCE

    model = ScriptedModel(
        [
            ModelTurn(
                tool_calls=(ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),)
            ),
            ModelTurn(content="Fixed."),
        ]
    )
    result = await AgentRunner(
        task_for(tiny_bug_repo),
        model,
        tmp_path / "runs",
        approval_policy=CallbackApprovalPolicy(approve),
    ).run()

    assert result.status == RunStatus.COMPLETED
    assert [request.effect for request in requests] == [
        ToolEffect.MODIFY,
        ToolEffect.EXECUTE,
    ]
    event_types = [
        json.loads(line)["event_type"]
        for line in Path(result.artifacts["events"]).read_text().splitlines()
    ]
    assert "approval.requested" in event_types
    assert "approval.resolved" in event_types
    assert (Path(result.artifacts["result"]).parent / "session.json").is_file()


class InterruptingModel:
    provider_name = "scripted"
    model_id = "scripted"
    protocol = ModelProtocol.SCRIPTED
    capabilities = ModelCapabilities(
        tool_calling=True,
        streaming=False,
        structured_output=False,
    )

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages: object, tools: object = ()) -> ModelTurn:
        self.calls += 1
        if self.calls == 1:
            return ModelTurn(
                tool_calls=(ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),)
            )
        raise asyncio.CancelledError

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_nonterminal_session_resumes_without_reapplying_patch(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    run_root = tmp_path / "runs"
    run_id = "resume-test"
    first = AgentRunner(
        task_for(tiny_bug_repo),
        InterruptingModel(),
        run_root,
        run_id=run_id,
        approval_policy=HeadlessApprovalPolicy(allow_modify=True, allow_execute=True),
    )

    with pytest.raises(asyncio.CancelledError):
        await first.run()

    resumed = await AgentRunner.resume(
        run_root / run_id,
        ScriptedModel([ModelTurn(content="The existing patch is ready to verify.")]),
        approval_policy=HeadlessApprovalPolicy(allow_modify=True, allow_execute=True),
    )
    result = await resumed.run()

    assert result.status == RunStatus.COMPLETED
    patch = Path(result.artifacts["patch"]).read_text()
    assert patch.count("+    return left + right") == 1
    events = [
        json.loads(line)
        for line in Path(result.artifacts["events"]).read_text().splitlines()
    ]
    assert [event["sequence"] for event in events] == list(range(len(events)))
    assert any(event["event_type"] == "session.resumed" for event in events)
    manifest = json.loads((run_root / run_id / "session.json").read_text())
    assert manifest["terminal"] is True
    assert manifest["phase"] == "completed"


@pytest.mark.asyncio
async def test_resume_abandons_an_unresolved_approval_without_executing_it(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    run_root = tmp_path / "runs"
    run_id = "pending-approval"

    async def interrupt(_: ApprovalRequest) -> ApprovalDecision:
        raise asyncio.CancelledError

    first = AgentRunner(
        task_for(tiny_bug_repo),
        ScriptedModel(
            [
                ModelTurn(
                    tool_calls=(
                        ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),
                    )
                )
            ]
        ),
        run_root,
        run_id=run_id,
        approval_policy=CallbackApprovalPolicy(interrupt),
    )
    with pytest.raises(asyncio.CancelledError):
        await first.run()

    interrupted = json.loads((run_root / run_id / "session.json").read_text())
    assert interrupted["step"] == 1
    assert interrupted["phase"] == "waiting_approval"
    assert interrupted["pending_action"]["tool_call"]["name"] == "apply_patch"
    interrupted_source = (
        run_root / run_id / "workspace/src/tiny_python_bug/calculator.py"
    ).read_text()
    assert not interrupted_source.endswith("return left + right\n")

    resumed = await AgentRunner.resume(
        run_root / run_id,
        ScriptedModel(
            [
                ModelTurn(
                    tool_calls=(
                        ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),
                    )
                ),
                ModelTurn(content="Fixed after requesting approval again."),
            ]
        ),
        approval_policy=HeadlessApprovalPolicy(allow_modify=True, allow_execute=True),
    )
    result = await resumed.run()

    assert result.status == RunStatus.COMPLETED
    events = [
        json.loads(line)
        for line in Path(result.artifacts["events"]).read_text().splitlines()
    ]
    assert any(event["event_type"] == "approval.abandoned" for event in events)
    assert Path(result.artifacts["patch"]).read_text().count("+    return left + right") == 1


@pytest.mark.asyncio
async def test_resume_reconciles_approval_decided_before_started_event(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    run_root = tmp_path / "runs"
    run_id = "approved-not-started"
    first = AgentRunner(
        task_for(tiny_bug_repo),
        ScriptedModel(
            [
                ModelTurn(
                    tool_calls=(
                        ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),
                    )
                )
            ]
        ),
        run_root,
        run_id=run_id,
        approval_policy=HeadlessApprovalPolicy(allow_modify=True, allow_execute=True),
    )
    emit = first._event

    async def interrupt_before_started(event_type: str, **data: object) -> None:
        if event_type == "tool.started":
            raise asyncio.CancelledError
        await emit(event_type, **data)

    first._event = interrupt_before_started  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await first.run()

    interrupted = json.loads((run_root / run_id / "session.json").read_text())
    assert interrupted["pending_action"]["tool_call"]["name"] == "apply_patch"
    assert interrupted["approval_history"][-1]["decision"] == "allow_once"

    resumed = await AgentRunner.resume(
        run_root / run_id,
        ScriptedModel(
            [
                ModelTurn(
                    tool_calls=(
                        ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),
                    )
                ),
                ModelTurn(content="Fixed after approval recovery."),
            ]
        ),
        approval_policy=HeadlessApprovalPolicy(allow_modify=True, allow_execute=True),
    )
    result = await resumed.run()

    assert result.status == RunStatus.COMPLETED
    assert Path(result.artifacts["patch"]).read_text().count("+    return left + right") == 1


@pytest.mark.asyncio
async def test_allow_session_grant_is_persisted_and_reused(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    requests: list[ApprovalRequest] = []

    def allow_session(request: ApprovalRequest) -> ApprovalDecision:
        requests.append(request)
        return ApprovalDecision.ALLOW_SESSION

    model = ScriptedModel(
        [
            ModelTurn(
                tool_calls=(
                    ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),
                    ToolCall(name="apply_patch", arguments={"patch": NOTE_PATCH}),
                )
            ),
            ModelTurn(content="Fixed."),
        ]
    )
    result = await AgentRunner(
        task_for(tiny_bug_repo),
        model,
        tmp_path / "runs",
        approval_policy=CallbackApprovalPolicy(allow_session),
    ).run()

    assert result.status == RunStatus.COMPLETED
    assert [request.effect for request in requests] == [ToolEffect.MODIFY, ToolEffect.EXECUTE]
    event_types = [
        json.loads(line)["event_type"]
        for line in Path(result.artifacts["events"]).read_text().splitlines()
    ]
    assert "approval.reused" in event_types
    manifest = json.loads((Path(result.artifacts["result"]).parent / "session.json").read_text())
    assert sorted(manifest["granted_effects"]) == ["execute", "modify"]


@pytest.mark.asyncio
async def test_resume_reconciles_reused_grant_before_started_event(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    run_root = tmp_path / "runs"
    run_id = "reused-grant-not-started"

    def allow_session(_: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision.ALLOW_SESSION

    first = AgentRunner(
        task_for(tiny_bug_repo),
        ScriptedModel(
            [
                ModelTurn(
                    tool_calls=(
                        ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),
                        ToolCall(name="apply_patch", arguments={"patch": NOTE_PATCH}),
                    )
                )
            ]
        ),
        run_root,
        run_id=run_id,
        approval_policy=CallbackApprovalPolicy(allow_session),
    )
    emit = first._event
    started_count = 0

    async def interrupt_second_started(event_type: str, **data: object) -> None:
        nonlocal started_count
        if event_type == "tool.started":
            started_count += 1
            if started_count == 2:
                raise asyncio.CancelledError
        await emit(event_type, **data)

    first._event = interrupt_second_started  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await first.run()

    interrupted = json.loads((run_root / run_id / "session.json").read_text())
    assert interrupted["pending_action"]["tool_call"]["arguments"]["patch"] == NOTE_PATCH

    resumed = await AgentRunner.resume(
        run_root / run_id,
        ScriptedModel(
            [
                ModelTurn(
                    tool_calls=(
                        ToolCall(name="apply_patch", arguments={"patch": NOTE_PATCH}),
                    )
                ),
                ModelTurn(content="Finished after reusing the grant safely."),
            ]
        ),
        approval_policy=HeadlessApprovalPolicy(allow_modify=True, allow_execute=True),
    )
    result = await resumed.run()

    assert result.status == RunStatus.COMPLETED
    patch = Path(result.artifacts["patch"]).read_text()
    assert patch.count("+    return left + right") == 1
    assert patch.count("+verified by the coding agent") == 1


@pytest.mark.asyncio
async def test_resume_does_not_replenish_consumed_wall_time(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    run_root = tmp_path / "runs"
    run_id = "spent-budget"
    first = AgentRunner(
        task_for(tiny_bug_repo),
        InterruptingModel(),
        run_root,
        run_id=run_id,
        approval_policy=HeadlessApprovalPolicy(allow_modify=True, allow_execute=True),
    )
    with pytest.raises(asyncio.CancelledError):
        await first.run()

    store = SessionStore(run_root / run_id, durable=False)
    with store.acquire_writer() as lease:
        manifest = await store.claim(lease)
        manifest = manifest.model_copy(
            update={
                "active_wall_time_seconds": 0.0,
                "active_started_at": datetime.now(UTC) - timedelta(seconds=31),
            }
        )
        await store.save(manifest, lease)

    resumed = await AgentRunner.resume(
        run_root / run_id,
        ScriptedModel([]),
        approval_policy=HeadlessApprovalPolicy(allow_modify=True, allow_execute=True),
    )
    result = await resumed.run()

    assert result.status == RunStatus.FAILED
    assert result.terminal_reason == "wall_time_exceeded"
