import sys
import time
from pathlib import Path

import pytest
from conftest import run_git

from rivumi.runtime import (
    LocalGitWorkspace,
    WorkspacePreparationError,
    run_bounded_command,
)


def test_bounded_command_delivers_complete_stdout_lines_before_exit(tmp_path: Path) -> None:
    delivered: list[tuple[str, bool, float]] = []

    result = run_bounded_command(
        (
            sys.executable,
            "-c",
            "import time; print('ready', flush=True); time.sleep(0.4); print('done')",
        ),
        cwd=tmp_path,
        timeout_seconds=2,
        max_output_chars=1_000,
        stdout_line_callback=lambda line, truncated: delivered.append(
            (line, truncated, time.monotonic())
        ),
        max_stdout_line_bytes=64,
    )

    assert result.ok
    assert [(line, truncated) for line, truncated, _ in delivered] == [
        ("ready", False),
        ("done", False),
    ]
    assert delivered[1][2] - delivered[0][2] >= 0.3


def test_bounded_command_bounds_each_callback_line_without_losing_capture(
    tmp_path: Path,
) -> None:
    delivered: list[tuple[str, bool]] = []

    result = run_bounded_command(
        (sys.executable, "-c", "print('x' * 1000)"),
        cwd=tmp_path,
        timeout_seconds=2,
        max_output_chars=2_000,
        stdout_line_callback=lambda line, truncated: delivered.append((line, truncated)),
        max_stdout_line_bytes=32,
    )

    assert result.ok
    assert delivered == [("x" * 32, True)]
    assert result.stdout == "x" * 1000 + "\n"


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

    assert not runtime.workspace_path.exists()
