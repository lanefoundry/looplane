"""Disposable clones are reviewed against their base commit, not HEAD or the index."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import run_git

from looplane.contracts import VerificationCommand
from looplane.policy import SafePathPolicy
from looplane.tools import ToolExecutor
from looplane.workspace.local_git import LocalGitWorkspace

_COMMIT = "git -c user.name=A -c user.email=a@example.invalid commit -qm agent"


@pytest.fixture
def clone(tiny_bug_repo: Path, tmp_path: Path) -> tuple[ToolExecutor, Path, str]:
    sha = run_git(tiny_bug_repo, "rev-parse", "HEAD")
    workspace = LocalGitWorkspace(tiny_bug_repo, tmp_path / "run", sha)
    path = workspace.prepare()
    executor = ToolExecutor(
        workspace=workspace,
        policy=SafePathPolicy(path),
        verification_commands=(VerificationCommand(name="tests", argv=("true",)),),
    )
    return executor, path, sha


def _tracked_file(workspace: Path) -> str:
    return run_git(workspace, "ls-files").splitlines()[0]


def test_agent_commit_does_not_hide_changes(clone: tuple[ToolExecutor, Path, str]) -> None:
    executor, path, sha = clone
    tracked = _tracked_file(path)
    (path / tracked).write_text("edited\n")
    executor.create_file("added.txt", "new\n")

    executor.shell(f"git add -A && {_COMMIT}")

    head = run_git(path, "rev-parse", "HEAD")
    assert head != sha  # the harness leaves the agent's Git state alone
    patch = executor.reviewable_patch()
    assert patch.changed_paths == tuple(sorted(("added.txt", tracked)))
    assert "+edited" in patch.content and "+new" in patch.content
    assert run_git(path, "rev-parse", "HEAD") == head


def test_staged_and_branch_changes_are_reviewed(clone: tuple[ToolExecutor, Path, str]) -> None:
    executor, path, _ = clone
    tracked = _tracked_file(path)
    executor.shell("git checkout -q -b feature")
    (path / tracked).write_text("staged\n")
    executor.shell(f"git add {tracked}")

    patch = executor.reviewable_patch()

    assert patch.changed_paths == (tracked,)
    assert "+staged" in patch.content


def test_committed_deletion_is_reviewed(clone: tuple[ToolExecutor, Path, str]) -> None:
    executor, path, _ = clone
    tracked = _tracked_file(path)
    executor.shell(f"git rm -q {tracked} && {_COMMIT}")

    patch = executor.reviewable_patch()

    assert patch.changed_paths == (tracked,)
    assert "deleted file mode" in patch.content


def test_unregistered_untracked_files_stay_out_of_patch(
    clone: tuple[ToolExecutor, Path, str],
) -> None:
    executor, path, _ = clone
    (path / "scratch.txt").write_text("scratch\n")

    assert executor.reviewable_patch().changed_paths == ()
