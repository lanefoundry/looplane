"""Git worktree management for isolated agent execution."""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"^[a-zA-Z0-9._-]{1,64}$")
_WORKTREE_PREFIX = "worktree-agent-"
_STALE_CUTOFF_SECONDS = 30 * 24 * 3600  # 30 days


def validate_worktree_slug(slug: str) -> None:
    if not _SLUG_RE.match(slug):
        raise ValueError(f"worktree slug must match [a-zA-Z0-9._-]{{1,64}}, got: {slug!r}")


def create_agent_worktree(
    repo_root: Path,
    slug: str,
    *,
    base_ref: str = "HEAD",
) -> Path:
    """Create (or reuse) a git worktree for an agent run.

    Returns the absolute path to the worktree directory.
    """
    validate_worktree_slug(slug)
    branch_name = f"{_WORKTREE_PREFIX}{slug}"
    worktree_path = repo_root / ".worktrees" / slug

    if _is_existing_worktree(worktree_path):
        logger.info("reusing existing worktree: %s", worktree_path)
        return worktree_path

    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "-B",
            branch_name,
            str(worktree_path),
            base_ref,
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    logger.info("created worktree: %s (branch %s)", worktree_path, branch_name)
    return worktree_path


def has_worktree_changes(worktree_path: Path) -> bool:
    """Check if a worktree has uncommitted changes or new commits vs its base."""
    if not worktree_path.is_dir():
        return False
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        return True
    result = subprocess.run(
        ["git", "log", "--oneline", "HEAD...HEAD~1"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def remove_agent_worktree(repo_root: Path, slug: str) -> None:
    """Remove a worktree and its branch."""
    validate_worktree_slug(slug)
    worktree_path = repo_root / ".worktrees" / slug
    branch_name = f"{_WORKTREE_PREFIX}{slug}"

    if worktree_path.is_dir():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
    subprocess.run(
        ["git", "branch", "-D", branch_name],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    logger.info("removed worktree: %s", worktree_path)


def cleanup_stale_worktrees(repo_root: Path) -> int:
    """Remove agent worktrees older than the stale cutoff. Returns count removed."""
    worktrees_dir = repo_root / ".worktrees"
    if not worktrees_dir.is_dir():
        return 0
    now = time.time()
    removed = 0
    for entry in worktrees_dir.iterdir():
        if not entry.is_dir():
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age > _STALE_CUTOFF_SECONDS and not has_worktree_changes(entry):
            try:
                remove_agent_worktree(repo_root, entry.name)
                removed += 1
            except (subprocess.CalledProcessError, OSError):
                logger.warning("failed to clean up stale worktree: %s", entry)
    return removed


def _is_existing_worktree(path: Path) -> bool:
    """Fast check: a .git file (not directory) indicates a worktree."""
    git_path = path / ".git"
    return git_path.is_file()
