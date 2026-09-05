from pathlib import Path

import pytest
from conftest import run_git

from looplane.workspace.local_git import LocalGitWorkspace, WorkspacePreparationError


def test_disposable_workspace_keeps_source_unchanged_and_produces_patch(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    source_sha = run_git(tiny_bug_repo, "rev-parse", "HEAD")
    source_status = run_git(tiny_bug_repo, "status", "--porcelain=v1")
    source_file = tiny_bug_repo / "src" / "tiny_python_bug" / "calculator.py"
    source_contents = source_file.read_bytes()
    runtime = LocalGitWorkspace(tiny_bug_repo, tmp_path / "run", source_sha)

    workspace = runtime.prepare()
    workspace_file = workspace / "src" / "tiny_python_bug" / "calculator.py"
    workspace_file.write_text(source_file.read_text().replace("left - right", "left + right"))
    patch = run_git(workspace, "diff", "--binary", "--no-ext-diff")

    assert workspace == runtime.workspace_path
    assert workspace.resolve() != tiny_bug_repo.resolve()
    assert "-    return left - right" in patch
    assert "+    return left + right" in patch
    assert run_git(tiny_bug_repo, "rev-parse", "HEAD") == source_sha
    assert run_git(tiny_bug_repo, "status", "--porcelain=v1") == source_status == ""
    assert source_file.read_bytes() == source_contents


def test_disposable_workspace_is_pinned_to_commit_not_dirty_source(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    source_sha = run_git(tiny_bug_repo, "rev-parse", "HEAD")
    source_file = tiny_bug_repo / "src" / "tiny_python_bug" / "calculator.py"
    committed_contents = source_file.read_text()
    dirty_contents = committed_contents.replace("left - right", "left * right")
    source_file.write_text(dirty_contents)

    workspace = LocalGitWorkspace(tiny_bug_repo, tmp_path / "run", source_sha).prepare()

    assert (workspace / "src" / "tiny_python_bug" / "calculator.py").read_text() == (
        committed_contents
    )
    assert source_file.read_text() == dirty_contents
    assert run_git(workspace, "rev-parse", "HEAD") == source_sha


def test_workspace_preparation_respects_explicit_timeout(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    source_sha = run_git(tiny_bug_repo, "rev-parse", "HEAD")
    runtime = LocalGitWorkspace(tiny_bug_repo, tmp_path / "run", source_sha)

    with pytest.raises(WorkspacePreparationError, match="preparation exceeded"):
        runtime.prepare(timeout_seconds=1e-12)
