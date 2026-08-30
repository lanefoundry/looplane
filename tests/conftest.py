from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep looplane's disk caches and saved config out of the real user state
    so tests neither pollute it nor read entries from it."""

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LOOPLANE_CONFIG", str(tmp_path / "config" / "config.json"))
    monkeypatch.delenv("PCA_CONFIG", raising=False)


@pytest.fixture(autouse=True)
def _force_tty_width(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin terminal width so typer/click help rendering matches across
    local dev, CI runners, and bare ``CliRunner`` invocations. GitHub
    Actions runners expose a wide default terminal width that pushes
    typer 0.26's help formatter into a boxed layout where option names
    get hidden inside border padding, so assertions like ``--api-url in
    result.output`` silently fail even though the option is present.
    typer reads ``TERMINAL_WIDTH`` (not ``COLUMNS``) for its ``MAX_WIDTH``
    when computing rich table layouts, so we have to set the typer-native
    env var, not the click/termcap one.
    """

    monkeypatch.setenv("TERMINAL_WIDTH", "80")
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "40")
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "evals" / "fixtures" / "tiny-python-bug"


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
