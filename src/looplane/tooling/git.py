"""Bounded Git commands and isolated review/fingerprint indexes."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from looplane.execution.environment import sanitized_subprocess_env
from looplane.execution.local_process import run_local_process
from looplane.execution.types import CommandResult
from looplane.policy import SafePathPolicy
from looplane.tooling.filesystem import OutputLimits
from looplane.tooling.patch_validation import PatchLimits
from looplane.tooling.timeouts import effective_timeout
from looplane.tooling.types import ReviewablePatch, ToolExecutionError


class GitProcess(Protocol):
    """Callable process seam used for one bounded Git invocation."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        max_output_chars: int,
        env: Mapping[str, str],
        stdin: str | None = None,
    ) -> CommandResult: ...


class TaskEnvironment(Protocol):
    def __call__(self, *, task_home: Path | None = None) -> dict[str, str]: ...


class GitCommands(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
        max_output_bytes: int | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


class PatchReview(Protocol):
    def reviewable_patch(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> ReviewablePatch: ...


class IndexReset(Protocol):
    def reset_paths(
        self,
        paths: Sequence[str],
        *,
        timeout_seconds: float = 5.0,
    ) -> CommandResult: ...


class WorkspaceGit:
    """Git evidence owner sharing the caller's live output and patch limits.

    Bound methods also satisfy the existing Slice 2.2 callable seams; integration
    can supply run, reviewable_patch and reset_paths without executor callbacks.
    """

    def __init__(
        self,
        *,
        policy: SafePathPolicy,
        output_limits: OutputLimits,
        patch_limits: PatchLimits,
        task_home: Path,
        git_dir: Path | None = None,
        base_sha: str | None = None,
        preexisting_dirty_paths: frozenset[str] = frozenset(),
        run_command: GitProcess = run_local_process,
        environment: TaskEnvironment = sanitized_subprocess_env,
        clock: Callable[[], float] = time.monotonic,
        new_id: Callable[[], str] | None = None,
    ) -> None:
        self.policy = policy
        self.workspace = policy.workspace_root
        self.output_limits = output_limits
        self.patch_limits = patch_limits
        self.task_home = Path(task_home).resolve(strict=False)
        self.git_dir = Path(git_dir).resolve(strict=True) if git_dir is not None else None
        if self.git_dir is not None and not self.git_dir.is_dir():
            raise ValueError("git_dir must be an existing directory")
        self.base_sha = base_sha
        self.preexisting_dirty_paths = frozenset(preexisting_dirty_paths)
        self.run_command = run_command
        self.environment = environment
        self.clock = clock
        self.new_id = new_id or (lambda: uuid4().hex)

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
        max_output_bytes: int | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        prefix: tuple[str, ...] = ()
        if self.git_dir is not None:
            prefix = (
                f"--git-dir={self.git_dir}",
                f"--work-tree={self.workspace}",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
            )
        env = self.environment(task_home=self.task_home)
        if extra_env:
            env.update(extra_env)
        return self.run_command(
            ("git", *prefix, *argv),
            cwd=self.workspace,
            timeout_seconds=effective_timeout(30.0, timeout_seconds),
            max_output_chars=max_output_bytes or self.output_limits.max_output_chars,
            env=env,
            stdin=stdin,
        )

    def reset_paths(
        self,
        paths: Sequence[str],
        *,
        timeout_seconds: float = 5.0,
    ) -> CommandResult:
        """Reset the supplied paths; the snapshot owner handles the result."""

        return self.run(
            ("reset", "--quiet", "HEAD", "--", *sorted(paths)),
            timeout_seconds=timeout_seconds,
        )

    def reviewable_patch(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> ReviewablePatch:
        if self.base_sha is not None:
            return self._reviewable_patch_pinned(timeout_seconds=timeout_seconds)
        budget = effective_timeout(30.0, timeout_seconds)
        deadline = self.clock() + budget

        def remaining() -> float:
            value = deadline - self.clock()
            if value <= 0:
                raise ToolExecutionError("reviewable_patch exceeded the harness timeout")
            return value

        result = self.run(
            (
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--no-renames",
                "--",
            ),
            timeout_seconds=remaining(),
            max_output_bytes=self.patch_limits.max_patch_bytes + 1,
        )
        if not result.ok:
            raise ToolExecutionError(f"git diff failed: {result.stderr.strip()}")
        if result.stdout_bytes > self.patch_limits.max_patch_bytes or result.stdout_truncated:
            raise ToolExecutionError(
                f"final patch exceeds {self.patch_limits.max_patch_bytes} bytes; "
                "refusing truncated artifact"
            )
        if len(result.stdout.splitlines()) > self.patch_limits.max_patch_lines:
            raise ToolExecutionError(
                f"final patch exceeds {self.patch_limits.max_patch_lines} lines"
            )

        names = self.run(
            ("diff", "--name-only", "--no-renames", "-z", "--"),
            timeout_seconds=remaining(),
            max_output_bytes=self.output_limits.max_output_chars,
        )
        if not names.ok:
            raise ToolExecutionError(f"git diff --name-only failed: {names.stderr.strip()}")
        if names.stdout_truncated:
            raise ToolExecutionError("changed path list exceeded the tool output limit")
        changed_paths = tuple(sorted(path for path in names.stdout.split("\x00") if path))
        if len(changed_paths) > self.patch_limits.max_changed_files:
            raise ToolExecutionError(
                f"final patch exceeds {self.patch_limits.max_changed_files} changed files"
            )
        for path in changed_paths:
            self.policy.resolve(path)
        return ReviewablePatch(content=result.stdout, changed_paths=changed_paths)

    def workspace_fingerprint(self, *, timeout_seconds: float | None = None) -> str:
        """Capture tracked and non-ignored untracked state in a temporary index."""

        budget = effective_timeout(30.0, timeout_seconds)
        deadline = self.clock() + budget

        def remaining() -> float:
            value = deadline - self.clock()
            if value <= 0:
                raise ToolExecutionError("workspace fingerprint exceeded the harness timeout")
            return value

        git_dir_result = self.run(("rev-parse", "--git-dir"), timeout_seconds=remaining())
        if not git_dir_result.ok:
            raise ToolExecutionError(f"could not resolve git dir: {git_dir_result.stderr.strip()}")
        git_dir = Path(git_dir_result.stdout.strip())
        if not git_dir.is_absolute():
            git_dir = (self.workspace / git_dir).resolve(strict=True)
        fingerprint_index = git_dir / f"looplane-fingerprint-index-{self.new_id()}"
        extra_env = {"GIT_INDEX_FILE": str(fingerprint_index)}
        try:
            read_tree = self.run(
                ("read-tree", "HEAD"),
                timeout_seconds=remaining(),
                extra_env=extra_env,
            )
            if not read_tree.ok:
                raise ToolExecutionError(
                    "could not initialize the workspace fingerprint index: "
                    + read_tree.stderr.strip()
                )
            added = self.run(
                ("add", "-A", "--", "."),
                timeout_seconds=remaining(),
                extra_env=extra_env,
                max_output_bytes=20_000,
            )
            if not added.ok:
                raise ToolExecutionError(
                    f"could not fingerprint workspace changes: {added.stderr.strip()}"
                )
            tree = self.run(
                ("write-tree",),
                timeout_seconds=remaining(),
                extra_env=extra_env,
            )
            fingerprint = tree.stdout.strip()
            if not tree.ok or not fingerprint:
                raise ToolExecutionError(
                    f"could not write workspace fingerprint: {tree.stderr.strip()}"
                )
            return fingerprint
        finally:
            fingerprint_index.unlink(missing_ok=True)

    def _reviewable_patch_pinned(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> ReviewablePatch:
        """Review against base_sha, excluding whole preexisting dirty paths."""

        assert self.base_sha is not None
        budget = effective_timeout(30.0, timeout_seconds)
        deadline = self.clock() + budget

        def remaining() -> float:
            value = deadline - self.clock()
            if value <= 0:
                raise ToolExecutionError("reviewable_patch exceeded the harness timeout")
            return value

        git_dir_result = self.run(("rev-parse", "--git-dir"), timeout_seconds=remaining())
        if not git_dir_result.ok:
            raise ToolExecutionError(f"could not resolve git dir: {git_dir_result.stderr.strip()}")
        git_dir = Path(git_dir_result.stdout.strip())
        if not git_dir.is_absolute():
            git_dir = (self.workspace / git_dir).resolve(strict=True)
        review_index = git_dir / f"looplane-review-index-{self.new_id()}"
        extra_env = {"GIT_INDEX_FILE": str(review_index)}
        try:
            read_tree = self.run(
                ("read-tree", self.base_sha),
                timeout_seconds=remaining(),
                extra_env=extra_env,
            )
            if not read_tree.ok:
                raise ToolExecutionError(
                    "could not initialize the isolated review index: " + read_tree.stderr.strip()
                )
            added = self.run(
                ("add", "-A", "-f", "--", "."),
                timeout_seconds=remaining(),
                extra_env=extra_env,
                max_output_bytes=20_000,
            )
            if not added.ok:
                raise ToolExecutionError(
                    f"could not index workspace changes: {added.stderr.strip()}"
                )

            names = self.run(
                ("diff", "--cached", "--name-only", "--no-renames", "-z", self.base_sha, "--"),
                timeout_seconds=remaining(),
                extra_env=extra_env,
                max_output_bytes=self.output_limits.max_output_chars,
            )
            if not names.ok:
                raise ToolExecutionError(f"git diff --name-only failed: {names.stderr.strip()}")
            if names.stdout_truncated:
                raise ToolExecutionError("changed path list exceeded the tool output limit")
            all_changed_paths = tuple(sorted(path for path in names.stdout.split("\x00") if path))
            # Exclusion applies before path checks and final diff generation.
            changed_paths = tuple(
                path for path in all_changed_paths if path not in self.preexisting_dirty_paths
            )
            if len(changed_paths) > self.patch_limits.max_changed_files:
                raise ToolExecutionError(
                    f"final patch exceeds {self.patch_limits.max_changed_files} changed files"
                )
            for path in changed_paths:
                self.policy.resolve(path)
            if not changed_paths:
                return ReviewablePatch(content="", changed_paths=())

            result = self.run(
                (
                    "diff",
                    "--cached",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-color",
                    "--no-renames",
                    self.base_sha,
                    "--",
                    *changed_paths,
                ),
                timeout_seconds=remaining(),
                extra_env=extra_env,
                max_output_bytes=self.patch_limits.max_patch_bytes + 1,
            )
            if not result.ok:
                raise ToolExecutionError(f"git diff failed: {result.stderr.strip()}")
            if result.stdout_bytes > self.patch_limits.max_patch_bytes or result.stdout_truncated:
                raise ToolExecutionError(
                    f"final patch exceeds {self.patch_limits.max_patch_bytes} bytes; "
                    "refusing truncated artifact"
                )
            if len(result.stdout.splitlines()) > self.patch_limits.max_patch_lines:
                raise ToolExecutionError(
                    f"final patch exceeds {self.patch_limits.max_patch_lines} lines"
                )
            return ReviewablePatch(content=result.stdout, changed_paths=changed_paths)
        finally:
            review_index.unlink(missing_ok=True)

    def git_diff(self, *, timeout_seconds: float | None = None) -> str:
        return self.reviewable_patch(timeout_seconds=timeout_seconds).content
