from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from looplane.execution.environment import sanitized_subprocess_env
from looplane.execution.local_process import run_local_process
from looplane.execution.types import CommandResult

_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


class WorkspacePreparationError(RuntimeError):
    """Raised when a disposable Git workspace cannot be prepared safely."""


@dataclass
class LocalGitWorkspace:
    source_repo: Path
    run_dir: Path
    base_sha: str
    workspace_name: str = "workspace"
    git_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        self.source_repo = Path(self.source_repo).resolve(strict=False)
        self.run_dir = Path(self.run_dir).resolve(strict=False)
        if not self.workspace_name or Path(self.workspace_name).name != self.workspace_name:
            raise ValueError("workspace_name must be one path segment")
        if not _FULL_SHA.fullmatch(self.base_sha):
            raise ValueError("base_sha must be a full 40-character Git commit SHA")

    @property
    def workspace_path(self) -> Path:
        return self.run_dir / self.workspace_name

    @staticmethod
    def _run_command(*args, **kwargs) -> CommandResult:
        return run_local_process(*args, **kwargs)

    def _git(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        return self._run_command(
            ("git", *argv),
            cwd=cwd,
            timeout_seconds=min(self.git_timeout_seconds, timeout_seconds)
            if timeout_seconds is not None
            else self.git_timeout_seconds,
            max_output_chars=20_000,
            env=sanitized_subprocess_env(task_home=self.run_dir / ".task-env"),
        )

    def prepare(self, *, timeout_seconds: float | None = None) -> Path:
        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None

        def remaining() -> float | None:
            if deadline is None:
                return None
            value = deadline - time.monotonic()
            if value <= 0:
                raise WorkspacePreparationError(
                    "workspace preparation exceeded the harness timeout"
                )
            return value

        source = self.source_repo.resolve(strict=True)
        if not source.is_dir():
            raise WorkspacePreparationError(f"source repository is not a directory: {source}")

        run_dir = self.run_dir.resolve(strict=False)
        try:
            run_dir.relative_to(source)
        except ValueError:
            pass
        else:
            raise WorkspacePreparationError("run_dir must not be inside the source repository")

        if self.workspace_path.exists():
            raise WorkspacePreparationError(f"workspace already exists: {self.workspace_path}")
        self.run_dir.mkdir(parents=True, exist_ok=True)

        resolved = self._git(
            ("-C", str(source), "rev-parse", "--verify", f"{self.base_sha}^{{commit}}"),
            cwd=self.run_dir,
            timeout_seconds=remaining(),
        )
        if not resolved.ok or resolved.stdout.strip().lower() != self.base_sha.lower():
            raise WorkspacePreparationError(
                "base_sha is not an exact commit in the source repository"
            )

        cloned = self._git(
            (
                "clone",
                "--no-hardlinks",
                "--no-checkout",
                "--",
                str(source),
                str(self.workspace_path),
            ),
            cwd=self.run_dir,
            timeout_seconds=remaining(),
        )
        if not cloned.ok:
            raise WorkspacePreparationError(f"git clone failed: {cloned.stderr.strip()}")

        checked_out = self._git(
            ("checkout", "--detach", self.base_sha),
            cwd=self.workspace_path,
            timeout_seconds=remaining(),
        )
        if not checked_out.ok:
            raise WorkspacePreparationError(f"git checkout failed: {checked_out.stderr.strip()}")
        head = self._git(
            ("rev-parse", "HEAD"),
            cwd=self.workspace_path,
            timeout_seconds=remaining(),
        )
        if not head.ok or head.stdout.strip().lower() != self.base_sha.lower():
            raise WorkspacePreparationError("disposable workspace HEAD does not match base_sha")
        return self.workspace_path
