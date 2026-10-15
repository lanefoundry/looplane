from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from conftest import run_git

from looplane.contracts import VerificationCommand
from looplane.execution.local_process import run_local_process
from looplane.execution.types import CommandResult
from looplane.policy import SafePathPolicy
from looplane.tools import ToolExecutor
from looplane.workspace.base_anchor import BaseAnchorError, restore_base_anchor
from looplane.workspace.local_git import LocalGitWorkspace

_IDENTITY = ("-c", "user.name=Agent", "-c", "user.email=agent@example.invalid")


def _runner(workspace: Path):
    def git(argv: Sequence[str]) -> CommandResult:
        return run_local_process(
            ("git", *argv),
            cwd=workspace,
            timeout_seconds=10,
            max_output_chars=1_000_000,
        )

    return git


@pytest.fixture
def clone(tiny_bug_repo: Path, tmp_path: Path) -> tuple[Path, str]:
    sha = run_git(tiny_bug_repo, "rev-parse", "HEAD")
    workspace = LocalGitWorkspace(tiny_bug_repo, tmp_path / "run", sha).prepare()
    return workspace, sha


def _tracked_file(workspace: Path) -> str:
    return run_git(workspace, "ls-files").splitlines()[0]


def test_no_drift_is_a_no_op(clone: tuple[Path, str]) -> None:
    workspace, sha = clone
    (workspace / "scratch.txt").write_text("untracked\n")

    assert restore_base_anchor(_runner(workspace), sha, workspace) is None
    assert run_git(workspace, "rev-parse", "HEAD") == sha


def test_agent_commit_is_undone_without_losing_edits(clone: tuple[Path, str]) -> None:
    workspace, sha = clone
    tracked = _tracked_file(workspace)
    (workspace / tracked).write_text("edited\n")
    (workspace / "added.txt").write_text("new\n")
    run_git(workspace, "add", "-A")
    run_git(workspace, *_IDENTITY, "commit", "-qm", "agent commit")
    committed = run_git(workspace, "rev-parse", "HEAD")

    restored = restore_base_anchor(_runner(workspace), sha, workspace)

    assert restored is not None
    assert restored.previous_head == committed
    assert restored.head_moved and restored.index_restored
    assert "never exported" in restored.describe()
    assert run_git(workspace, "rev-parse", "HEAD") == sha
    assert run_git(workspace, "rev-parse", "--symbolic-full-name", "HEAD") == "HEAD"
    assert (workspace / tracked).read_text() == "edited\n"
    # New files stay registered with intent-to-add, so a plain `git diff` shows them.
    assert sorted(run_git(workspace, "diff", "--name-only").splitlines()) == sorted(
        ["added.txt", tracked]
    )
    assert restore_base_anchor(_runner(workspace), sha, workspace) is None


def test_branch_checkout_is_detached_back_to_base(clone: tuple[Path, str]) -> None:
    workspace, sha = clone
    run_git(workspace, "checkout", "-q", "-b", "feature")

    restored = restore_base_anchor(_runner(workspace), sha, workspace)

    assert restored is not None
    assert restored.previous_ref == "refs/heads/feature"
    assert not restored.index_restored
    assert run_git(workspace, "rev-parse", "--symbolic-full-name", "HEAD") == "HEAD"


def test_staged_changes_are_unstaged(clone: tuple[Path, str]) -> None:
    workspace, sha = clone
    tracked = _tracked_file(workspace)
    (workspace / tracked).write_text("staged\n")
    run_git(workspace, "add", tracked)

    restored = restore_base_anchor(_runner(workspace), sha, workspace)

    assert restored is not None
    assert not restored.head_moved and restored.index_restored
    assert run_git(workspace, "diff", "--name-only").splitlines() == [tracked]
    assert run_git(workspace, "diff", "--cached", "--name-only") == ""


def test_missing_base_commit_is_reported(clone: tuple[Path, str]) -> None:
    workspace, _ = clone
    run_git(workspace, "checkout", "-q", "-b", "feature")

    with pytest.raises(BaseAnchorError, match="missing"):
        restore_base_anchor(_runner(workspace), "f" * 40, workspace)


def test_executor_shell_reanchors_and_keeps_patch(tiny_bug_repo: Path, tmp_path: Path) -> None:
    sha = run_git(tiny_bug_repo, "rev-parse", "HEAD")
    workspace = LocalGitWorkspace(tiny_bug_repo, tmp_path / "run", sha)
    path = workspace.prepare()
    executor = ToolExecutor(
        workspace=workspace,
        policy=SafePathPolicy(path),
        verification_commands=(VerificationCommand(name="tests", argv=("true",)),),
    )
    tracked = _tracked_file(path)
    (path / tracked).write_text("edited\n")

    output = executor.shell(
        "git add -A && git -c user.name=A -c user.email=a@example.invalid commit -qm x"
    )

    assert isinstance(output, str)
    assert "restored to the base commit" in output
    assert run_git(path, "rev-parse", "HEAD") == sha
    patch = executor.reviewable_patch()
    assert patch.changed_paths == (tracked,)
    assert "+edited" in patch.content


def test_executor_patch_collection_reanchors_without_shell(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    sha = run_git(tiny_bug_repo, "rev-parse", "HEAD")
    workspace = LocalGitWorkspace(tiny_bug_repo, tmp_path / "run", sha)
    path = workspace.prepare()
    executor = ToolExecutor(
        workspace=workspace,
        policy=SafePathPolicy(path),
        verification_commands=(VerificationCommand(name="tests", argv=("true",)),),
    )
    tracked = _tracked_file(path)
    (path / tracked).write_text("edited\n")
    # e.g. a background job or verification command committed behind the agent's back
    run_git(path, "add", "-A")
    run_git(path, *_IDENTITY, "commit", "-qm", "x")

    patch = executor.reviewable_patch()

    assert patch.changed_paths == (tracked,)
    assert run_git(path, "rev-parse", "HEAD") == sha


def test_direct_edit_executor_never_reanchors(tiny_bug_repo: Path) -> None:
    executor = ToolExecutor(
        workspace=tiny_bug_repo,
        policy=SafePathPolicy(tiny_bug_repo),
        verification_commands=(VerificationCommand(name="tests", argv=("true",)),),
    )
    run_git(tiny_bug_repo, "checkout", "-q", "-b", "user-branch")

    output = executor.shell("true")

    assert "looplane" not in str(output)
    assert run_git(tiny_bug_repo, "rev-parse", "--abbrev-ref", "HEAD") == "user-branch"
