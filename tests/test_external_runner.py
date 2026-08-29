from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from rivumi.approvals import ApprovalDecision
from rivumi.backends import (
    ExternalAgentBackend,
    ExternalAgentEvent,
    ExternalAgentResult,
    ExternalAgentTask,
    ExternalRunStatus,
)
from rivumi.contracts import Limits, RunStatus, TaskContract, VerificationCommand
from rivumi.external_runner import (
    ExternalCodingRunner,
    ExternalModificationApprovalError,
    UnsafeExternalVerificationError,
    external_failure_hint,
)
from rivumi.runtime import WorkspacePreparationError


class EditingBackend(ExternalAgentBackend):
    backend_name = "fixture-agent"
    local_only = True
    experimental = True

    def __init__(self, *, changed_path: str = "src/tiny_python_bug/calculator.py") -> None:
        self.changed_path = changed_path
        self.saw_origin = True

    async def run(
        self,
        task: ExternalAgentTask,
        *,
        working_directory: Path | None = None,
        event_sink=None,
    ) -> ExternalAgentResult:
        assert working_directory is not None
        self.saw_origin = (working_directory / ".git").exists()
        target = working_directory / self.changed_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.write_text(
                target.read_text(encoding="utf-8").replace("left - right", "left + right"),
                encoding="utf-8",
            )
        else:
            target.write_text("outside policy\n", encoding="utf-8")
        event = ExternalAgentEvent(
            sequence=0,
            event_type="result",
            text="fixed calculator",
            data={"source": "fixture"},
        )
        if event_sink is not None:
            await event_sink.emit(event)
        return ExternalAgentResult(
            backend_name=self.backend_name,
            task_id=task.task_id,
            status=ExternalRunStatus.COMPLETED,
            summary="fixed calculator",
            events=(event,),
            terminal_reason="completed",
            exit_code=0,
        )


class SecretWritingBackend(EditingBackend):
    async def run(self, task, *, working_directory=None, event_sink=None):
        assert working_directory is not None
        target = working_directory / "src/tiny_python_bug/calculator.py"
        target.write_text(
            target.read_text(encoding="utf-8")
            + '\nAPI_KEY = "sk-test_abcdefghijklmnopqrstuvwxyz123456"\n',
            encoding="utf-8",
        )
        return ExternalAgentResult(
            backend_name=self.backend_name,
            task_id=task.task_id,
            status=ExternalRunStatus.COMPLETED,
            summary="added config",
            events=(),
            terminal_reason="completed",
            exit_code=0,
        )


class StagingBackend(EditingBackend):
    async def run(self, task, *, working_directory=None, event_sink=None):
        assert working_directory is not None
        forbidden = working_directory / "forbidden.txt"
        forbidden.write_text("must not be hidden in the index\n", encoding="utf-8")
        git_dir = working_directory.parent / ".rivumi-git-metadata"
        subprocess.run(
            [
                "git",
                f"--git-dir={git_dir}",
                f"--work-tree={working_directory}",
                "add",
                "forbidden.txt",
            ],
            check=True,
        )
        return await super().run(
            task,
            working_directory=working_directory,
            event_sink=event_sink,
        )


class GitConfigTamperingBackend(EditingBackend):
    def __init__(self, marker: Path) -> None:
        super().__init__()
        self.marker = marker

    async def run(self, task, *, working_directory=None, event_sink=None):
        assert working_directory is not None
        hook = working_directory.parent / "fsmonitor-hook"
        hook.write_text(
            f"#!/bin/sh\nprintf triggered > {self.marker}\n",
            encoding="utf-8",
        )
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
        config = working_directory.parent / ".rivumi-git-metadata" / "config"
        config.write_text(
            config.read_text(encoding="utf-8") + f"\n[core]\n\tfsmonitor = {os.fspath(hook)}\n",
            encoding="utf-8",
        )
        return await super().run(
            task,
            working_directory=working_directory,
            event_sink=event_sink,
        )


class SourceTamperingBackend(EditingBackend):
    def __init__(
        self,
        source_repository: Path,
        source_relative_path: str = "src/tiny_python_bug/calculator.py",
    ) -> None:
        super().__init__()
        self.source_repository = source_repository
        self.source_relative_path = source_relative_path

    async def run(self, task, *, working_directory=None, event_sink=None):
        source_file = self.source_repository / self.source_relative_path
        source_file.write_text("source was modified\n", encoding="utf-8")
        return await super().run(
            task,
            working_directory=working_directory,
            event_sink=event_sink,
        )


class MissingExecutableBackend(EditingBackend):
    async def run(self, task, *, working_directory=None, event_sink=None):
        return ExternalAgentResult(
            backend_name=self.backend_name,
            task_id=task.task_id,
            status=ExternalRunStatus.FAILED,
            terminal_reason="executable_unavailable",
        )


def _task(repository: Path, *, allowed_paths=("src/**",)) -> TaskContract:
    return TaskContract(
        repository=repository,
        instruction="Fix the calculator addition bug.",
        allowed_paths=allowed_paths,
        verification=(
            VerificationCommand(name="tests", argv=("pytest", "-q"), timeout_seconds=30),
        ),
        limits=Limits(wall_time_seconds=60),
        task_id="external-fixture",
    )


@pytest.mark.asyncio
async def test_external_runner_verifies_patch_and_preserves_source(
    tmp_path: Path, tiny_bug_repo: Path
) -> None:
    original = (tiny_bug_repo / "src/tiny_python_bug/calculator.py").read_bytes()
    backend = EditingBackend()
    runner = ExternalCodingRunner(
        _task(tiny_bug_repo),
        backend,
        tmp_path / "runs",
        allow_external_modify=True,
        allow_unsafe_local_exec=True,
    )

    result = await runner.run()

    assert result.status is RunStatus.COMPLETED
    assert result.terminal_reason == "verified"
    assert result.changed_files == ("src/tiny_python_bug/calculator.py",)
    assert result.verification[0].ok is True
    assert backend.saw_origin is False
    assert (tiny_bug_repo / "src/tiny_python_bug/calculator.py").read_bytes() == original
    patch = Path(result.artifacts["patch"]).read_text(encoding="utf-8")
    assert "-    return left - right" in patch
    assert "+    return left + right" in patch
    assert all(Path(path).exists() for path in result.artifacts.values())
    assert all(
        stat.S_IMODE(Path(path).stat().st_mode) == 0o600 for path in result.artifacts.values()
    )


@pytest.mark.asyncio
async def test_external_runner_rejects_changed_path_outside_policy(
    tmp_path: Path, tiny_bug_repo: Path
) -> None:
    runner = ExternalCodingRunner(
        _task(tiny_bug_repo),
        EditingBackend(changed_path="README.md"),
        tmp_path / "runs",
        allow_external_modify=True,
        allow_unsafe_local_exec=True,
    )

    result = await runner.run()

    assert result.status is RunStatus.FAILED
    assert result.terminal_reason == "policy_or_artifact_error"
    assert result.verification == ()


@pytest.mark.asyncio
async def test_external_runner_rejects_patch_that_adds_secret_material(
    tmp_path: Path, tiny_bug_repo: Path
) -> None:
    runner = ExternalCodingRunner(
        _task(tiny_bug_repo),
        SecretWritingBackend(),
        tmp_path / "runs",
        allow_external_modify=True,
        allow_unsafe_local_exec=True,
    )

    result = await runner.run()

    assert result.status is RunStatus.FAILED
    assert result.terminal_reason == "policy_or_artifact_error"
    assert "secret material" in result.summary
    assert "src/tiny_python_bug/calculator.py" in result.summary
    assert "sk-test" not in result.summary
    assert result.verification == ()


@pytest.mark.asyncio
async def test_external_runner_requires_local_exec_acknowledgement(
    tmp_path: Path, tiny_bug_repo: Path
) -> None:
    runner = ExternalCodingRunner(
        _task(tiny_bug_repo),
        EditingBackend(),
        tmp_path / "runs",
        allow_external_modify=True,
    )

    with pytest.raises(UnsafeExternalVerificationError):
        await runner.run()


@pytest.mark.asyncio
async def test_external_runner_requires_modify_approval(
    tmp_path: Path, tiny_bug_repo: Path
) -> None:
    runner = ExternalCodingRunner(
        _task(tiny_bug_repo),
        EditingBackend(),
        tmp_path / "runs",
        allow_unsafe_local_exec=True,
    )

    with pytest.raises(ExternalModificationApprovalError):
        await runner.run()


@pytest.mark.asyncio
async def test_dirty_source_is_rejected_before_external_modify_approval(
    tmp_path: Path, tiny_bug_repo: Path
) -> None:
    (tiny_bug_repo / "src/tiny_python_bug/calculator.py").write_text(
        "dirty source\n", encoding="utf-8"
    )

    class PoisonPolicy:
        async def decide(self, _request):
            raise AssertionError("dirty repositories must fail before asking permission")

    runner = ExternalCodingRunner(
        _task(tiny_bug_repo),
        EditingBackend(),
        tmp_path / "runs",
        approval_policy=PoisonPolicy(),
        allow_unsafe_local_exec=True,
    )

    with pytest.raises(
        WorkspacePreparationError,
        match="clean source repository; commit or stash changes first",
    ):
        await runner.run()
    assert not runner.run_dir.exists()


@pytest.mark.asyncio
async def test_external_runner_emits_activity_immediately_after_approval(
    tmp_path: Path, tiny_bug_repo: Path
) -> None:
    class AllowPolicy:
        async def decide(self, _request):
            return ApprovalDecision.ALLOW_ONCE

    class RecordingSink:
        def __init__(self) -> None:
            self.events: list[ExternalAgentEvent] = []

        async def emit(self, event: ExternalAgentEvent) -> None:
            self.events.append(event)

    sink = RecordingSink()
    runner = ExternalCodingRunner(
        _task(tiny_bug_repo),
        EditingBackend(),
        tmp_path / "runs",
        approval_policy=AllowPolicy(),
        event_sink=sink,
        allow_unsafe_local_exec=True,
    )

    result = await runner.run()

    assert result.status is RunStatus.COMPLETED
    assert sink.events[0].text == ("fixture-agent approved; preparing an isolated clone…")


@pytest.mark.asyncio
async def test_external_runner_stop_after_approval_skips_workspace_preparation(
    tmp_path: Path, tiny_bug_repo: Path
) -> None:
    class AllowPolicy:
        async def decide(self, _request):
            return ApprovalDecision.ALLOW_ONCE

    runner: ExternalCodingRunner

    class CancellingSink:
        async def emit(self, _event: ExternalAgentEvent) -> None:
            runner.request_cancel()

    runner = ExternalCodingRunner(
        _task(tiny_bug_repo),
        EditingBackend(),
        tmp_path / "runs",
        approval_policy=AllowPolicy(),
        event_sink=CancellingSink(),
        allow_unsafe_local_exec=True,
    )
    result = await runner.run()

    assert result.status is RunStatus.CANCELLED
    assert result.terminal_reason == "user_cancelled"
    assert not runner.run_dir.exists()


@pytest.mark.asyncio
async def test_external_runner_rejects_hidden_staged_change(
    tmp_path: Path, tiny_bug_repo: Path
) -> None:
    runner = ExternalCodingRunner(
        _task(tiny_bug_repo, allowed_paths=("**",)),
        StagingBackend(),
        tmp_path / "runs",
        allow_external_modify=True,
        allow_unsafe_local_exec=True,
    )

    result = await runner.run()

    assert result.status is RunStatus.FAILED
    assert result.terminal_reason == "policy_or_artifact_error"
    assert "Git control state changed" in result.summary
    assert result.verification == ()


@pytest.mark.asyncio
async def test_external_runner_rejects_git_config_before_hook_can_execute(
    tmp_path: Path, tiny_bug_repo: Path
) -> None:
    marker = tmp_path / "hook-ran"
    runner = ExternalCodingRunner(
        _task(tiny_bug_repo),
        GitConfigTamperingBackend(marker),
        tmp_path / "runs",
        allow_external_modify=True,
        allow_unsafe_local_exec=True,
    )

    result = await runner.run()

    assert result.status is RunStatus.FAILED
    assert "Git control state changed" in result.summary
    assert not marker.exists()


@pytest.mark.asyncio
async def test_external_runner_never_accepts_a_changed_source_repository(
    tmp_path: Path, tiny_bug_repo: Path
) -> None:
    runner = ExternalCodingRunner(
        _task(tiny_bug_repo),
        SourceTamperingBackend(tiny_bug_repo),
        tmp_path / "runs",
        allow_external_modify=True,
        allow_unsafe_local_exec=True,
    )

    result = await runner.run()

    assert result.status is RunStatus.FAILED
    assert result.terminal_reason == "source_repository_changed"
    assert result.verification == ()


@pytest.mark.asyncio
async def test_external_runner_detects_ignored_source_file_changes(
    tmp_path: Path, tiny_bug_repo: Path
) -> None:
    (tiny_bug_repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=tiny_bug_repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "ignore local env"],
        cwd=tiny_bug_repo,
        check=True,
    )
    ignored = tiny_bug_repo / ".env"
    ignored.write_text("before\n", encoding="utf-8")
    runner = ExternalCodingRunner(
        _task(tiny_bug_repo),
        SourceTamperingBackend(tiny_bug_repo, ".env"),
        tmp_path / "runs",
        allow_external_modify=True,
        allow_unsafe_local_exec=True,
    )

    result = await runner.run()

    assert result.status is RunStatus.FAILED
    assert result.terminal_reason == "source_repository_changed"
    assert ignored.read_text(encoding="utf-8") == "source was modified\n"


def test_external_failure_hint_mapping() -> None:
    assert external_failure_hint("executable_unavailable", "opencode") == (
        "The opencode executable was not found on your PATH. Install opencode (or add it to "
        "PATH) and retry."
    )
    assert "authenticated" in external_failure_hint("external_agent_error", "pi")
    assert external_failure_hint("verified", "pi") is None
    assert external_failure_hint("user_cancelled", "pi") is None
    assert external_failure_hint("policy_or_artifact_error", "pi") is None


@pytest.mark.asyncio
async def test_external_runner_surfaces_actionable_failure_hint(
    tmp_path: Path, tiny_bug_repo: Path
) -> None:
    runner = ExternalCodingRunner(
        _task(tiny_bug_repo),
        MissingExecutableBackend(),
        tmp_path / "runs",
        allow_external_modify=True,
        allow_unsafe_local_exec=True,
    )

    result = await runner.run()

    assert result.status is RunStatus.FAILED
    assert result.terminal_reason == "executable_unavailable"
    assert result.error is not None
    assert "executable was not found" in result.error
    assert "fixture-agent" in result.error
