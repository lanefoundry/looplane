from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1] / "evals" / "fixtures" / "tiny-python-bug"
)


def run_git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def tiny_bug_repo(tmp_path: Path) -> Path:
    repository = tmp_path / "tiny-python-bug"
    shutil.copytree(
        FIXTURE_ROOT,
        repository,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
    )
    run_git(repository, "init", "-q")
    run_git(repository, "config", "user.name", "Fixture Author")
    run_git(repository, "config", "user.email", "fixture@example.invalid")
    run_git(repository, "add", ".")
    run_git(repository, "commit", "-q", "-m", "fixture: add tiny calculator bug")
    return repository
