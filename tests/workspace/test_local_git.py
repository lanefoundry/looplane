from pathlib import Path

import pytest
from conftest import run_git

from looplane import runtime
from looplane.execution.types import CommandResult
from looplane.workspace.local_git import LocalGitWorkspace, WorkspacePreparationError


@pytest.mark.parametrize("workspace_type", [LocalGitWorkspace, runtime.LocalGitWorkspace])
def test_workspace_rejects_nested_destination_and_existing_workspace(
    tiny_bug_repo,
    tmp_path,
    workspace_type,
):
    sha = run_git(tiny_bug_repo, "rev-parse", "HEAD")
    with pytest.raises(WorkspacePreparationError, match="inside the source"):
        workspace_type(tiny_bug_repo, tiny_bug_repo / "run", sha).prepare()
    run_dir = tmp_path / "existing"
    (run_dir / "workspace").mkdir(parents=True)
    with pytest.raises(WorkspacePreparationError, match="already exists"):
        workspace_type(tiny_bug_repo, run_dir, sha).prepare()


def test_facade_git_calls_keep_legacy_runner_patch(tmp_path, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return CommandResult(tuple(argv), 0, "", "")

    monkeypatch.setattr(runtime, "run_bounded_command", run)
    workspace = runtime.LocalGitWorkspace(tmp_path, tmp_path / "run", "a" * 40)
    assert workspace._git(("status",), cwd=tmp_path, timeout_seconds=0.25).ok
    assert calls[0][0] == ("git", "status")
    assert calls[0][1]["timeout_seconds"] == 0.25
    assert Path(calls[0][1]["env"]["TMPDIR"]).is_dir()
