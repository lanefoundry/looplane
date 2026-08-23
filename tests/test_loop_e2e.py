from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from conftest import run_git

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
    VerificationCommand,
)
from rivumi.loop import AgentRunner
from rivumi.models import ScriptedModel

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
    task = make_task(tiny_bug_repo, limits=Limits(max_steps=1, wall_time_seconds=30))
    runner = AgentRunner(
        task,
        ScriptedModel([ModelTurn(content="Done without fixing anything.")]),
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
        limits=Limits(max_steps=2, wall_time_seconds=0.6),
        verification=(command,),
    )
    started = time.monotonic()

    result = await AgentRunner(
        task,
        ScriptedModel([ModelTurn(content="Verify now.")]),
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
            ModelTurn(content="I think it is already fixed."),
            ModelTurn(
                tool_calls=(ToolCall(name="apply_patch", arguments={"patch": FIX_PATCH}),)
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
        for item in model.calls[1][0]
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
