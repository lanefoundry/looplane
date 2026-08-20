from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from coding_agent.backends import (
    ExternalAgentBackend,
    ExternalAgentEvent,
    ExternalAgentResult,
    ExternalAgentTask,
    ExternalRunStatus,
)
from coding_agent.contracts import Limits, RunStatus, TaskContract, VerificationCommand
from coding_agent.external_runner import (
    ExternalCodingRunner,
    ExternalModificationApprovalError,
    UnsafeExternalVerificationError,
)


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


class StagingBackend(EditingBackend):
    async def run(self, task, *, working_directory=None, event_sink=None):
        assert working_directory is not None
        forbidden = working_directory / "forbidden.txt"
        forbidden.write_text("must not be hidden in the index\n", encoding="utf-8")
        git_dir = working_directory.parent / ".pca-git-metadata"
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
        config = working_directory.parent / ".pca-git-metadata" / "config"
        config.write_text(
            config.read_text(encoding="utf-8")
            + f"\n[core]\n\tfsmonitor = {os.fspath(hook)}\n",
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
        stat.S_IMODE(Path(path).stat().st_mode) == 0o600
        for path in result.artifacts.values()
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
