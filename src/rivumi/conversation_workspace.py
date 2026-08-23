"""Long-lived, disposable Git workspaces for unified coding conversations.

A conversation always starts from exact committed ``HEAD``. Dirty source files
are reported but deliberately are not copied into the disposable clone. Once
created, the clone is independent of concurrent changes in the source worktree.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path

from rivumi.policy import PathPolicyError, SafePathPolicy
from rivumi.runtime import (
    LocalGitWorkspace,
    WorkspacePreparationError,
    run_bounded_command,
    sanitized_subprocess_env,
)
from rivumi.tools import ReviewablePatch, ToolExecutionError


class ConversationWorkspaceIntegrityError(RuntimeError):
    """Raised when the disposable workspace violates its audit boundary."""


class ConversationWorkspace:
    """A pinned clone that persists for the lifetime of one conversation.

    Use :meth:`create` instead of constructing this class directly.  The
    workspace contains committed ``HEAD`` only.  ``source_was_dirty`` and
    ``source_snapshot_warning`` make that boundary explicit to callers.
    """

    snapshot_strategy = "committed-head-only"

    def __init__(
        self,
        *,
        source_repository: Path,
        workspace_path: Path,
        root_path: Path,
        git_dir: Path,
        base_sha: str,
        source_was_dirty: bool,
        git_control: dict[str, str | None],
        git_pointer_digest: str,
        timeout_seconds: float,
        max_patch_bytes: int,
        max_patch_lines: int,
        max_changed_files: int,
    ) -> None:
        self.source_repository = source_repository
        self.workspace_path = workspace_path
        self.root_path = root_path
        self.git_dir = git_dir
        self.base_sha = base_sha
        self.source_was_dirty = source_was_dirty
        self.source_snapshot_warning = (
            "The source repository had uncommitted changes when this session started. "
            "The disposable workspace contains committed HEAD only; staged, unstaged, "
            "and untracked source changes are not included."
            if source_was_dirty
            else None
        )
        self._git_control = git_control
        self._git_pointer_digest = git_pointer_digest
        self._timeout_seconds = timeout_seconds
        self._max_patch_bytes = max_patch_bytes
        self._max_patch_lines = max_patch_lines
        self._max_changed_files = max_changed_files
        self._closed = False

    @classmethod
    async def create(
        cls,
        source_repository: str | Path,
        *,
        timeout_seconds: float = 60.0,
        max_patch_bytes: int = 2_000_000,
        max_patch_lines: int = 50_000,
        max_changed_files: int = 1_000,
    ) -> ConversationWorkspace:
        """Create an exact, detached ``HEAD`` clone of the source repository."""

        for name, value in (
            ("timeout_seconds", timeout_seconds),
            ("max_patch_bytes", max_patch_bytes),
            ("max_patch_lines", max_patch_lines),
            ("max_changed_files", max_changed_files),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")

        return await asyncio.to_thread(
            cls._create_sync,
            Path(source_repository),
            float(timeout_seconds),
            int(max_patch_bytes),
            int(max_patch_lines),
            int(max_changed_files),
        )

    @classmethod
    def _create_sync(
        cls,
        source_repository: Path,
        timeout_seconds: float,
        max_patch_bytes: int,
        max_patch_lines: int,
        max_changed_files: int,
    ) -> ConversationWorkspace:
        deadline = time.monotonic() + timeout_seconds
        source = source_repository.resolve(strict=True)
        if not source.is_dir():
            raise WorkspacePreparationError(f"source repository is not a directory: {source}")

        root_result = cls._source_git(source, ("rev-parse", "--show-toplevel"), deadline)
        if not root_result.ok or Path(root_result.stdout.strip()).resolve() != source:
            raise WorkspacePreparationError("source_repository must be the Git worktree root")
        head = cls._source_git(source, ("rev-parse", "--verify", "HEAD^{commit}"), deadline)
        base_sha = head.stdout.strip().lower()
        if not head.ok or len(base_sha) != 40:
            raise WorkspacePreparationError("could not resolve a full source HEAD commit")
        status_result = cls._source_git(
            source,
            ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
            deadline,
            max_output_chars=2_000_000,
        )
        if not status_result.ok or status_result.stdout_truncated:
            raise WorkspacePreparationError("could not inspect source repository status")

        root = Path(tempfile.mkdtemp(prefix="rivumi-conversation-")).resolve()
        try:
            root.relative_to(source)
        except ValueError:
            pass
        else:
            shutil.rmtree(root, ignore_errors=True)
            if source.parent == source:
                raise WorkspacePreparationError(
                    "could not place conversation workspace outside source repository"
                )
            root = Path(
                tempfile.mkdtemp(prefix="rivumi-conversation-", dir=source.parent)
            ).resolve()
        try:
            workspace = LocalGitWorkspace(source, root, base_sha).prepare(
                timeout_seconds=cls._remaining(deadline)
            )
            git_dir = root / ".rivumi-git-metadata"
            source_clone_git = workspace / ".git"
            if source_clone_git.is_symlink() or not source_clone_git.is_dir():
                raise WorkspacePreparationError("disposable clone has unsafe Git metadata")
            os.replace(source_clone_git, git_dir)
            env = sanitized_subprocess_env(task_home=root / ".task-env")
            for argv in (
                ("remote", "remove", "origin"),
                ("config", "--local", "core.hooksPath", os.devnull),
                ("config", "--local", "core.fsmonitor", "false"),
            ):
                result = cls._workspace_git(workspace, git_dir, argv, deadline, env=env)
                if not result.ok:
                    raise WorkspacePreparationError(
                        "could not secure disposable Git metadata: " + result.stderr.strip()
                    )

            pointer = f"gitdir: {git_dir}\n"
            pointer_path = workspace / ".git"
            pointer_path.write_text(pointer, encoding="utf-8")
            pointer_digest = hashlib.sha256(pointer.encode()).hexdigest()
            control = cls._git_control_snapshot(git_dir, deadline=deadline)
            instance = cls(
                source_repository=source,
                workspace_path=workspace,
                root_path=root,
                git_dir=git_dir,
                base_sha=base_sha,
                source_was_dirty=bool(status_result.stdout),
                git_control=control,
                git_pointer_digest=pointer_digest,
                timeout_seconds=timeout_seconds,
                max_patch_bytes=max_patch_bytes,
                max_patch_lines=max_patch_lines,
                max_changed_files=max_changed_files,
            )
            instance._assert_workspace_git_intact(deadline)
            return instance
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise

    async def review(
        self,
        *,
        allowed_paths: tuple[str, ...] = ("**",),
        timeout_seconds: float | None = None,
    ) -> ReviewablePatch:
        """Return a bounded patch after validating disposable Git control state."""

        self._assert_open()
        budget = self._budget(timeout_seconds)
        return await asyncio.to_thread(self._review_sync, allowed_paths, budget)

    def _review_sync(
        self, allowed_paths: tuple[str, ...], timeout_seconds: float
    ) -> ReviewablePatch:
        deadline = time.monotonic() + timeout_seconds
        self._assert_workspace_git_intact(deadline)
        policy = SafePathPolicy(self.workspace_path, allowed_paths)

        review_index = self.root_path / ".review-index"
        review_index.unlink(missing_ok=True)
        env = sanitized_subprocess_env(task_home=self.root_path / ".task-env")
        env["GIT_INDEX_FILE"] = str(review_index)
        try:
            read_tree = self._workspace_git(
                self.workspace_path,
                self.git_dir,
                ("read-tree", self.base_sha),
                deadline,
                env=env,
            )
            if not read_tree.ok:
                raise ToolExecutionError("could not initialize the isolated review index")
            added = self._workspace_git(
                self.workspace_path,
                self.git_dir,
                ("add", "-A", "-f", "--", "."),
                deadline,
                env=env,
                max_output_chars=20_000,
            )
            if not added.ok:
                raise ToolExecutionError(
                    f"could not index workspace changes: {added.stderr.strip()}"
                )
            names = self._workspace_git(
                self.workspace_path,
                self.git_dir,
                ("diff", "--cached", "--name-only", "--no-renames", "-z", self.base_sha, "--"),
                deadline,
                env=env,
                max_output_chars=2_000_000,
            )
            if not names.ok or names.stdout_truncated:
                raise ToolExecutionError("could not obtain a complete changed path list")
            changed_paths = tuple(sorted(path for path in names.stdout.split("\x00") if path))
            if len(changed_paths) > self._max_changed_files:
                raise ToolExecutionError(
                    f"final patch exceeds {self._max_changed_files} changed files"
                )
            for relative in changed_paths:
                try:
                    target = policy.resolve(relative)
                except PathPolicyError as exc:
                    raise ToolExecutionError(str(exc)) from exc
                if target.exists() or target.is_symlink():
                    mode = target.lstat().st_mode
                    if stat.S_ISREG(mode):
                        continue
                    raise ToolExecutionError(
                        f"conversation patch leaves a non-regular file: {relative}"
                    )
            patch = self._workspace_git(
                self.workspace_path,
                self.git_dir,
                (
                    "diff",
                    "--cached",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-color",
                    "--no-renames",
                    "--binary",
                    self.base_sha,
                    "--",
                ),
                deadline,
                env=env,
                max_output_chars=self._max_patch_bytes + 1,
            )
            if not patch.ok:
                raise ToolExecutionError(f"git diff failed: {patch.stderr.strip()}")
            if patch.stdout_truncated or patch.stdout_bytes > self._max_patch_bytes:
                raise ToolExecutionError(f"final patch exceeds {self._max_patch_bytes} bytes")
            if len(patch.stdout.splitlines()) > self._max_patch_lines:
                raise ToolExecutionError(f"final patch exceeds {self._max_patch_lines} lines")
            return ReviewablePatch(content=patch.stdout, changed_paths=changed_paths)
        finally:
            review_index.unlink(missing_ok=True)

    async def aclose(self) -> None:
        """Delete the entire disposable workspace; repeated calls are safe."""

        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(shutil.rmtree, self.root_path, True)

    async def __aenter__(self) -> ConversationWorkspace:
        self._assert_open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    def _assert_workspace_git_intact(self, deadline: float) -> None:
        pointer_path = self.workspace_path / ".git"
        if pointer_path.is_symlink() or not pointer_path.is_file():
            raise ConversationWorkspaceIntegrityError("workspace Git pointer was replaced")
        if hashlib.sha256(pointer_path.read_bytes()).hexdigest() != self._git_pointer_digest:
            raise ConversationWorkspaceIntegrityError("workspace Git pointer was modified")
        if self._git_control_snapshot(self.git_dir, deadline=deadline) != self._git_control:
            raise ConversationWorkspaceIntegrityError("workspace Git control state was modified")
        head = self._workspace_git(
            self.workspace_path,
            self.git_dir,
            ("rev-parse", "HEAD"),
            deadline,
            env=sanitized_subprocess_env(task_home=self.root_path / ".task-env"),
        )
        if not head.ok or head.stdout.strip().lower() != self.base_sha:
            raise ConversationWorkspaceIntegrityError("workspace HEAD no longer matches base_sha")

    @classmethod
    def _git_control_snapshot(
        cls, git_dir: Path, *, deadline: float | None = None
    ) -> dict[str, str | None]:
        snapshot: dict[str, str | None] = {}
        controls = [
            "HEAD",
            "config",
            "index",
            "packed-refs",
            "shallow",
            "commondir",
            "info/attributes",
            "info/exclude",
            "objects/info/alternates",
        ]
        refs = git_dir / "refs"
        if refs.exists():
            if refs.is_symlink() or not refs.is_dir():
                raise WorkspacePreparationError("unsafe Git refs directory")
            controls.extend(
                path.relative_to(git_dir).as_posix()
                for path in sorted(refs.rglob("*"))
                if path.is_file() or path.is_symlink()
            )
        for relative in controls:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Git control snapshot exceeded its wall-time budget")
            path = git_dir / relative
            if not path.exists():
                snapshot[relative] = None
            elif path.is_symlink() or not path.is_file():
                raise WorkspacePreparationError(f"unsafe Git control file: {relative}")
            else:
                digest = hashlib.sha256()
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(path, flags)
                try:
                    with os.fdopen(descriptor, "rb", closefd=False) as handle:
                        while chunk := handle.read(1024 * 1024):
                            digest.update(chunk)
                            if deadline is not None and time.monotonic() >= deadline:
                                raise TimeoutError(
                                    "Git control snapshot exceeded its wall-time budget"
                                )
                finally:
                    os.close(descriptor)
                snapshot[relative] = digest.hexdigest()
        return snapshot

    @classmethod
    def _source_git(
        cls,
        source: Path,
        argv: tuple[str, ...],
        deadline: float,
        *,
        max_output_chars: int = 20_000,
    ):
        env = sanitized_subprocess_env()
        env["GIT_OPTIONAL_LOCKS"] = "0"
        return run_bounded_command(
            (
                "git",
                "--no-optional-locks",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                *argv,
            ),
            cwd=source,
            timeout_seconds=min(30.0, cls._remaining(deadline)),
            max_output_chars=max_output_chars,
            env=env,
        )

    @classmethod
    def _workspace_git(
        cls,
        workspace: Path,
        git_dir: Path,
        argv: tuple[str, ...],
        deadline: float,
        *,
        env: dict[str, str],
        max_output_chars: int = 20_000,
    ):
        return run_bounded_command(
            (
                "git",
                f"--git-dir={git_dir}",
                f"--work-tree={workspace}",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                *argv,
            ),
            cwd=workspace,
            timeout_seconds=min(30.0, cls._remaining(deadline)),
            max_output_chars=max_output_chars,
            env=env,
        )

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("conversation workspace exceeded its wall-time budget")
        return remaining

    def _budget(self, timeout_seconds: float | None) -> float:
        budget = self._timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        if budget <= 0:
            raise ValueError("timeout_seconds must be positive")
        return budget

    def _assert_open(self) -> None:
        if self._closed:
            raise RuntimeError("conversation workspace is closed")
