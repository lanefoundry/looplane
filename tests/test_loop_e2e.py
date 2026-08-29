from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

import pytest
from conftest import run_git

from rivumi.approvals import (
    ApprovalDecision,
    ApprovalReason,
    ApprovalRequest,
    HeadlessApprovalPolicy,
    ToolEffect,
)
from rivumi.contracts import (
    Limits,
    Message,
    ModelCapabilities,
    ModelProtocol,
    ModelTurn,
    RunResult,
    RunStatus,
    TaskContract,
    ToolCall,
    Usage,
    VerificationCommand,
)
from rivumi.loop import AgentRunner
from rivumi.models import ProviderError, ProviderErrorKind, ScriptedModel
from rivumi.permissions import PermissionGuard
from rivumi.prompts import WORKSPACE_CONTEXT_REMINDER_VERSION, build_workspace_context_reminder
from rivumi.session import SessionManifest, SessionPhase, SessionStore

BROKEN_PATCH = """\
diff --git a/src/tiny_python_bug/calculator.py b/src/tiny_python_bug/calculator.py
--- a/src/tiny_python_bug/calculator.py
+++ b/src/tiny_python_bug/calculator.py
@@ -1,3 +1,3 @@
 def add(left: int, right: int) -> int:
-    \"\"\"Return the sum of two integers.\"\"\"
+    \"\"\"Add two integers together.\"\"\"
     return left - right
"""


FIX_PATCH_AFTER_BROKEN = """\
diff --git a/src/tiny_python_bug/calculator.py b/src/tiny_python_bug/calculator.py
--- a/src/tiny_python_bug/calculator.py
+++ b/src/tiny_python_bug/calculator.py
@@ -1,3 +1,3 @@
 def add(left: int, right: int) -> int:
     \"\"\"Add two integers together.\"\"\"
-    return left - right
+    return left + right
"""


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


def make_task(
    repository: Path,
    *,
    limits: Limits,
    verification: tuple[VerificationCommand, ...] | None = None,
    allowed_paths: tuple[str, ...] = ("src/**",),
) -> TaskContract:
    return TaskContract(
        task_id="tiny-python-bug",
        repository=repository,
        base_sha=run_git(repository, "rev-parse", "HEAD"),
        instruction="Fix the bounded fixture bug.",
        allowed_paths=allowed_paths,
        verification=verification
        or (VerificationCommand(name="tests", argv=("pytest", "-q"), timeout_seconds=30),),
        limits=limits,
    )


def read_events(result: RunResult) -> list[dict[str, object]]:
    artifacts = result.artifacts
    return [
        json.loads(line)
        for line in Path(artifacts["events"]).read_text().splitlines()
        if line.strip()
    ]


@pytest.mark.asyncio
async def test_execute_approval_audit_includes_suspicious_policy_reason(
    tmp_path: Path,
) -> None:
    class RecordingApprovalPolicy:
        def __init__(self) -> None:
            self.calls: list[ApprovalRequest] = []

        async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
            self.calls.append(request)
            return ApprovalDecision.ALLOW_ONCE

    approval_policy = RecordingApprovalPolicy()
    command = VerificationCommand(
        name="diff",
        argv=("git", "status", "&&", "git", "diff", "--check"),
        timeout_seconds=30,
    )
    task = TaskContract(
        repository=tmp_path,
        instruction="Check the patch.",
        allowed_paths=("**",),
        verification=(command,),
        limits=Limits(),
    )
    runner = AgentRunner(
        task,
        ScriptedModel([ModelTurn(content="unused")]),
        tmp_path / "runs",
        approval_policy=approval_policy,
        permission_guard=PermissionGuard(),
        run_id="approval-policy-reason",
    )

    decision, _request_id = await runner._approval(
        action_id="verification:diff",
        effect=ToolEffect.EXECUTE,
        reason=ApprovalReason.FINAL_VERIFICATION,
        preview="$ git status && git diff --check",
        command=command,
    )

    events = [
        json.loads(line)
        for line in (runner.run_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    requested = next(event for event in events if event["event_type"] == "approval.requested")
    assert decision is ApprovalDecision.ALLOW_ONCE
    assert approval_policy.calls[0].policy_reason.startswith("suspicious command shape:")
    assert "compound shell command" in requested["data"]["policy_reason"]


def write_loop_mcp_server(path: Path) -> None:
    path.write_text(
        """
from __future__ import annotations

import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if "id" not in request:
        continue
    if method == "initialize":
        result = {
            "protocolVersion": request["params"]["protocolVersion"],
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
            "serverInfo": {"name": "fake", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": []}
    elif method == "resources/list":
        result = {"resources": [{"uri": "file:///notes.md", "name": "notes"}]}
    elif method == "prompts/list":
        result = {"prompts": [{"name": "review", "description": "Review a topic."}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
""".lstrip(),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_initial_prompt_injects_explicit_memory(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_path = tmp_path / "memory.jsonl"
    monkeypatch.setenv("RIVUMI_MEMORY_PATH", str(memory_path))
    from rivumi.memory import remember

    remember("user: prefer concise final answers", project=tiny_bug_repo)
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=1))
    model = ScriptedModel([ModelTurn(content="No change needed.")])

    await AgentRunner(
        task=task,
        model=model,
        run_root=tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    first_messages, _tools = model.calls[0]
    system = first_messages[0]
    assert isinstance(system, Message)
    assert system.role == "system"
    assert "Known context from explicit /remember entries" in (system.content or "")
    assert "prefer concise final answers" in (system.content or "")


@pytest.mark.asyncio
async def test_initial_prompt_injects_project_instructions(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RIVUMI_USER_INSTRUCTIONS", str(tmp_path / "missing.md"))
    (tiny_bug_repo / "AGENTS.md").write_text("Project instruction: use pytest.", encoding="utf-8")
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=1))
    model = ScriptedModel([ModelTurn(content="No change needed.")])

    await AgentRunner(
        task=task,
        model=model,
        run_root=tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    first_messages, _tools = model.calls[0]
    system = first_messages[0]
    assert isinstance(system, Message)
    assert system.role == "system"
    assert "Additional instructions from configured files" in (system.content or "")
    assert "Project instruction: use pytest." in (system.content or "")


@pytest.mark.asyncio
async def test_scripted_model_fixes_bug_verifies_and_writes_auditable_bundle(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    source_sha = run_git(tiny_bug_repo, "rev-parse", "HEAD")
    source_status = run_git(tiny_bug_repo, "status", "--porcelain=v1")
    source_file = tiny_bug_repo / "src" / "tiny_python_bug" / "calculator.py"
    source_contents = source_file.read_bytes()
    task = TaskContract(
        task_id="tiny-python-bug",
        repository=tiny_bug_repo,
        base_sha=source_sha,
        instruction="Fix add so the existing test passes. Do not change tests.",
        allowed_paths=("src/**",),
        verification=(
            VerificationCommand(name="tests", argv=("pytest", "-q"), timeout_seconds=30),
        ),
        limits=Limits(max_steps=8, wall_time_seconds=60),
    )
    model = ScriptedModel(
        turns=[
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        name="read_file",
                        arguments={"path": "src/tiny_python_bug/calculator.py"},
                    ),
                )
            ),
            ModelTurn(
                tool_calls=(ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),)
            ),
            ModelTurn(
                tool_calls=(ToolCall(name="run_check", arguments={"name": "tests"}),)
            ),
            ModelTurn(content="Fixed the calculator and verified the test suite."),
        ]
    )

    result = await AgentRunner(
        task=task,
        model=model,
        run_root=tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    assert result.status == RunStatus.COMPLETED, result.model_dump()
    assert result.changed_files == ("src/tiny_python_bug/calculator.py",)
    assert result.verification
    assert all(outcome.ok for outcome in result.verification)
    assert run_git(tiny_bug_repo, "rev-parse", "HEAD") == source_sha
    assert run_git(tiny_bug_repo, "status", "--porcelain=v1") == source_status == ""
    assert source_file.read_bytes() == source_contents

    expected_artifacts = {"request", "events", "checkpoint", "patch", "test_log", "result"}
    artifact_paths = {key: Path(value) for key, value in result.artifacts.items()}
    assert set(artifact_paths) == expected_artifacts
    assert all(path.is_absolute() for path in artifact_paths.values())
    assert all(path.is_file() for path in artifact_paths.values())

    request = json.loads(artifact_paths["request"].read_text())
    checkpoint = json.loads(artifact_paths["checkpoint"].read_text())
    persisted_result = json.loads(artifact_paths["result"].read_text())
    events = [
        json.loads(line)
        for line in artifact_paths["events"].read_text().splitlines()
        if line.strip()
    ]
    patch = artifact_paths["patch"].read_text()
    test_log = artifact_paths["test_log"].read_text()

    assert request["task_id"] == task.task_id
    assert request["base_sha"] == source_sha
    assert events
    assert [event["sequence"] for event in events] == list(range(len(events)))
    assert any(event["event_type"] == "tool.completed" for event in events)
    assert checkpoint["status"] == RunStatus.COMPLETED
    assert persisted_result["status"] == RunStatus.COMPLETED
    assert "-    return left - right" in patch
    assert "+    return left + right" in patch
    assert "passed" in test_log.lower()


@pytest.mark.parametrize(
    "run_id",
    ["../escape", "nested/run", "/absolute", "C:\\escape", ".", "..", "bad\x00id"],
)
def test_run_id_must_be_one_safe_relative_segment(
    tiny_bug_repo: Path, tmp_path: Path, run_id: str
) -> None:
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=1))

    with pytest.raises(ValueError, match="safe relative path segment"):
        AgentRunner(task, ScriptedModel([ModelTurn(content="unused")]), tmp_path, run_id=run_id)


@pytest.mark.asyncio
async def test_max_steps_retains_failed_verification_and_failure_artifacts(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = make_task(
        tiny_bug_repo,
        limits=Limits(max_steps=2, wall_time_seconds=30),
        verification=(
            VerificationCommand(
                name="clean-diff",
                argv=("git", "diff", "--exit-code"),
                timeout_seconds=30,
            ),
        ),
    )
    runner = AgentRunner(
        task,
        ScriptedModel(
            [
                ModelTurn(
                    tool_calls=(ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),)
                ),
                ModelTurn(content="Done, but the declared check will not accept this."),
            ]
        ),
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
    )

    result = await runner.run()

    assert result.status == RunStatus.FAILED
    assert result.terminal_reason == "max_steps_exceeded"
    assert len(result.verification) == 1
    assert result.verification[0].ok is False
    assert set(result.artifacts) == {
        "request",
        "events",
        "checkpoint",
        "patch",
        "test_log",
        "result",
    }
    assert all(Path(path).is_file() for path in result.artifacts.values())
    events = read_events(result)
    assert [event["sequence"] for event in events] == list(range(len(events)))
    assert events[-1]["event_type"] == "run.failed"
    assert events[-1]["data"]["terminal_reason"] == "max_steps_exceeded"


@pytest.mark.asyncio
async def test_conversational_run_skips_verification_and_completes_without_changes(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=4, wall_time_seconds=30))
    model = ScriptedModel(
        [
            ModelTurn(tool_calls=(ToolCall(name="list_files", arguments={"path": "."}),)),
            ModelTurn(content="Hi! Ask me to fix or inspect something in this repository."),
        ]
    )

    result = await AgentRunner(
        task,
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    assert result.status == RunStatus.COMPLETED, result.model_dump()
    assert result.terminal_reason == "no_changes"
    assert result.changed_files == ()
    assert result.verification == ()
    events = read_events(result)
    assert not any(event["event_type"] == "verification.started" for event in events)
    assert not any(event["event_type"].startswith("verification.") for event in events)
    assert events[-1]["event_type"] == "run.completed"
    assert events[-1]["data"]["terminal_reason"] == "no_changes"


@pytest.mark.asyncio
async def test_read_only_tool_calls_execute_as_parallel_batch(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=4, wall_time_seconds=30))
    model = ScriptedModel(
        [
            ModelTurn(
                tool_calls=(
                    ToolCall(name="list_files", arguments={"path": "."}),
                    ToolCall(
                        name="read_file",
                        arguments={"path": "src/tiny_python_bug/calculator.py"},
                    ),
                )
            ),
            ModelTurn(content="Inspected the repository."),
        ]
    )

    result = await AgentRunner(
        task,
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    assert result.status == RunStatus.COMPLETED, result.model_dump()
    events = read_events(result)
    assert any(event["event_type"] == "tool.batch_started" for event in events)
    assert any(event["event_type"] == "tool.batch_completed" for event in events)
    completed = [event for event in events if event["event_type"] == "tool.completed"]
    assert {event["data"]["name"] for event in completed} == {"list_files", "read_file"}
    observations = [item for item in model.calls[1][0] if not isinstance(item, Message)]
    assert [item.name for item in observations] == ["list_files", "read_file"]


@pytest.mark.asyncio
async def test_mcp_resource_and_prompt_reads_execute_as_parallel_batch(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = tmp_path / "fake_mcp_server.py"
    write_loop_mcp_server(server)
    (tiny_bug_repo / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "local": {"command": sys.executable, "args": [str(server)]},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RIVUMI_MCP_ALLOWLIST", "local")
    task = make_task(
        tiny_bug_repo,
        limits=Limits(max_steps=4, wall_time_seconds=30),
        allowed_paths=("src/**", ".mcp.json"),
    )
    model = ScriptedModel(
        [
            ModelTurn(
                tool_calls=(
                    ToolCall(name="mcp_resource__local__list"),
                    ToolCall(name="mcp_prompt__local__list"),
                )
            ),
            ModelTurn(content="Inspected MCP resources and prompts."),
        ]
    )

    result = await AgentRunner(
        task,
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    assert result.status == RunStatus.COMPLETED, result.model_dump()
    events = read_events(result)
    assert any(event["event_type"] == "tool.batch_started" for event in events)
    assert any(event["event_type"] == "tool.batch_completed" for event in events)
    observations = [item for item in model.calls[1][0] if not isinstance(item, Message)]
    assert [item.name for item in observations] == [
        "mcp_resource__local__list",
        "mcp_prompt__local__list",
    ]


@pytest.mark.asyncio
async def test_read_only_batch_preserves_repetition_guard_progress(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    repeated = ToolCall(name="read_file", arguments={"path": "src/tiny_python_bug/calculator.py"})
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=4, wall_time_seconds=30))
    model = ScriptedModel([ModelTurn(tool_calls=(repeated, repeated, repeated))])

    result = await AgentRunner(
        task,
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    assert result.status == RunStatus.FAILED
    assert result.terminal_reason == "repeated_action"
    events = read_events(result)
    assert sum(event["event_type"] == "tool.completed" for event in events) == 2
    assert events[-1]["event_type"] == "run.failed"


@pytest.mark.asyncio
async def test_three_identical_actions_trigger_repetition_guard(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    call = ToolCall(name="read_file", arguments={"path": "src/tiny_python_bug/calculator.py"})
    model = ScriptedModel(
        [
            ModelTurn(tool_calls=(call,)),
            ModelTurn(tool_calls=(call,)),
            ModelTurn(tool_calls=(call,)),
        ]
    )
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=5, wall_time_seconds=30))

    result = await AgentRunner(
        task,
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    assert result.status == RunStatus.FAILED
    assert result.terminal_reason == "repeated_action"
    events = read_events(result)
    assert sum(event["event_type"] == "tool.completed" for event in events) == 2
    assert events[-1]["event_type"] == "run.failed"


class SlowModel:
    provider_name = "slow-test"
    model_id = "slow-test"
    protocol = ModelProtocol.SCRIPTED
    capabilities = ModelCapabilities(
        tool_calling=True,
        streaming=False,
        structured_output=False,
    )

    async def complete(self, messages: object, tools: object = ()) -> ModelTurn:
        import asyncio

        await asyncio.sleep(2)
        return ModelTurn(content="too late")

    async def aclose(self) -> None:
        return None


class ToolDisabledModel(SlowModel):
    capabilities = ModelCapabilities(
        tool_calling=False,
        streaming=False,
        structured_output=False,
    )

    async def complete(self, messages: object, tools: object = ()) -> ModelTurn:
        raise AssertionError("a tool-disabled provider must never be called")


@pytest.mark.asyncio
async def test_agent_runner_fails_closed_for_tool_disabled_provider(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=1))
    run_root = tmp_path / "runs"

    with pytest.raises(ValueError, match="does not advertise tool calling"):
        await AgentRunner(
            task,
            ToolDisabledModel(),
            run_root,
            allow_unsafe_local_exec=True,
        ).run()

    assert not run_root.exists()


@pytest.mark.asyncio
async def test_model_call_is_clamped_by_run_wall_time(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=2, wall_time_seconds=0.5))
    started = time.monotonic()

    result = await AgentRunner(
        task,
        SlowModel(),
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    assert time.monotonic() - started < 1.5
    assert result.status == RunStatus.FAILED
    assert result.terminal_reason == "wall_time_exceeded"
    assert read_events(result)[-1]["event_type"] == "run.failed"


@pytest.mark.asyncio
async def test_verification_command_is_clamped_by_run_wall_time(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    command = VerificationCommand(
        name="slow",
        argv=(sys.executable, "-c", "import time; time.sleep(5)"),
        timeout_seconds=10,
    )
    task = make_task(
        tiny_bug_repo,
        limits=Limits(max_steps=3, wall_time_seconds=0.6),
        verification=(command,),
    )
    started = time.monotonic()

    result = await AgentRunner(
        task,
        ScriptedModel(
            [
                ModelTurn(
                    tool_calls=(ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),)
                ),
                ModelTurn(content="Verify now."),
            ]
        ),
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    assert time.monotonic() - started < 2
    assert result.status == RunStatus.FAILED
    assert result.terminal_reason == "wall_time_exceeded"
    assert result.verification[0].exit_code == 124


@pytest.mark.asyncio
async def test_failed_final_verification_is_fed_back_then_retried(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=4, wall_time_seconds=30))
    model = ScriptedModel(
        [
            ModelTurn(
                tool_calls=(ToolCall(name="apply_patch", arguments={"patch": BROKEN_PATCH}),)
            ),
            ModelTurn(content="I think it is already fixed."),
            ModelTurn(
                tool_calls=(
                    ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH_AFTER_BROKEN}),)
            ),
            ModelTurn(content="Fixed after reading the failed verification."),
        ]
    )

    result = await AgentRunner(
        task,
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    assert result.status == RunStatus.COMPLETED, result.model_dump()
    assert result.terminal_reason == "verified"
    feedback_messages = [
        item
        for item in model.calls[2][0]
        if isinstance(item, Message)
        and item.role == "user"
        and item.content is not None
        and "checks and they failed" in item.content
    ]
    assert len(feedback_messages) == 1
    events = read_events(result)
    verification_events = [
        event for event in events if event["event_type"] == "verification.completed"
    ]
    assert [event["data"]["ok"] for event in verification_events] == [False, True]
    test_log = Path(result.artifacts["test_log"]).read_text().lower()
    assert "failed" in test_log
    assert "passed" in test_log
    assert events[-1]["event_type"] == "run.completed"


@pytest.mark.asyncio
async def test_truncated_model_turn_continues_without_running_verification(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=3, wall_time_seconds=30))
    model = ScriptedModel(
        [
            ModelTurn(content="", finish_reason="length"),
            ModelTurn(
                tool_calls=(ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),)
            ),
            ModelTurn(content="Fixed after the truncated turn."),
        ]
    )

    result = await AgentRunner(
        task,
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    assert result.status == RunStatus.COMPLETED
    assert len(model.calls) == 3
    assert any(
        isinstance(item, Message)
        and item.role == "user"
        and item.content is not None
        and "output limit" in item.content
        for item in model.calls[1][0]
    )
    verification_events = [
        event for event in read_events(result) if event["event_type"] == "verification.completed"
    ]
    assert len(verification_events) == 1


@pytest.mark.asyncio
async def test_new_and_deleted_files_are_reported_by_agent_result(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    obsolete = tiny_bug_repo / "src" / "tiny_python_bug" / "obsolete.py"
    obsolete.write_text("OLD = True\n")
    run_git(tiny_bug_repo, "add", ".")
    run_git(tiny_bug_repo, "commit", "-q", "-m", "fixture: add obsolete module")
    patch = """\
diff --git a/src/tiny_python_bug/new_module.py b/src/tiny_python_bug/new_module.py
new file mode 100644
--- /dev/null
+++ b/src/tiny_python_bug/new_module.py
@@ -0,0 +1 @@
+NEW = True
diff --git a/src/tiny_python_bug/obsolete.py b/src/tiny_python_bug/obsolete.py
deleted file mode 100644
--- a/src/tiny_python_bug/obsolete.py
+++ /dev/null
@@ -1 +0,0 @@
-OLD = True
"""
    command = VerificationCommand(
        name="always-pass",
        argv=(sys.executable, "-c", "raise SystemExit(0)"),
        timeout_seconds=5,
    )
    task = make_task(
        tiny_bug_repo,
        limits=Limits(max_steps=2, wall_time_seconds=30),
        verification=(command,),
    )
    model = ScriptedModel(
        [
            ModelTurn(tool_calls=(ToolCall(name="apply_patch", arguments={"patch": patch}),)),
            ModelTurn(content="Added replacement and deleted obsolete module."),
        ]
    )

    result = await AgentRunner(
        task,
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    assert result.status == RunStatus.COMPLETED
    assert result.changed_files == (
        "src/tiny_python_bug/new_module.py",
        "src/tiny_python_bug/obsolete.py",
    )
    patch_artifact = Path(result.artifacts["patch"]).read_text()
    assert "new file mode 100644" in patch_artifact
    assert "deleted file mode 100644" in patch_artifact


def _retryable_error(status_code: int, **kwargs: object) -> ProviderError:
    return ProviderError(
        f"nvidia-nim request failed: error code {status_code}",
        kind=ProviderErrorKind.RETRYABLE,
        provider_name="nvidia-nim",
        status_code=status_code,
        **kwargs,
    )


def _clean_check_task(repository: Path) -> TaskContract:
    return make_task(
        repository,
        limits=Limits(max_steps=8, wall_time_seconds=60),
        verification=(
            VerificationCommand(
                name="check", argv=("git", "diff", "--check"), timeout_seconds=30
            ),
        ),
    )


@pytest.mark.asyncio
async def test_token_budget_cap_fails_the_run_before_more_model_calls(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = make_task(
        tiny_bug_repo,
        limits=Limits(
            max_steps=8,
            wall_time_seconds=60,
            max_total_tokens=100,
        ),
        verification=(
            VerificationCommand(
                name="check", argv=("git", "diff", "--check"), timeout_seconds=30
            ),
        ),
    )
    model = ScriptedModel(
        [
            ModelTurn(
                content="Working.",
                usage=Usage(input_tokens=150, output_tokens=10),
            ),
            ModelTurn(content="Should never be requested."),
        ]
    )
    runner = AgentRunner(task, model, tmp_path / "runs", allow_unsafe_local_exec=True)

    result = await runner.run()

    assert result.status == RunStatus.FAILED
    assert result.terminal_reason == "token_budget_exceeded"
    assert result.error is not None
    assert "160" in result.error
    assert "100" in result.error
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_context_pressure_reminder_is_injected_once_before_next_model_request(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = make_task(
        tiny_bug_repo,
        limits=Limits(
            max_steps=3,
            wall_time_seconds=60,
            max_total_tokens=100,
        ),
        verification=(
            VerificationCommand(
                name="check", argv=("git", "diff", "--check"), timeout_seconds=30
            ),
        ),
    )
    model = ScriptedModel(
        [
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        name="read_file",
                        arguments={"path": "src/tiny_python_bug/calculator.py"},
                    ),
                ),
                usage=Usage(input_tokens=80, output_tokens=5),
            ),
            ModelTurn(
                content="Partial answer.",
                finish_reason="length",
                usage=Usage(input_tokens=1, output_tokens=1),
            ),
            ModelTurn(content="No repository change is needed."),
        ]
    )
    runner = AgentRunner(task, model, tmp_path / "runs", allow_unsafe_local_exec=True)

    result = await runner.run()

    assert result.status == RunStatus.COMPLETED
    assert len(model.calls) == 3
    first_call_messages, _tools = model.calls[0]
    second_call_messages, _tools = model.calls[1]
    third_call_messages, _tools = model.calls[2]
    assert not [
        message
        for message in first_call_messages
        if isinstance(message, Message)
        and message.content
        and "b9-b1-context-pressure-v1" in message.content
    ]
    second_reminders = [
        message
        for message in second_call_messages
        if isinstance(message, Message)
        and message.content
        and "b9-b1-context-pressure-v1" in message.content
    ]
    third_reminders = [
        message
        for message in third_call_messages
        if isinstance(message, Message)
        and message.content
        and "b9-b1-context-pressure-v1" in message.content
    ]
    assert len(second_reminders) == 1
    assert len(third_reminders) == 1
    assert "85 of 100 allowed task tokens" in (second_reminders[0].content or "")
    events = read_events(result)
    assert len(
        [
            event
            for event in events
            if event["event_type"] == "context_pressure.reminder_injected"
        ]
    ) == 1


@pytest.mark.asyncio
async def test_history_summary_fallback_compacts_old_native_messages_once(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = make_task(
        tiny_bug_repo,
        limits=Limits(
            max_steps=4,
            wall_time_seconds=60,
            max_total_tokens=100,
        ),
        verification=(
            VerificationCommand(
                name="check", argv=("git", "diff", "--check"), timeout_seconds=30
            ),
        ),
    )
    read_calculator = ToolCall(
        name="read_file",
        arguments={"path": "src/tiny_python_bug/calculator.py"},
    )
    read_package = ToolCall(
        name="read_file",
        arguments={"path": "src/tiny_python_bug/__init__.py"},
    )
    model = ScriptedModel(
        [
            ModelTurn(tool_calls=(read_calculator,), usage=Usage(input_tokens=40)),
            ModelTurn(tool_calls=(read_package,), usage=Usage(input_tokens=45)),
            ModelTurn(tool_calls=(read_calculator,), usage=Usage(input_tokens=1)),
            ModelTurn(content="No repository change is needed."),
        ]
    )
    runner = AgentRunner(task, model, tmp_path / "runs", allow_unsafe_local_exec=True)

    result = await runner.run()

    assert result.status == RunStatus.COMPLETED
    assert len(model.calls) == 4
    fourth_call_messages, _tools = model.calls[3]
    summaries = [
        message
        for message in fourth_call_messages
        if isinstance(message, Message)
        and message.content
        and "b9-summary-fallback-v1" in message.content
    ]
    assert len(summaries) == 1
    assert "Compacted source message indexes: 2..5" in (summaries[0].content or "")
    assert isinstance(fourth_call_messages[0], Message)
    assert fourth_call_messages[0].role == "system"
    assert isinstance(fourth_call_messages[1], Message)
    assert fourth_call_messages[1].role == "user"
    events = read_events(result)
    assert len(
        [
            event
            for event in events
            if event["event_type"] == "context_pressure.summary_fallback_applied"
        ]
    ) == 1


@pytest.mark.asyncio
async def test_workspace_context_reminder_is_injected_after_summary_fallback(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = make_task(
        tiny_bug_repo,
        limits=Limits(
            max_steps=4,
            wall_time_seconds=60,
            max_total_tokens=100,
        ),
        verification=(
            VerificationCommand(
                name="check", argv=("git", "diff", "--check"), timeout_seconds=30
            ),
        ),
    )
    patch_call = ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH})
    read_calculator = ToolCall(
        name="read_file",
        arguments={"path": "src/tiny_python_bug/calculator.py"},
    )
    read_package = ToolCall(
        name="read_file",
        arguments={"path": "src/tiny_python_bug/__init__.py"},
    )
    model = ScriptedModel(
        [
            ModelTurn(tool_calls=(patch_call,), usage=Usage(input_tokens=30)),
            ModelTurn(tool_calls=(read_calculator,), usage=Usage(input_tokens=30)),
            ModelTurn(tool_calls=(read_package,), usage=Usage(input_tokens=25)),
            ModelTurn(content="Fixed calculator."),
        ]
    )

    result = await AgentRunner(
        task,
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    assert result.status == RunStatus.COMPLETED, result.model_dump()
    fourth_call_messages, _tools = model.calls[3]
    reminders = [
        message
        for message in fourth_call_messages
        if isinstance(message, Message)
        and message.content
        and WORKSPACE_CONTEXT_REMINDER_VERSION in message.content
    ]
    assert len(reminders) == 1
    reminder = reminders[0].content or ""
    assert "Changed files:" in reminder
    assert "src/tiny_python_bug/calculator.py" in reminder
    assert "Check status:" in reminder
    assert "no checks have run yet" in reminder
    assert "Recent important paths:" in reminder
    assert "Active constraints:" in reminder
    assert "allowed_paths=src/**" in reminder
    events = read_events(result)
    assert len(
        [
            event
            for event in events
            if event["event_type"] == "context_pressure.workspace_reminder_injected"
        ]
    ) == 1


@pytest.mark.asyncio
async def test_resume_detects_existing_workspace_context_reminder_marker(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    run_root = tmp_path / "runs"
    run_id = "resume-marker"
    run_dir = run_root / run_id
    run_dir.mkdir(parents=True)
    shutil.copytree(tiny_bug_repo, run_dir / "workspace")
    task = make_task(
        tiny_bug_repo,
        limits=Limits(
            max_steps=3,
            wall_time_seconds=60,
            max_total_tokens=100,
        ),
        verification=(
            VerificationCommand(
                name="always-pass",
                argv=(sys.executable, "-c", "raise SystemExit(0)"),
                timeout_seconds=5,
            ),
        ),
    )
    (run_dir / "request.json").write_text(
        json.dumps(task.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    existing_reminder = build_workspace_context_reminder(
        changed_files=("src/tiny_python_bug/calculator.py",),
        check_status=("tests: failed (exit 1)",),
        recent_paths=("src/tiny_python_bug/calculator.py",),
        constraints=("allowed_paths=src/**",),
    )
    manifest = SessionManifest.new(
        run_id=run_id,
        task_id=task.task_id,
        provider_name="scripted",
        model_id="scripted",
        protocol=str(ModelProtocol.SCRIPTED),
        base_sha=task.base_sha or "",
    ).model_copy(
        update={
            "phase": SessionPhase.RUNNING,
            "step": 2,
            "messages": (
                Message(role="system", content="system"),
                Message(role="user", content="task"),
                Message(role="user", content="[b9-summary-fallback-v1]\nsummary"),
                existing_reminder,
            ),
            "usage": Usage(input_tokens=85),
        }
    )
    store = SessionStore(run_dir)
    lease = store.acquire_writer()
    try:
        await store.initialize(manifest, lease)
    finally:
        lease.release()
    model = ScriptedModel([ModelTurn(content="Ready to verify.")])

    result = await (
        await AgentRunner.resume(
            run_dir,
            model,
            approval_policy=HeadlessApprovalPolicy(allow_modify=True, allow_execute=True),
        )
    ).run()

    assert result.status == RunStatus.COMPLETED, result.model_dump()
    assert len(model.calls) == 1
    call_messages, _tools = model.calls[0]
    reminders = [
        message
        for message in call_messages
        if isinstance(message, Message)
        and message.content
        and WORKSPACE_CONTEXT_REMINDER_VERSION in message.content
    ]
    assert len(reminders) == 1
    events = read_events(result)
    assert not [
        event
        for event in events
        if event["event_type"] == "context_pressure.workspace_reminder_injected"
    ]


@pytest.mark.asyncio
async def test_retryable_provider_errors_are_retried_until_success(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = _clean_check_task(tiny_bug_repo)
    model = ScriptedModel(
        [
            _retryable_error(500),
            ProviderError(
                "nvidia-nim request failed: overloaded",
                kind=ProviderErrorKind.RETRYABLE,
                provider_name="nvidia-nim",
                status_code=503,
                retry_after_seconds=0.0,
            ),
            ModelTurn(content="The repository is clean; no change is needed."),
        ]
    )
    runner = AgentRunner(task, model, tmp_path / "runs", allow_unsafe_local_exec=True)
    runner.model_retry_delay = lambda attempt, retry_after_seconds: 0.0

    result = await runner.run()

    assert result.status == RunStatus.COMPLETED, result.model_dump()
    assert len(model.calls) == 3
    retries = [
        event for event in read_events(result) if event["event_type"] == "model.retry"
    ]
    assert [event["data"]["attempt"] for event in retries] == [1, 2]
    assert all(event["data"]["provider"] == "nvidia-nim" for event in retries)
    assert [event["data"]["delay_seconds"] for event in retries] == [0.0, 0.0]


@pytest.mark.asyncio
async def test_exhausted_retryable_provider_errors_fail_with_readable_error(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = _clean_check_task(tiny_bug_repo)
    model = ScriptedModel(
        [_retryable_error(code) for code in (500, 503, 500, 502, 504)]
    )
    runner = AgentRunner(task, model, tmp_path / "runs", allow_unsafe_local_exec=True)
    runner.model_retry_delay = lambda attempt, retry_after_seconds: 0.0

    result = await runner.run()

    assert result.status == RunStatus.FAILED
    assert result.terminal_reason == "provider_retryable"
    assert len(model.calls) == 5
    assert result.error is not None
    assert "nvidia-nim" in result.error
    assert "5 consecutive" in result.error
    assert "500" in result.error and "503" in result.error
    events = read_events(result)
    assert [e["data"]["attempt"] for e in events if e["event_type"] == "model.retry"] == [
        1,
        2,
        3,
        4,
    ]
    failed = [e for e in events if e["event_type"] == "model.failed"]
    assert failed and failed[0]["data"]["retryable"] is True


@pytest.mark.asyncio
async def test_retry_exhaustion_falls_back_to_next_model_with_fresh_budget(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = _clean_check_task(tiny_bug_repo)
    primary = ScriptedModel(
        [_retryable_error(500)] * 5, model_id="primary"
    )
    fallback = ScriptedModel(
        [ModelTurn(content="The repository is clean; no change is needed.")],
        model_id="fallback",
    )
    runner = AgentRunner(
        task,
        primary,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
        fallback_models=(fallback,),
    )
    runner.model_retry_delay = lambda attempt, retry_after_seconds: 0.0

    result = await runner.run()

    assert result.status == RunStatus.COMPLETED, result.model_dump()
    assert len(primary.calls) == 5
    assert len(fallback.calls) == 1
    events = read_events(result)
    fallback_events = [e for e in events if e["event_type"] == "model.fallback"]
    assert len(fallback_events) == 1
    data = fallback_events[0]["data"]
    assert data["from_model"] == "primary"
    assert data["to_model"] == "fallback"
    assert data["failure_codes"] == [500, 500, 500, 500, 500]
    assert [e["data"]["attempt"] for e in events if e["event_type"] == "model.retry"] == [
        1,
        2,
        3,
        4,
    ]


@pytest.mark.asyncio
async def test_fallback_exhaustion_fails_after_all_candidates(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = _clean_check_task(tiny_bug_repo)
    primary = ScriptedModel([_retryable_error(500)] * 5, model_id="primary")
    fallback = ScriptedModel([_retryable_error(503)] * 5, model_id="fallback")
    runner = AgentRunner(
        task,
        primary,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
        fallback_models=(fallback,),
    )
    runner.model_retry_delay = lambda attempt, retry_after_seconds: 0.0

    result = await runner.run()

    assert result.status == RunStatus.FAILED
    assert result.terminal_reason == "provider_retryable"
    assert len(primary.calls) == 5
    assert len(fallback.calls) == 5
    events = read_events(result)
    assert len([e for e in events if e["event_type"] == "model.fallback"]) == 1


@pytest.mark.asyncio
async def test_verified_patch_runs_read_only_reviewer_lane(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = _clean_check_task(tiny_bug_repo)
    primary = ScriptedModel(
        [
            ModelTurn(
                tool_calls=(ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),),
                usage=Usage(input_tokens=10, output_tokens=5),
            ),
            ModelTurn(content="Fixed calculator.", usage=Usage(input_tokens=20, output_tokens=5)),
        ],
        model_id="primary",
    )
    reviewer = ScriptedModel(
        [ModelTurn(content="Verdict: no findings.", usage=Usage(input_tokens=7, output_tokens=3))],
        model_id="reviewer",
    )

    result = await AgentRunner(
        task,
        primary,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
        review_model=reviewer,
    ).run()

    assert result.status == RunStatus.COMPLETED, result.model_dump()
    assert "Reviewer lane:\nVerdict: no findings." in result.summary
    assert Path(result.artifacts["review"]).read_text(encoding="utf-8") == (
        "Verdict: no findings."
    )
    assert len(reviewer.calls) == 1
    review_messages, review_tools = reviewer.calls[0]
    assert review_tools == ()
    assert "Patch:" in (review_messages[1].content or "")
    assert [record.lane for record in result.model_usage] == ["primary", "primary", "reviewer"]
    assert result.usage.total_tokens == 50
    assert result.cost is None
    events = read_events(result)
    assert any(event["event_type"] == "role_lane.requested" for event in events)
    assert any(event["event_type"] == "role_lane.completed" for event in events)


@pytest.mark.asyncio
async def test_non_retryable_provider_errors_fail_without_retrying(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = _clean_check_task(tiny_bug_repo)
    model = ScriptedModel(
        [
            ProviderError(
                "nvidia-nim request failed: invalid api key",
                kind=ProviderErrorKind.AUTH,
                provider_name="nvidia-nim",
                status_code=401,
            ),
        ]
    )
    runner = AgentRunner(task, model, tmp_path / "runs", allow_unsafe_local_exec=True)
    runner.model_retry_delay = lambda attempt, retry_after_seconds: 0.0

    result = await runner.run()

    assert result.status == RunStatus.FAILED
    assert result.terminal_reason == "provider_auth"
    assert result.error is not None
    assert "nvidia-nim" in result.error
    assert "auth" in result.error
    assert "invalid api key" in result.error
    assert len(model.calls) == 1
    assert not [
        event for event in read_events(result) if event["event_type"] == "model.retry"
    ]


def test_retry_delay_uses_jitter_and_caps() -> None:
    from rivumi.loop import (
        RETRY_JITTER_FRACTION,
        RETRY_MAX_DELAY_SECONDS,
        RETRY_SERVER_HINT_MAX_SECONDS,
        retry_delay_seconds,
    )

    for attempt in range(1, 12):
        delay = retry_delay_seconds(attempt, None)
        base = min(1.0 * 2 ** (attempt - 1), RETRY_MAX_DELAY_SECONDS)
        assert base * (1 - RETRY_JITTER_FRACTION) <= delay <= base * (1 + RETRY_JITTER_FRACTION)
    assert retry_delay_seconds(20, None) <= RETRY_MAX_DELAY_SECONDS * (1 + RETRY_JITTER_FRACTION)
    hint = retry_delay_seconds(1, 120.0)
    assert hint == 120.0
    assert retry_delay_seconds(1, 9999.0) == RETRY_SERVER_HINT_MAX_SECONDS
