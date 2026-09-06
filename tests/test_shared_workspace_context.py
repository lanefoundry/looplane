from __future__ import annotations

import subprocess
import time

from looplane.shared_workspace_context import SharedWorkspaceContext


def _init_repo(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )


async def test_shared_context_captures_head_and_clean_status(tmp_path) -> None:
    _init_repo(tmp_path)
    ctx = await SharedWorkspaceContext.create(tmp_path)

    assert len(ctx.base_sha) == 40
    assert all(c in "0123456789abcdef" for c in ctx.base_sha)
    assert ctx.source_was_dirty is False
    assert ctx.source_snapshot_warning is None
    assert ctx.source_repository == tmp_path.resolve()
    assert ctx.version
    assert ctx.created_at > 0


async def test_shared_context_dirty_detection(tmp_path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "uncommitted.txt").write_text("dirty")

    ctx = await SharedWorkspaceContext.create(tmp_path)

    assert ctx.source_was_dirty is True
    assert ctx.source_snapshot_warning is not None
    assert "uncommitted changes" in ctx.source_snapshot_warning


async def test_shared_context_staleness(tmp_path) -> None:
    _init_repo(tmp_path)
    ctx = await SharedWorkspaceContext.create(tmp_path)

    assert ctx.is_stale(max_age_seconds=300.0) is False

    stale = SharedWorkspaceContext(
        source_repository=ctx.source_repository,
        base_sha=ctx.base_sha,
        source_was_dirty=ctx.source_was_dirty,
        source_snapshot_warning=ctx.source_snapshot_warning,
        version=ctx.version,
        created_at=time.monotonic() - 600,
    )
    assert stale.is_stale(max_age_seconds=300.0) is True


async def test_shared_context_refresh_produces_new_version(tmp_path) -> None:
    _init_repo(tmp_path)
    ctx1 = await SharedWorkspaceContext.create(tmp_path)
    ctx2 = await ctx1.refresh()

    assert ctx2.base_sha == ctx1.base_sha
    assert ctx2.source_was_dirty == ctx1.source_was_dirty
    assert ctx2.version != ctx1.version
    assert ctx2.created_at >= ctx1.created_at


async def test_shared_context_version_is_deterministic(tmp_path) -> None:
    _init_repo(tmp_path)
    ctx = await SharedWorkspaceContext.create(tmp_path)

    import hashlib

    expected = hashlib.sha256(
        f"{ctx.base_sha}:{ctx.source_was_dirty}:{ctx.created_at}".encode()
    ).hexdigest()
    assert ctx.version == expected
