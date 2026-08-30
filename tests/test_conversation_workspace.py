from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from looplane.conversation_workspace import (
    ConversationWorkspace,
    ConversationWorkspaceIntegrityError,
)


def _git(repository: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *argv),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        },
    )


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "fixture@example.com")
    _git(repository, "config", "user.name", "Fixture")
    (repository / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (repository / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _git(repository, "add", ".gitignore", "tracked.txt")
    _git(repository, "commit", "-m", "initial")
    return repository


@pytest.mark.asyncio
async def test_dirty_source_creates_head_only_repo_recognizable_workspace(
    tmp_path: Path,
) -> None:
    source = _repository(tmp_path)
    (source / "tracked.txt").write_text("dirty source\n", encoding="utf-8")
    (source / "untracked.txt").write_text("not committed\n", encoding="utf-8")
    (source / "ignored.txt").write_text("also observed\n", encoding="utf-8")
    source_index_before = (source / ".git" / "index").read_bytes()
    source_status_before = _git(source, "status", "--porcelain=v1", "-z").stdout

    workspace = await ConversationWorkspace.create(source)
    try:
        assert workspace.source_was_dirty is True
        assert workspace.snapshot_strategy == "committed-head-only"
        assert "committed HEAD only" in (workspace.source_snapshot_warning or "")
        assert (workspace.workspace_path / "tracked.txt").read_text() == "committed\n"
        assert not (workspace.workspace_path / "untracked.txt").exists()
        assert not (workspace.workspace_path / "ignored.txt").exists()
        assert workspace.root_path.parent != source
        assert workspace.root_path not in source.parents
        assert (workspace.workspace_path / ".git").is_file()
        recognized = _git(workspace.workspace_path, "rev-parse", "--is-inside-work-tree")
        assert recognized.stdout.strip() == "true"
        assert _git(workspace.workspace_path, "remote").stdout == ""
        assert (source / ".git" / "index").read_bytes() == source_index_before
        assert _git(source, "status", "--porcelain=v1", "-z").stdout == source_status_before
    finally:
        await workspace.aclose()


@pytest.mark.asyncio
async def test_review_allows_unrelated_source_filesystem_mutation_including_ignored(
    tmp_path: Path,
) -> None:
    source = _repository(tmp_path)
    (source / "ignored.txt").write_text("initial ignored bytes\n", encoding="utf-8")
    workspace = await ConversationWorkspace.create(source)
    try:
        (source / "ignored.txt").write_text("mutated ignored bytes\n", encoding="utf-8")
        (workspace.workspace_path / "tracked.txt").write_text(
            "conversation edit\n", encoding="utf-8"
        )

        patch = await workspace.review()

        assert patch.changed_paths == ("tracked.txt",)
        assert "+conversation edit" in patch.content
        assert (source / "ignored.txt").read_text() == "mutated ignored bytes\n"
    finally:
        await workspace.aclose()


@pytest.mark.asyncio
async def test_source_git_config_hook_is_never_executed(tmp_path: Path) -> None:
    source = _repository(tmp_path)
    marker = tmp_path / "hook-ran"
    hook = tmp_path / "malicious-fsmonitor"
    hook.write_text(f"#!/bin/sh\nprintf ran > {marker}\n", encoding="utf-8")
    hook.chmod(0o700)
    _git(source, "config", "core.fsmonitor", str(hook))
    _git(source, "config", "core.hooksPath", str(tmp_path / "malicious-hooks"))

    workspace = await ConversationWorkspace.create(source)
    try:
        (workspace.workspace_path / "tracked.txt").write_text("review me\n", encoding="utf-8")
        patch = await workspace.review()

        assert patch.changed_paths == ("tracked.txt",)
        assert not marker.exists()
        hooks_path = _git(workspace.workspace_path, "config", "--local", "core.hooksPath")
        assert hooks_path.stdout.strip() == os.devnull
    finally:
        await workspace.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["stage", "head"])
async def test_review_rejects_staged_or_head_tampering(tmp_path: Path, tamper: str) -> None:
    source = _repository(tmp_path)
    workspace = await ConversationWorkspace.create(source)
    try:
        (workspace.workspace_path / "tracked.txt").write_text("changed\n", encoding="utf-8")
        if tamper == "stage":
            _git(workspace.workspace_path, "add", "tracked.txt")
        else:
            _git(
                workspace.workspace_path,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.com",
                "commit",
                "-am",
                "forbidden",
            )

        with pytest.raises(ConversationWorkspaceIntegrityError, match="Git control|HEAD"):
            await workspace.review()
    finally:
        await workspace.aclose()


@pytest.mark.asyncio
async def test_review_includes_new_files_without_mutating_real_index_and_cleanup(
    tmp_path: Path,
) -> None:
    source = _repository(tmp_path)
    workspace = await ConversationWorkspace.create(source)
    root = workspace.root_path
    (workspace.workspace_path / "new.txt").write_text("new content\n", encoding="utf-8")

    patch = await workspace.review()

    assert patch.changed_paths == ("new.txt",)
    assert "new file mode" in patch.content
    assert _git(workspace.workspace_path, "diff", "--cached", "--name-only").stdout == ""
    await workspace.aclose()
    await workspace.aclose()
    assert not root.exists()


@pytest.mark.asyncio
async def test_review_and_cleanup_allow_source_git_drift(tmp_path: Path) -> None:
    source = _repository(tmp_path)
    (source / "tracked.txt").write_text("dirty before snapshot\n", encoding="utf-8")
    workspace = await ConversationWorkspace.create(source)
    root = workspace.root_path
    _git(source, "add", "tracked.txt")
    (workspace.workspace_path / "tracked.txt").write_text("conversation edit\n", encoding="utf-8")

    patch = await workspace.review()
    await workspace.aclose()

    assert patch.changed_paths == ("tracked.txt",)
    assert not root.exists()
