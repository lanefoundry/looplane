from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

import pytest
from conftest import run_git

from looplane.approvals import (
    ApprovalDecision,
    ApprovalReason,
    ApprovalRequest,
    CallbackApprovalPolicy,
    HeadlessApprovalPolicy,
    ToolEffect,
)
from looplane.cache_strategy import ProviderCacheTrace
from looplane.contracts import (
    InjectedContext,
    Limits,
    Message,
    ModelCapabilities,
    ModelProtocol,
    ModelTurn,
    RunResult,
    RunStatus,
    TaskContract,
    ToolCall,
    ToolObservation,
    Usage,
    VerificationCommand,
)
from looplane.loop import AgentRunner
from looplane.models import ProviderError, ProviderErrorKind, ScriptedModel
from looplane.permissions import PermissionGuard
from looplane.prompts import (
    INTERACTION_CONTEXT_VERSION,
    WORKSPACE_CONTEXT_REMINDER_VERSION,
    WORKSPACE_STATE_CONTEXT_VERSION,
    build_workspace_context_reminder,
)
from looplane.session import SessionManifest, SessionPhase, SessionStore
from looplane.tools import ToolExecutor

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

# A verification command that checks the fix landed without writing stray cache
# files into the workspace -- unlike `pytest`, whose own `.pytest_cache` output
# would itself count as an (unrelated) change when direct-edit mode diffs the
# real repository against base_sha.
IMPORT_CHECK_ARGV = (
    sys.executable,
    "-c",
    "import sys; sys.path.insert(0, 'src'); "
    "from tiny_python_bug.calculator import add; "
    "assert add(2, 3) == 5",
)


def make_task(
    repository: Path,
    *,
    limits: Limits,
    verification: tuple[VerificationCommand, ...] | None = None,
    allowed_paths: tuple[str, ...] = ("src/**",),
    enabled_skills: tuple[str, ...] = (),
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
        enabled_skills=enabled_skills,
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
    monkeypatch.setenv("LOOPLANE_MEMORY_PATH", str(memory_path))
    from looplane.memory import remember

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
    monkeypatch.setenv("LOOPLANE_USER_INSTRUCTIONS", str(tmp_path / "missing.md"))
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
async def test_initial_prompt_injects_tool_workspace_and_runtime_sections(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=1))
    model = ScriptedModel([ModelTurn(content="No change needed.")])

    await AgentRunner(
        task=task,
        model=model,
        run_root=tmp_path / "runs",
        allow_unsafe_local_exec=True,
        sandbox_checks=True,
        sandbox_backend="none",
    ).run()

    first_messages, _tools = model.calls[0]
    system = first_messages[0]
    assert isinstance(system, Message)
    content = system.content or ""
    assert "<section name='tool_policy' cache='stable'>" in content
    assert "[b1-tool-policy-v1]" in content
    assert "- read_file" in content
    assert "- dispatch_subagents" in content
    assert "[a10-subagent-planner-policy-v1]" in content
    assert "Use proposed_transaction" in content
    assert "<section name='interaction_policy' cache='stable'>" in content
    assert f"[{INTERACTION_CONTEXT_VERSION}]" in content
    assert "ask_mode: ask_only_when_required_or_high_risk" in content
    assert "<section name='runtime_context' cache='dynamic'>" in content
    assert "sandbox_checks: True" in content
    assert "<section name='workspace_state' cache='dynamic'>" in content
    assert f"[{WORKSPACE_STATE_CONTEXT_VERSION}]" in content
    assert "allowed_paths:" in content
    assert "verification_required_after_file_changes:" in content
    assert "git_status_short:" in content
    assert "- ##" in content


@pytest.mark.asyncio
async def test_initial_prompt_omits_subagent_planner_when_dispatch_disabled(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=1))
    model = ScriptedModel([ModelTurn(content="No change needed.")])

    await AgentRunner(
        task=task,
        model=model,
        run_root=tmp_path / "runs",
        allow_unsafe_local_exec=True,
        enable_subagent_dispatch=False,
    ).run()

    first_messages, _tools = model.calls[0]
    system = first_messages[0]
    assert isinstance(system, Message)
    content = system.content or ""
    assert "[a10-subagent-planner-policy-v1]" not in content
    assert "- dispatch_subagents" not in content


@pytest.mark.asyncio
async def test_model_cache_trace_is_persisted_as_event_and_artifact(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    class CacheTracingModel(ScriptedModel):
        def __init__(self) -> None:
            super().__init__([ModelTurn(content="No change needed.")])
            self.last_cache_trace: ProviderCacheTrace | None = None

        async def complete(self, messages, tools=()):
            turn = await super().complete(messages, tools)
            self.last_cache_trace = ProviderCacheTrace(
                provider="openai-responses",
                prompt_cache_key="looplane-responses:test",
                tool_schema_fingerprint="tools",
                cache_control_blocks=0,
            )
            return turn

    task = make_task(tiny_bug_repo, limits=Limits(max_steps=1, wall_time_seconds=30))
    model = CacheTracingModel()

    result = await AgentRunner(
        task=task,
        model=model,
        run_root=tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    assert result.status == RunStatus.COMPLETED, result.model_dump()
    assert "cache_traces" in result.artifacts
    records = [
        json.loads(line)
        for line in Path(result.artifacts["cache_traces"]).read_text().splitlines()
        if line.strip()
    ]
    assert records == [
        {
            "lane": "primary",
            "model": "scripted",
            "provider": "scripted",
            "step": 1,
            "trace": {
                "cache_control_blocks": 0,
                "cache_ready": True,
                "prompt_cache_key": "looplane-responses:test",
                "provider": "openai-responses",
                "tool_schema_fingerprint": "tools",
                "warnings": [],
            },
        }
    ]
    events = read_events(result)
    trace_event = next(event for event in events if event["event_type"] == "model.cache_trace")
    assert trace_event["data"]["cache_ready"] is True
    assert trace_event["data"]["prompt_cache_key"] == "looplane-responses:test"


@pytest.mark.asyncio
async def test_initial_prompt_injects_project_skills(tiny_bug_repo: Path, tmp_path: Path) -> None:
    skills = tiny_bug_repo / ".looplane" / "skills"
    skills.mkdir(parents=True)
    (skills / "review.md").write_text(
        "---\nname: reviewer\ndescription: local review skill\n---\nCheck edge cases.",
        encoding="utf-8",
    )
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
    assert "Project skills from .looplane/skills" in (system.content or "")
    assert "Check edge cases." in (system.content or "")


@pytest.mark.asyncio
async def test_initial_prompt_injects_only_enabled_project_skills(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    skills = tiny_bug_repo / ".looplane" / "skills"
    skills.mkdir(parents=True)
    (skills / "review.md").write_text(
        "---\nname: reviewer\n---\nReview changed code.",
        encoding="utf-8",
    )
    (skills / "test.md").write_text(
        "---\nname: test-writer\n---\nWrite regression tests.",
        encoding="utf-8",
    )
    task = make_task(
        tiny_bug_repo,
        limits=Limits(max_steps=1),
        enabled_skills=("test-writer",),
    )
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
    assert "Write regression tests." in (system.content or "")
    assert "Review changed code." not in (system.content or "")


@pytest.mark.asyncio
async def test_runtime_context_provider_injects_context_before_model_request(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = tmp_path / "provider.py"
    provider.write_text(
        """
import json
import sys

payload = json.loads(sys.stdin.read())
print(json.dumps({"source": "ide", "content": f"provider step={payload['payload']['step']}"}))
""".lstrip(),
        encoding="utf-8",
    )
    looplane_dir = tiny_bug_repo / ".looplane"
    looplane_dir.mkdir()
    (looplane_dir / "context-providers.json").write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "ide",
                        "command": [sys.executable, str(provider)],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LOOPLANE_ENABLE_PROJECT_HOOKS", "1")
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=2))
    model = ScriptedModel(
        [
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        name="read_file",
                        arguments={"path": "src/tiny_python_bug/calculator.py"},
                    ),
                )
            ),
            ModelTurn(content="No change needed."),
        ]
    )

    result = await AgentRunner(
        task,
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    assert result.status is RunStatus.COMPLETED
    second_messages, _tools = model.calls[1]
    injected = [
        message
        for message in second_messages
        if isinstance(message, InjectedContext) and message.source == "context_provider:ide"
    ]
    assert injected
    assert "provider step=1" in injected[-1].content
    events = read_events(result)
    assert any(event["event_type"] == "context_provider.injected" for event in events)


@pytest.mark.asyncio
async def test_project_pre_tool_hook_can_deny_tool_execution(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks_dir = tiny_bug_repo / ".looplane"
    hooks_dir.mkdir(exist_ok=True)
    hook_script = hooks_dir / "deny_read.py"
    hook_script.write_text(
        """
from __future__ import annotations

import json
import sys

payload = json.loads(sys.stdin.read())
if payload["payload"]["tool_call"]["name"] == "read_file":
    print(json.dumps({"decision": "deny", "reason": "read blocked by test hook"}))
""".lstrip(),
        encoding="utf-8",
    )
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "pre_tool_use": [
                    {
                        "command": [sys.executable, str(hook_script)],
                        "tools": ["read_file"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LOOPLANE_ENABLE_PROJECT_HOOKS", "1")
    model = ScriptedModel(
        [
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        name="read_file",
                        arguments={"path": "src/tiny_python_bug/calculator.py"},
                    ),
                )
            ),
            ModelTurn(content="No source change was needed."),
        ]
    )
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=2))

    result = await AgentRunner(
        task=task,
        model=model,
        run_root=tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()
    events = read_events(result)

    assert result.status is RunStatus.COMPLETED
    assert any(event["event_type"] == "hook.denied" for event in events)
    assert not any(
        event["event_type"] == "tool.started" and event["data"].get("name") == "read_file"
        for event in events
    )
    assert any(
        event["event_type"] == "tool.completed"
        and event["data"].get("name") == "read_file"
        and event["data"].get("error") == "action denied by user"
        for event in events
    )


@pytest.mark.asyncio
async def test_ide_diagnostics_are_injected_as_harness_context(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    diagnostics_dir = tiny_bug_repo / ".looplane" / "ide"
    diagnostics_dir.mkdir(parents=True)
    (diagnostics_dir / "diagnostics.json").write_text(
        json.dumps(
            {
                "diagnostics": [
                    {
                        "path": "src/tiny_python_bug/calculator.py",
                        "range": {
                            "start": {"line": 2, "character": 11},
                            "end": {"line": 2, "character": 16},
                        },
                        "severity": 1,
                        "source": "pyright",
                        "code": "operator",
                        "message": "Operator '-' is suspicious here.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=1))
    model = ScriptedModel([ModelTurn(content="No change needed.")])

    result = await AgentRunner(
        task=task,
        model=model,
        run_root=tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    assert result.status is RunStatus.COMPLETED
    first_messages, _tools = model.calls[0]
    diagnostics = [
        message
        for message in first_messages
        if isinstance(message, InjectedContext) and message.source == "ide_diagnostics"
    ]
    assert len(diagnostics) == 1
    assert "[ide-lsp-diagnostics-v1]" in diagnostics[0].content
    assert "src/tiny_python_bug/calculator.py:3:12" in diagnostics[0].content
    events = read_events(result)
    assert any(event["event_type"] == "ide.diagnostics_injected" for event in events)


@pytest.mark.asyncio
async def test_ide_open_files_are_injected_as_harness_context(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    ide_dir = tiny_bug_repo / ".looplane" / "ide"
    ide_dir.mkdir(parents=True)
    (ide_dir / "open-files.json").write_text(
        json.dumps(
            {
                "files": [
                    {
                        "path": "src/tiny_python_bug/calculator.py",
                        "active": True,
                        "cursor": {"line": 2, "character": 11},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=1))
    model = ScriptedModel([ModelTurn(content="No change needed.")])

    result = await AgentRunner(
        task=task,
        model=model,
        run_root=tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    assert result.status is RunStatus.COMPLETED
    first_messages, _tools = model.calls[0]
    open_files = [
        message
        for message in first_messages
        if isinstance(message, InjectedContext) and message.source == "ide_open_files"
    ]
    assert len(open_files) == 1
    assert "[ide-open-files-v1]" in open_files[0].content
    assert "src/tiny_python_bug/calculator.py (active, cursor=3:12)" in open_files[0].content
    events = read_events(result)
    assert any(event["event_type"] == "ide.open_files_injected" for event in events)


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
            ModelTurn(tool_calls=(ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),)),
            ModelTurn(tool_calls=(ToolCall(name="run_check", arguments={"name": "tests"}),)),
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
    model_check_approval = next(
        event
        for event in events
        if event["event_type"] == "approval.requested"
        and event["data"].get("reason") == ApprovalReason.MODEL_TOOL
        and event["data"].get("effect") == ToolEffect.EXECUTE
    )
    assert model_check_approval["data"]["preview"] == "$ pytest -q"
    assert checkpoint["status"] == RunStatus.COMPLETED
    assert persisted_result["status"] == RunStatus.COMPLETED
    assert "-    return left - right" in patch
    assert "+    return left + right" in patch
    assert "passed" in test_log.lower()


@pytest.mark.asyncio
async def test_agent_runner_continues_conversation_with_prior_messages_and_workspace_edits(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    source_sha = run_git(tiny_bug_repo, "rev-parse", "HEAD")
    run_root = tmp_path / "runs"
    task = TaskContract(
        task_id="tiny-python-bug",
        repository=tiny_bug_repo,
        base_sha=source_sha,
        instruction="Fix add so the existing test passes. Do not change tests.",
        allowed_paths=("src/**",),
        verification=(
            VerificationCommand(name="tests", argv=("pytest", "-q"), timeout_seconds=30),
        ),
        limits=Limits(max_steps=4, wall_time_seconds=60),
    )
    model_turn1 = ScriptedModel(
        turns=[
            ModelTurn(tool_calls=(ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),)),
            ModelTurn(tool_calls=(ToolCall(name="run_check", arguments={"name": "tests"}),)),
            ModelTurn(content="Fixed the calculator and verified the test suite."),
        ]
    )
    runner1 = AgentRunner(
        task=task,
        model=model_turn1,
        run_root=run_root,
        allow_unsafe_local_exec=True,
        approval_policy=CallbackApprovalPolicy(lambda _request: ApprovalDecision.ALLOW_SESSION),
    )
    result1 = await runner1.run()

    assert result1.status == RunStatus.COMPLETED, result1.model_dump()
    assert result1.changed_files == ("src/tiny_python_bug/calculator.py",)
    verification_events_before_follow_up = sum(
        event["event_type"] == "verification.started" for event in read_events(result1)
    )
    assert runner1._manifest is not None
    assert ToolEffect.MODIFY in runner1._manifest.granted_effects

    workspace_file = runner1.run_dir / "workspace" / "src" / "tiny_python_bug" / "calculator.py"
    edited_contents = workspace_file.read_bytes()
    assert b"left + right" in edited_contents

    # A single-step budget would immediately exceed the guard if the step count carried
    # over cumulatively from turn 1 instead of resetting per turn.
    follow_up_task = TaskContract(
        task_id="tiny-python-bug",
        repository=tiny_bug_repo,
        base_sha=source_sha,
        instruction="Now also add a docstring example.",
        allowed_paths=("src/**",),
        verification=(
            VerificationCommand(name="tests", argv=("pytest", "-q"), timeout_seconds=30),
        ),
        limits=Limits(max_steps=1, wall_time_seconds=60),
    )
    model_turn2 = ScriptedModel(
        turns=[ModelTurn(content="Already fixed in the previous turn; nothing further needed.")]
    )
    runner2 = AgentRunner(
        task=follow_up_task,
        model=model_turn2,
        run_root=run_root,
        run_id=runner1.run_id,
        continuation=True,
        allow_unsafe_local_exec=True,
    )
    result2 = await runner2.run()

    assert result2.status == RunStatus.COMPLETED, result2.model_dump()
    assert result2.terminal_reason == "no_changes"
    assert result2.verification == ()
    assert result2.run_id == runner1.run_id
    assert len(model_turn2.calls) == 1
    assert (
        sum(event["event_type"] == "verification.started" for event in read_events(result2))
        == verification_events_before_follow_up
    )

    call_messages, _tools = model_turn2.calls[0]
    text_contents = [
        message.content
        for message in call_messages
        if isinstance(message, Message) and isinstance(message.content, str)
    ]
    assert any("Fix add so the existing test passes" in text for text in text_contents)
    assert any("Now also add a docstring example." in text for text in text_contents)

    # The disposable clone was reused, not re-cloned from HEAD: turn 1's edit is still there.
    assert workspace_file.read_bytes() == edited_contents
    assert runner2._manifest is not None
    assert ToolEffect.MODIFY in runner2._manifest.granted_effects

    # A non-ignored out-of-band change invalidates the verified workspace stamp,
    # even when the next model turn itself does not call a modifying tool.
    (workspace_file.parent / "external.py").write_text("VALUE = 1\n", encoding="utf-8")
    model_turn3 = ScriptedModel(turns=[ModelTurn(content="No additional edits requested.")])
    runner3 = AgentRunner(
        task=follow_up_task.model_copy(update={"instruction": "Inspect the current state."}),
        model=model_turn3,
        run_root=run_root,
        run_id=runner1.run_id,
        continuation=True,
        allow_unsafe_local_exec=True,
    )

    result3 = await runner3.run()

    assert result3.status == RunStatus.COMPLETED, result3.model_dump()
    assert result3.terminal_reason == "verified"
    assert (
        sum(event["event_type"] == "verification.started" for event in read_events(result3))
        == verification_events_before_follow_up + 1
    )


@pytest.mark.asyncio
async def test_agent_runner_continuation_falls_back_to_fresh_run_on_model_mismatch(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    run_root = tmp_path / "runs"
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=2, wall_time_seconds=30))
    model1 = ScriptedModel(turns=[ModelTurn(content="Hello!")])
    runner1 = AgentRunner(
        task=task,
        model=model1,
        run_root=run_root,
        allow_unsafe_local_exec=True,
    )
    result1 = await runner1.run()
    assert result1.status == RunStatus.COMPLETED, result1.model_dump()

    other_model = ScriptedModel(
        turns=[ModelTurn(content="Hello again!")], model_id="scripted-other"
    )
    followup_task = make_task(tiny_bug_repo, limits=Limits(max_steps=2, wall_time_seconds=30))
    runner2 = AgentRunner(
        task=followup_task,
        model=other_model,
        run_root=run_root,
        run_id=runner1.run_id,
        continuation=True,
        allow_unsafe_local_exec=True,
    )
    result2 = await runner2.run()

    assert result2.status == RunStatus.COMPLETED, result2.model_dump()
    assert result2.run_id != runner1.run_id
    events = read_events(result2)
    assert any(event["event_type"] == "run.continuation_fallback" for event in events)


@pytest.mark.asyncio
async def test_agent_runner_continuation_falls_back_when_workspace_missing(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    run_root = tmp_path / "runs"
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=2, wall_time_seconds=30))
    model1 = ScriptedModel(turns=[ModelTurn(content="Nothing to fix yet.")])
    runner1 = AgentRunner(
        task=task,
        model=model1,
        run_root=run_root,
        allow_unsafe_local_exec=True,
    )
    result1 = await runner1.run()
    assert result1.status == RunStatus.COMPLETED, result1.model_dump()

    shutil.rmtree(runner1.run_dir / "workspace")

    model2 = ScriptedModel(turns=[ModelTurn(content="Starting fresh.")])
    followup_task = make_task(tiny_bug_repo, limits=Limits(max_steps=2, wall_time_seconds=30))
    runner2 = AgentRunner(
        task=followup_task,
        model=model2,
        run_root=run_root,
        run_id=runner1.run_id,
        continuation=True,
        allow_unsafe_local_exec=True,
    )
    result2 = await runner2.run()

    assert result2.status == RunStatus.COMPLETED, result2.model_dump()
    assert result2.run_id != runner1.run_id
    events = read_events(result2)
    assert any(event["event_type"] == "run.continuation_fallback" for event in events)


@pytest.mark.asyncio
async def test_scripted_model_edits_real_repo_directly_when_direct_edit_enabled(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    source_sha = run_git(tiny_bug_repo, "rev-parse", "HEAD")
    source_file = tiny_bug_repo / "src" / "tiny_python_bug" / "calculator.py"
    source_contents = source_file.read_bytes()
    task = TaskContract(
        task_id="tiny-python-bug",
        repository=tiny_bug_repo,
        base_sha=source_sha,
        instruction="Fix add so the existing test passes. Do not change tests.",
        allowed_paths=("src/**",),
        verification=(
            VerificationCommand(name="tests", argv=IMPORT_CHECK_ARGV, timeout_seconds=30),
        ),
        limits=Limits(max_steps=4, wall_time_seconds=60),
    )
    model = ScriptedModel(
        turns=[
            ModelTurn(tool_calls=(ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),)),
            ModelTurn(tool_calls=(ToolCall(name="run_check", arguments={"name": "tests"}),)),
            ModelTurn(content="Fixed the calculator directly in the real repository."),
        ]
    )

    result = await AgentRunner(
        task=task,
        model=model,
        run_root=tmp_path / "runs",
        allow_unsafe_local_exec=True,
        allow_direct_repo_edit=True,
    ).run()

    assert result.status == RunStatus.COMPLETED, result.model_dump()
    assert result.changed_files == ("src/tiny_python_bug/calculator.py",)
    assert all(outcome.ok for outcome in result.verification)

    # No disposable clone: the edit landed directly in the real repository, in place.
    assert run_git(tiny_bug_repo, "rev-parse", "HEAD") == source_sha
    assert source_file.read_bytes() != source_contents
    assert b"left + right" in source_file.read_bytes()
    assert run_git(tiny_bug_repo, "status", "--porcelain=v1") != ""

    patch = Path(result.artifacts["patch"]).read_text()
    assert "-    return left - right" in patch
    assert "+    return left + right" in patch

    events = read_events(result)
    assert any(event["event_type"] == "workspace.direct_edit_enabled" for event in events)
    assert not [
        event for event in events if event["event_type"] == "workspace.dirty_source_detected"
    ]


@pytest.mark.asyncio
async def test_failed_modifying_tool_side_effect_still_requires_verification(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_execute = ToolExecutor.execute

    def execute_with_partial_side_effect(
        executor: ToolExecutor,
        call: ToolCall,
        *,
        timeout_seconds: float | None = None,
    ) -> ToolObservation:
        if call.name == "replace_text":
            path = executor.workspace / "src" / "tiny_python_bug" / "calculator.py"
            path.write_text(
                path.read_text(encoding="utf-8").replace("left - right", "left + right"),
                encoding="utf-8",
            )
            return ToolObservation(
                tool_call_id=call.tool_call_id,
                name=call.name,
                ok=False,
                error="simulated tool failure after a partial write",
            )
        return original_execute(executor, call, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(ToolExecutor, "execute", execute_with_partial_side_effect)
    task = make_task(
        tiny_bug_repo,
        limits=Limits(max_steps=3, wall_time_seconds=30),
        verification=(
            VerificationCommand(name="tests", argv=IMPORT_CHECK_ARGV, timeout_seconds=30),
        ),
    )
    model = ScriptedModel(
        [
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        name="replace_text",
                        arguments={
                            "path": "src/tiny_python_bug/calculator.py",
                            "old_text": "left - right",
                            "new_text": "left + right",
                        },
                    ),
                )
            ),
            ModelTurn(content="The attempted edit reported a failure."),
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
    assert any(
        event["event_type"] == "verification.started" for event in read_events(result)
    )


@pytest.mark.asyncio
async def test_passing_verification_that_mutates_workspace_is_not_trusted(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    mutating_check = (
        sys.executable,
        "-c",
        "from pathlib import Path; "
        "p = Path('src/tiny_python_bug/calculator.py'); "
        "p.write_text(p.read_text() + '# changed by verification\\n')",
    )
    task = make_task(
        tiny_bug_repo,
        limits=Limits(max_steps=2, wall_time_seconds=30),
        verification=(
            VerificationCommand(name="mutating", argv=mutating_check, timeout_seconds=30),
        ),
    )
    model = ScriptedModel(
        [
            ModelTurn(tool_calls=(ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),)),
            ModelTurn(content="Fixed the calculator."),
        ]
    )

    result = await AgentRunner(
        task,
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    assert result.status == RunStatus.FAILED
    assert result.terminal_reason == "error_ToolExecutionError"
    assert "workspace changed while final verification was running" in result.summary


@pytest.mark.asyncio
async def test_direct_repo_edit_reports_dirty_source_warning(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    source_sha = run_git(tiny_bug_repo, "rev-parse", "HEAD")
    unrelated_file = tiny_bug_repo / "TASK.md"
    unrelated_file.write_text(unrelated_file.read_text() + "\npre-existing local note\n")

    task = TaskContract(
        task_id="tiny-python-bug",
        repository=tiny_bug_repo,
        base_sha=source_sha,
        instruction="Fix add so the existing test passes. Do not change tests.",
        allowed_paths=("src/**",),
        verification=(
            VerificationCommand(name="tests", argv=IMPORT_CHECK_ARGV, timeout_seconds=30),
        ),
        limits=Limits(max_steps=4, wall_time_seconds=60),
    )
    model = ScriptedModel(
        turns=[
            ModelTurn(tool_calls=(ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),)),
            ModelTurn(tool_calls=(ToolCall(name="run_check", arguments={"name": "tests"}),)),
            ModelTurn(content="Fixed the calculator."),
        ]
    )

    result = await AgentRunner(
        task=task,
        model=model,
        run_root=tmp_path / "runs",
        allow_unsafe_local_exec=True,
        allow_direct_repo_edit=True,
    ).run()

    assert result.status == RunStatus.COMPLETED, result.model_dump()
    # Pre-existing dirt is left exactly as the user had it, and never reported.
    patch = Path(result.artifacts["patch"]).read_text()
    assert "TASK.md" not in patch
    assert "pre-existing local note" in unrelated_file.read_text()

    events = read_events(result)
    dirty_events = [
        event for event in events if event["event_type"] == "workspace.dirty_source_detected"
    ]
    assert len(dirty_events) == 1
    assert "TASK.md" in dirty_events[0]["data"]["status_lines"]

    call_messages, _tools = model.calls[0]
    text_contents = [
        message.content
        for message in call_messages
        if isinstance(message, Message) and isinstance(message.content, str)
    ]
    assert any("direct_edit_warning" in text for text in text_contents)


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
    first_messages, _tools = model.calls[0]
    task_request = next(
        item.content
        for item in first_messages
        if isinstance(item, Message) and item.role == "user" and item.content.startswith("Task:")
    )
    assert "Verification required after file changes:" in task_request
    assert "For a read-only request" in task_request
    assert "make the smallest correct patch, and verify it" not in task_request
    events = read_events(result)
    assert not any(
        event["event_type"] == "tool.requested"
        and event["data"].get("name") == "run_check"
        for event in events
    )
    assert not any(event["event_type"] == "verification.started" for event in events)
    assert not any(event["event_type"].startswith("verification.") for event in events)
    assert events[-1]["event_type"] == "run.completed"
    assert events[-1]["data"]["terminal_reason"] == "no_changes"


@pytest.mark.asyncio
async def test_read_only_continuation_reuses_settled_workspace_without_verification(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    run_root = tmp_path / "runs"
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=4, wall_time_seconds=30))
    first_runner = AgentRunner(
        task,
        ScriptedModel([ModelTurn(content="The repository is ready.")]),
        run_root,
        allow_unsafe_local_exec=True,
    )
    first_result = await first_runner.run()
    first_events = read_events(first_result)

    second_runner = AgentRunner(
        task.model_copy(update={"instruction": "Explain the current state."}),
        ScriptedModel([ModelTurn(content="No files need to change.")]),
        run_root,
        run_id=first_result.run_id,
        continuation=True,
        allow_unsafe_local_exec=True,
    )
    second_result = await second_runner.run()
    continuation_events = read_events(second_result)[len(first_events) :]

    assert first_result.terminal_reason == "no_changes"
    assert second_result.terminal_reason == "no_changes"
    assert second_result.verification == ()
    assert not any(
        event["event_type"].startswith("verification.") for event in continuation_events
    )


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
async def test_native_loop_dispatches_read_only_scout_subagent(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=2, wall_time_seconds=30))
    model = ScriptedModel(
        [
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        name="dispatch_subagents",
                        arguments={
                            "agents": [
                                {
                                    "id": "scout-a",
                                    "role": "scout",
                                    "instruction": "Inspect calculator only.",
                                    "allowed_paths": ["src/tiny_python_bug/**"],
                                    "max_steps": 1,
                                }
                            ]
                        },
                    ),
                )
            ),
            ModelTurn(content="Scout found no required change."),
            ModelTurn(content="Parent done."),
        ]
    )

    result = await AgentRunner(
        task,
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    assert result.status == RunStatus.COMPLETED, result.model_dump()
    parent_second_messages, parent_tools = model.calls[2]
    assert "dispatch_subagents" in {tool.name for tool in parent_tools}
    observations = [
        message
        for message in parent_second_messages
        if isinstance(message, ToolObservation) and message.name == "dispatch_subagents"
    ]
    assert len(observations) == 1
    assert "## scout-a" in observations[0].content
    assert "status: completed" in observations[0].content
    events = read_events(result)
    assert any(event["event_type"] == "subagents.dispatch_started" for event in events)
    assert any(event["event_type"] == "subagents.dispatch_completed" for event in events)


@pytest.mark.asyncio
async def test_native_loop_dispatches_named_roles_with_handoff(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=2, wall_time_seconds=30))
    model = ScriptedModel(
        [
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        name="dispatch_subagents",
                        arguments={
                            "agents": [
                                {
                                    "id": "analysis",
                                    "role": "analyst",
                                    "instruction": "Inspect calculator behavior.",
                                    "allowed_paths": ["src/tiny_python_bug/**"],
                                    "max_steps": 1,
                                },
                                {
                                    "id": "review",
                                    "role": "reviewer",
                                    "instruction": "Review the analyst report.",
                                    "depends_on": ["analysis"],
                                    "allowed_paths": ["src/tiny_python_bug/**"],
                                    "max_steps": 1,
                                },
                            ]
                        },
                    ),
                )
            ),
            ModelTurn(content="Analyst says calculator subtracts."),
            ModelTurn(content="Parent done."),
        ],
        model_id="parent",
    )
    reviewer_model = ScriptedModel(
        [ModelTurn(content="Reviewer confirms the analyst report.")],
        model_id="reviewer-routed",
    )

    result = await AgentRunner(
        task,
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
        subagent_models={"reviewer": reviewer_model},
    ).run()

    assert result.status == RunStatus.COMPLETED, result.model_dump()
    assert len(reviewer_model.calls) == 1
    reviewer_messages, _reviewer_tools = reviewer_model.calls[0]
    reviewer_user = next(
        message
        for message in reviewer_messages
        if isinstance(message, Message) and message.role == "user"
    )
    assert "Role: reviewer" in reviewer_user.content
    assert "Prior subagent handoff reports:" in reviewer_user.content
    assert "[analysis] status=completed" in reviewer_user.content
    assert "Analyst says calculator subtracts." in reviewer_user.content
    parent_messages, _parent_tools = model.calls[2]
    observations = [
        message
        for message in parent_messages
        if isinstance(message, ToolObservation) and message.name == "dispatch_subagents"
    ]
    assert "role: analyst" in observations[0].content
    assert "role: reviewer" in observations[0].content
    assert "depends_on: analysis" in observations[0].content
    assert "model: scripted/reviewer-routed" in observations[0].content
    events = read_events(result)
    schedule_events = [
        event for event in events if event["event_type"] == "subagents.schedule_normalized"
    ]
    assert schedule_events[0]["data"]["agents"] == [
        {
            "id": "analysis",
            "role": "analyst",
            "depends_on": [],
            "wave": 0,
            "max_steps": 1,
            "proposed_transaction": False,
        },
        {
            "id": "review",
            "role": "reviewer",
            "depends_on": ["analysis"],
            "wave": 1,
            "max_steps": 1,
            "proposed_transaction": False,
        },
    ]
    assert len([event for event in events if event["event_type"] == "subagents.wave_started"]) == 2


@pytest.mark.asyncio
async def test_native_loop_executes_subagent_proposed_transaction(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=2, wall_time_seconds=30))
    model = ScriptedModel(
        [
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        name="dispatch_subagents",
                        arguments={
                            "agents": [
                                {
                                    "id": "fixer",
                                    "role": "analyst",
                                    "instruction": (
                                        "Review this transaction before parent applies it."
                                    ),
                                    "allowed_paths": ["src/tiny_python_bug/**"],
                                    "max_steps": 1,
                                    "proposed_transaction": {
                                        "steps": [
                                            {
                                                "op": "read_file",
                                                "args": {
                                                    "path": "src/tiny_python_bug/calculator.py"
                                                },
                                            },
                                            {
                                                "op": "replace_text",
                                                "args": {
                                                    "path": "src/tiny_python_bug/calculator.py",
                                                    "old_text": "return left - right",
                                                    "new_text": "return left + right",
                                                },
                                            },
                                            {"op": "run_check", "args": {"name": "tests"}},
                                        ]
                                    },
                                }
                            ]
                        },
                    ),
                )
            ),
            ModelTurn(content="Transaction is scoped and should be applied."),
            ModelTurn(content="Parent done."),
        ]
    )

    result = await AgentRunner(
        task,
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    assert result.status == RunStatus.COMPLETED, result.model_dump()
    assert result.changed_files == ("src/tiny_python_bug/calculator.py",)
    patch = Path(result.artifacts["patch"]).read_text(encoding="utf-8")
    assert "return left + right" in patch
    parent_messages, _parent_tools = model.calls[2]
    observation = next(
        message
        for message in parent_messages
        if isinstance(message, ToolObservation) and message.name == "dispatch_subagents"
    )
    assert observation.ok is True
    assert "transaction: ok" in observation.content
    events = read_events(result)
    assert any(event["event_type"] == "subagents.transaction_started" for event in events)
    assert any(
        event["event_type"] == "subagents.transaction_completed" and event["data"]["ok"] is True
        for event in events
    )
    assert any(
        event["event_type"] == "tool.completed"
        and event["data"]["name"] == "tool_transaction"
        and event["data"]["ok"] is True
        for event in events
    )


@pytest.mark.asyncio
async def test_native_loop_rejects_unknown_subagent_dependency(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=2, wall_time_seconds=30))
    model = ScriptedModel(
        [
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        name="dispatch_subagents",
                        arguments={
                            "agents": [
                                {
                                    "id": "review",
                                    "role": "reviewer",
                                    "instruction": "Review missing analysis.",
                                    "depends_on": ["analysis"],
                                }
                            ]
                        },
                    ),
                )
            ),
            ModelTurn(content="Dependency rejected."),
        ]
    )

    result = await AgentRunner(
        task,
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    assert result.status == RunStatus.COMPLETED, result.model_dump()
    parent_messages, _parent_tools = model.calls[1]
    observation = next(
        message
        for message in parent_messages
        if isinstance(message, ToolObservation) and message.name == "dispatch_subagents"
    )
    assert observation.ok is False
    assert "depends on unknown id" in (observation.error or "")


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
    monkeypatch.setenv("LOOPLANE_MCP_ALLOWLIST", "local")
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


class InstructionChangingModel(SlowModel):
    def __init__(self, repository: Path) -> None:
        self.repository = repository
        self.calls: list[tuple[object, object]] = []

    async def complete(self, messages: object, tools: object = ()) -> ModelTurn:
        self.calls.append((messages, tools))
        if len(self.calls) == 1:
            (self.repository / "AGENTS.md").write_text(
                "Reloaded project guidance.",
                encoding="utf-8",
            )
            return ModelTurn(
                tool_calls=(
                    ToolCall(
                        name="read_file",
                        arguments={"path": "src/tiny_python_bug/calculator.py"},
                    ),
                )
            )
        return ModelTurn(content="No change needed.")


class SkillChangingModel(SlowModel):
    def __init__(self, repository: Path) -> None:
        self.repository = repository
        self.calls: list[tuple[object, object]] = []

    async def complete(self, messages: object, tools: object = ()) -> ModelTurn:
        self.calls.append((messages, tools))
        if len(self.calls) == 1:
            skills = self.repository / ".looplane" / "skills"
            skills.mkdir(parents=True, exist_ok=True)
            (skills / "review.md").write_text(
                "---\nname: reviewer\n---\nReview the changed code.",
                encoding="utf-8",
            )
            return ModelTurn(
                tool_calls=(
                    ToolCall(
                        name="read_file",
                        arguments={"path": "src/tiny_python_bug/calculator.py"},
                    ),
                )
            )
        return ModelTurn(content="No change needed.")


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
async def test_instruction_changes_are_reloaded_as_injected_context(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOOPLANE_USER_INSTRUCTIONS", str(tmp_path / "missing.md"))
    (tiny_bug_repo / "AGENTS.md").write_text("Initial project guidance.", encoding="utf-8")
    model = InstructionChangingModel(tiny_bug_repo)
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=2))

    result = await AgentRunner(
        task,
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    assert result.status is RunStatus.COMPLETED
    second_messages, _tools = model.calls[1]
    reloads = [
        message
        for message in second_messages
        if isinstance(message, InjectedContext) and message.source == "instruction_reload"
    ]
    assert len(reloads) == 1
    assert "Reloaded project guidance." in reloads[0].content
    events = read_events(result)
    assert any(event["event_type"] == "instructions.reloaded" for event in events)


@pytest.mark.asyncio
async def test_project_context_watch_reloads_skill_changes_as_injected_context(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOOPLANE_USER_INSTRUCTIONS", str(tmp_path / "missing.md"))
    model = SkillChangingModel(tiny_bug_repo)
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=2))

    result = await AgentRunner(
        task,
        model,
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
    ).run()

    assert result.status is RunStatus.COMPLETED
    second_messages, _tools = model.calls[1]
    reloads = [
        message
        for message in second_messages
        if isinstance(message, InjectedContext) and message.source == "project_context_reload"
    ]
    assert len(reloads) == 1
    assert "[project-context-reload-v1]" in reloads[0].content
    assert "- skills: .looplane/skills/review.md" in reloads[0].content
    assert "Review the changed code." in reloads[0].content
    events = read_events(result)
    assert any(
        event["event_type"] == "project_context.reloaded"
        and event["data"]["categories"] == ["skills"]
        for event in events
    )


@pytest.mark.asyncio
async def test_model_call_is_clamped_by_run_wall_time(tiny_bug_repo: Path, tmp_path: Path) -> None:
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
        # Leave enough active budget for workspace preparation so the command
        # itself starts and proves that execution remains wall-time clamped.
        limits=Limits(max_steps=3, wall_time_seconds=1.5),
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

    assert time.monotonic() - started < 3
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
                    ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH_AFTER_BROKEN}),
                )
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
            ModelTurn(tool_calls=(ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),)),
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
            VerificationCommand(name="check", argv=("git", "diff", "--check"), timeout_seconds=30),
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
            VerificationCommand(name="check", argv=("git", "diff", "--check"), timeout_seconds=30),
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
            VerificationCommand(name="check", argv=("git", "diff", "--check"), timeout_seconds=30),
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
        if isinstance(message, InjectedContext)
        and message.source == "context_pressure"
        and message.content
        and "b9-b1-context-pressure-v1" in message.content
    ]
    second_reminders = [
        message
        for message in second_call_messages
        if isinstance(message, InjectedContext)
        and message.source == "context_pressure"
        and message.content
        and "b9-b1-context-pressure-v1" in message.content
    ]
    third_reminders = [
        message
        for message in third_call_messages
        if isinstance(message, InjectedContext)
        and message.source == "context_pressure"
        and message.content
        and "b9-b1-context-pressure-v1" in message.content
    ]
    assert len(second_reminders) == 1
    assert len(third_reminders) == 1
    assert "85 of 100 allowed task tokens" in (second_reminders[0].content or "")
    events = read_events(result)
    assert (
        len(
            [
                event
                for event in events
                if event["event_type"] == "context_pressure.reminder_injected"
            ]
        )
        == 1
    )


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
            VerificationCommand(name="check", argv=("git", "diff", "--check"), timeout_seconds=30),
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
        if isinstance(message, InjectedContext)
        and message.source == "history_summary_fallback"
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
    assert (
        len(
            [
                event
                for event in events
                if event["event_type"] == "context_pressure.summary_fallback_applied"
            ]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_history_summary_fallback_runs_pre_and_post_compaction_hooks(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks_dir = tiny_bug_repo / ".looplane"
    hooks_dir.mkdir()
    hook_log = tmp_path / "compact-hooks.jsonl"
    hook = tmp_path / "compact_hook.py"
    hook.write_text(
        f"""
from __future__ import annotations

import json
import sys

payload = json.loads(sys.stdin.read())
with open({str(hook_log)!r}, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, sort_keys=True) + "\\n")
print("{{}}")
""".lstrip(),
        encoding="utf-8",
    )
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "pre_compact": [{"command": [sys.executable, str(hook)]}],
                "post_compact": [{"command": [sys.executable, str(hook)]}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LOOPLANE_ENABLE_PROJECT_HOOKS", "1")
    task = make_task(
        tiny_bug_repo,
        limits=Limits(max_steps=4, wall_time_seconds=60, max_total_tokens=100),
        verification=(
            VerificationCommand(name="check", argv=("git", "diff", "--check"), timeout_seconds=30),
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

    result = await AgentRunner(task, model, tmp_path / "runs", allow_unsafe_local_exec=True).run()

    assert result.status == RunStatus.COMPLETED
    hook_payloads = [json.loads(line) for line in hook_log.read_text(encoding="utf-8").splitlines()]
    assert [payload["event"] for payload in hook_payloads] == [
        "pre_compact",
        "post_compact",
    ]
    assert hook_payloads[0]["payload"]["compaction"]["kind"] == "history_summary_fallback"
    assert hook_payloads[1]["payload"]["summary"]["source"] == "history_summary_fallback"
    events = read_events(result)
    assert [
        event["data"]["hook_event"]
        for event in events
        if event["event_type"] == "hook.completed"
        and event["data"]["hook_event"] in {"pre_compact", "post_compact"}
    ] == ["pre_compact", "post_compact"]


@pytest.mark.asyncio
async def test_history_summary_fallback_pre_compaction_hook_can_deny(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks_dir = tiny_bug_repo / ".looplane"
    hooks_dir.mkdir()
    hook = tmp_path / "deny_compact.py"
    hook.write_text(
        """
import json

print(json.dumps({"decision": "deny", "reason": "keep full context"}))
""".lstrip(),
        encoding="utf-8",
    )
    (hooks_dir / "hooks.json").write_text(
        json.dumps({"pre_compact": [{"command": [sys.executable, str(hook)]}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LOOPLANE_ENABLE_PROJECT_HOOKS", "1")
    task = make_task(
        tiny_bug_repo,
        limits=Limits(max_steps=4, wall_time_seconds=60, max_total_tokens=100),
        verification=(
            VerificationCommand(name="check", argv=("git", "diff", "--check"), timeout_seconds=30),
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

    result = await AgentRunner(task, model, tmp_path / "runs", allow_unsafe_local_exec=True).run()

    assert result.status == RunStatus.COMPLETED
    fourth_call_messages, _tools = model.calls[3]
    assert not [
        message
        for message in fourth_call_messages
        if isinstance(message, InjectedContext) and message.source == "history_summary_fallback"
    ]
    events = read_events(result)
    assert any(
        event["event_type"] == "hook.denied" and event["data"]["hook_event"] == "pre_compact"
        for event in events
    )
    assert any(
        event["event_type"] == "context_pressure.summary_fallback_skipped"
        and event["data"]["reason"] == "keep full context"
        for event in events
    )


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
            VerificationCommand(name="check", argv=("git", "diff", "--check"), timeout_seconds=30),
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
        if isinstance(message, InjectedContext)
        and message.source == "workspace_context_reminder"
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
    assert (
        len(
            [
                event
                for event in events
                if event["event_type"] == "context_pressure.workspace_reminder_injected"
            ]
        )
        == 1
    )


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
    existing_reminder = InjectedContext(
        source="workspace_context_reminder",
        content=build_workspace_context_reminder(
            changed_files=("src/tiny_python_bug/calculator.py",),
            check_status=("tests: failed (exit 1)",),
            recent_paths=("src/tiny_python_bug/calculator.py",),
            constraints=("allowed_paths=src/**",),
        ).content
        or "",
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
                InjectedContext(
                    source="history_summary_fallback",
                    content="[b9-summary-fallback-v1]\nsummary",
                ),
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
        if isinstance(message, InjectedContext)
        and message.source == "workspace_context_reminder"
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
    retries = [event for event in read_events(result) if event["event_type"] == "model.retry"]
    assert [event["data"]["attempt"] for event in retries] == [1, 2]
    assert all(event["data"]["provider"] == "nvidia-nim" for event in retries)
    assert [event["data"]["delay_seconds"] for event in retries] == [0.0, 0.0]


@pytest.mark.asyncio
async def test_exhausted_retryable_provider_errors_fail_with_readable_error(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    task = _clean_check_task(tiny_bug_repo)
    model = ScriptedModel([_retryable_error(code) for code in (500, 503, 500, 502, 504)])
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
    primary = ScriptedModel([_retryable_error(500)] * 5, model_id="primary")
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
    assert Path(result.artifacts["review"]).read_text(encoding="utf-8") == ("Verdict: no findings.")
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
    assert not [event for event in read_events(result) if event["event_type"] == "model.retry"]


def test_retry_delay_uses_jitter_and_caps() -> None:
    from looplane.loop import (
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


@pytest.mark.asyncio
async def test_openai_no_choices_error_is_retried_until_success(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    """``OpenAICompatibleModel`` raises ``RETRYABLE`` when the response body is
    valid JSON but ``choices`` is empty/missing — a known failure mode for
    half-formed SSE trailers from OpenRouter and similar gateways. The retry
    layer must treat that as transient and recover on a later attempt."""
    task = _clean_check_task(tiny_bug_repo)
    no_choices_error = ProviderError(
        "openai-compatible response contained no choices",
        kind=ProviderErrorKind.RETRYABLE,
        provider_name="openai-compatible",
    )
    model = ScriptedModel(
        [
            no_choices_error,
            no_choices_error,
            ModelTurn(content="The repository is clean; no change is needed."),
        ]
    )
    runner = AgentRunner(task, model, tmp_path / "runs", allow_unsafe_local_exec=True)
    runner.model_retry_delay = lambda attempt, retry_after_seconds: 0.0

    result = await runner.run()

    assert result.status == RunStatus.COMPLETED, result.model_dump()
    assert len(model.calls) == 3
    retries = [event for event in read_events(result) if event["event_type"] == "model.retry"]
    assert [event["data"]["attempt"] for event in retries] == [1, 2]
    assert all(event["data"]["provider"] == "openai-compatible" for event in retries)
    assert all("no choices" in event["data"]["error"] for event in retries)


@pytest.mark.asyncio
async def test_openai_no_choices_exhaustion_fails_with_provider_retryable(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    """Sustained empty-choices responses must surface as a readable provider
    failure rather than getting masked by a different terminal_reason."""
    task = _clean_check_task(tiny_bug_repo)
    no_choices_error = ProviderError(
        "openai-compatible response contained no choices",
        kind=ProviderErrorKind.RETRYABLE,
        provider_name="openai-compatible",
    )
    model = ScriptedModel([no_choices_error] * 5)
    runner = AgentRunner(task, model, tmp_path / "runs", allow_unsafe_local_exec=True)
    runner.model_retry_delay = lambda attempt, retry_after_seconds: 0.0

    result = await runner.run()

    assert result.status == RunStatus.FAILED
    assert result.terminal_reason == "provider_retryable"
    assert len(model.calls) == 5
    # ``error`` summarises the retryable burst by status code; the original
    # "no choices" message is preserved on ``summary`` for the run transcript.
    assert result.error is not None
    assert "openai-compatible" in result.error
    assert "5 consecutive" in result.error
    assert "no choices" in result.summary
    events = read_events(result)
    retry_events = [e for e in events if e["event_type"] == "model.retry"]
    assert [e["data"]["attempt"] for e in retry_events] == [1, 2, 3, 4]
    failed = [e for e in events if e["event_type"] == "model.failed"]
    assert failed and failed[0]["data"]["retryable"] is True
