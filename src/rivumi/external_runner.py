"""Rivumi-owned harness for coding delegated to an external agent CLI.

The external runtime may edit only a disposable Git clone. Rivumi then independently
validates the patch boundary and executes the exact configured verification commands.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
import time
from contextlib import suppress
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from rivumi.approvals import (
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalReason,
    ApprovalRequest,
    ToolEffect,
)
from rivumi.backends import (
    ExternalAgentBackend,
    ExternalAgentEvent,
    ExternalAgentResult,
    ExternalAgentTask,
    ExternalEventSink,
)
from rivumi.contracts import RunResult, RunStatus, TaskContract, ToolCall, Usage
from rivumi.events import atomic_write_json
from rivumi.policy import SafePathPolicy
from rivumi.runtime import (
    LocalGitWorkspace,
    WorkspacePreparationError,
    run_bounded_command,
    sanitized_subprocess_env,
)
from rivumi.tools import ToolExecutionError, ToolExecutor

EXTERNAL_FAILURE_HINTS: dict[str, str] = {
    "executable_unavailable": (
        "The {name} executable was not found on your PATH. Install {name} (or add it to "
        "PATH) and retry."
    ),
    "timeout": (
        "{name} exceeded its time budget. Raise the wall-time limit or narrow the task, then retry."
    ),
    "output_limit_exceeded": (
        "{name} produced more output than Rivumi can capture; the stream was truncated. Narrow the "
        "task scope, then retry."
    ),
    "malformed_event_stream": (
        "{name} returned output Rivumi could not parse. Check your {name} version and that it "
        "supports JSON streaming, then retry."
    ),
    "external_agent_error": (
        "{name} exited with an error. Confirm {name} is authenticated and its model/provider is "
        "reachable, then retry."
    ),
}


def external_failure_hint(terminal_reason: str, backend_name: str) -> str | None:
    """Actionable guidance for a failed external run, or ``None`` when not a mapped failure.

    The hint is best-effort: it points the user at the installed CLI's prerequisites
    (present on PATH, authenticated, reachable) without asserting a specific root cause such
    as an auth error unless the backend itself reported one.
    """
    template = EXTERNAL_FAILURE_HINTS.get(terminal_reason)
    if template is None:
        return None
    return template.format(name=backend_name)


class UnsafeExternalVerificationError(RuntimeError):
    """Raised when host-local verification was not explicitly acknowledged."""


class ExternalModificationApprovalError(RuntimeError):
    """Raised when a delegated agent was not explicitly allowed to edit its clone."""


class _ExternalRunCancelled(RuntimeError):
    """Internal cooperative-stop signal finalized as a cancelled RunResult."""


class ExternalCodingRunner:
    """Run an external coding agent in a pinned clone and audit its result."""

    #: Budget for the post-delegation source-integrity audit.  This is a fast,
    #: local ``git``/filesystem inspection and must not be starved by the
    #: backend's wall-clock budget; otherwise a backend timeout would make this
    #: check time out and misreport the terminal reason as
    #: ``source_repository_changed``.
    _SOURCE_INVARIANT_TIMEOUT = 30.0

    def __init__(
        self,
        task: TaskContract,
        backend: ExternalAgentBackend,
        run_root: str | Path,
        *,
        run_id: str | None = None,
        allow_external_modify: bool = False,
        allow_unsafe_local_exec: bool = False,
        approval_policy: ApprovalPolicy | None = None,
        event_sink: ExternalEventSink | None = None,
        durable_artifacts: bool = True,
    ) -> None:
        self.task = task
        self.backend = backend
        self.run_root = Path(run_root).resolve(strict=False)
        self.run_id = run_id or uuid4().hex
        run_path = Path(self.run_id)
        windows_path = PureWindowsPath(self.run_id)
        if (
            not self.run_id
            or "\x00" in self.run_id
            or self.run_id in {".", ".."}
            or run_path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or run_path.name != self.run_id
        ):
            raise ValueError("run_id must be one safe relative path segment")
        self.run_dir = self.run_root / self.run_id
        self.allow_external_modify = allow_external_modify
        self.allow_unsafe_local_exec = allow_unsafe_local_exec
        self.approval_policy = approval_policy
        self.event_sink = event_sink
        self.durable_artifacts = durable_artifacts
        self._cancel_requested = asyncio.Event()

    def request_cancel(self) -> None:
        """Request a cooperative stop without abandoning child cleanup or artifact finalization."""

        self._cancel_requested.set()

    def _raise_if_cancelled(self) -> None:
        if self._cancel_requested.is_set():
            raise _ExternalRunCancelled

    async def _require_approval(
        self, request: ApprovalRequest, *, explicitly_allowed: bool
    ) -> None:
        if explicitly_allowed:
            return
        if self.approval_policy is None:
            if request.effect == ToolEffect.MODIFY:
                raise ExternalModificationApprovalError(
                    "external coding backend requires explicit approval to modify its "
                    "disposable clone"
                )
            raise UnsafeExternalVerificationError(
                "final verification executes repository code on the host; pass the explicit "
                "unsafe-local-exec acknowledgement only for a trusted repository"
            )
        decision = await self.approval_policy.decide(request)
        if decision in {ApprovalDecision.ALLOW_ONCE, ApprovalDecision.ALLOW_SESSION}:
            return
        if decision == ApprovalDecision.CANCEL:
            raise _ExternalRunCancelled
        if request.effect == ToolEffect.MODIFY:
            raise ExternalModificationApprovalError("external modification approval was denied")
        raise UnsafeExternalVerificationError("external verification approval was denied")

    async def _run_backend(
        self,
        task: ExternalAgentTask,
        workspace: Path,
        timeout_seconds: float,
    ) -> ExternalAgentResult:
        backend_task = asyncio.create_task(
            self.backend.run(
                task,
                working_directory=workspace,
                event_sink=self.event_sink,
            )
        )
        cancel_task = asyncio.create_task(self._cancel_requested.wait())
        try:
            try:
                done, _ = await asyncio.wait(
                    {backend_task, cancel_task},
                    timeout=timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                backend_task.cancel()
                with suppress(asyncio.CancelledError):
                    await backend_task
                raise
            if not done:
                backend_task.cancel()
                with suppress(asyncio.CancelledError):
                    await backend_task
                raise TimeoutError
            if cancel_task in done:
                backend_task.cancel()
                with suppress(asyncio.CancelledError):
                    await backend_task
                raise _ExternalRunCancelled
            return await backend_task
        finally:
            cancel_task.cancel()
            with suppress(asyncio.CancelledError):
                await cancel_task

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("external coding run exceeded its wall-time budget")
        return remaining

    def _validate_location(self) -> None:
        source = self.task.repository.resolve(strict=True)
        candidate = self.run_dir.resolve(strict=False)
        try:
            candidate.relative_to(source)
        except ValueError:
            return
        raise WorkspacePreparationError("run directory must not be inside source repository")

    def _resolve_base_sha(self, deadline: float) -> str:
        if self.task.base_sha is not None:
            return self.task.base_sha
        result = run_bounded_command(
            ("git", "rev-parse", "HEAD"),
            cwd=self.task.repository.resolve(strict=True),
            timeout_seconds=min(30.0, self._remaining(deadline)),
            max_output_chars=2_000,
            env=sanitized_subprocess_env(),
        )
        sha = result.stdout.strip()
        if not result.ok or len(sha) != 40:
            raise WorkspacePreparationError("could not resolve source repository HEAD")
        return sha

    @staticmethod
    def _source_git(
        source: Path,
        argv: tuple[str, ...],
        deadline_remaining: float,
        *,
        max_output_chars: int = 2_000_000,
    ):
        return run_bounded_command(
            (
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                *argv,
            ),
            cwd=source,
            timeout_seconds=min(30.0, deadline_remaining),
            max_output_chars=max_output_chars,
            env=sanitized_subprocess_env(),
        )

    @classmethod
    def _assert_clean_source(
        cls,
        source: Path,
        deadline_remaining: float,
    ) -> None:
        status = cls._source_git(
            source,
            ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
            deadline_remaining,
        )
        if not status.ok or status.stdout_truncated:
            raise WorkspacePreparationError("could not inspect source repository status")
        if status.stdout:
            raise WorkspacePreparationError(
                "external coding requires a clean source repository; commit or stash changes first"
            )

    @classmethod
    def _capture_source_invariant(
        cls,
        source: Path,
        base_sha: str,
        deadline_remaining: float,
    ) -> dict[str, object]:
        cls._assert_clean_source(source, deadline_remaining)
        files = cls._filesystem_snapshot(
            source,
            deadline=time.monotonic() + deadline_remaining,
        )
        git_dir_result = cls._source_git(
            source,
            ("rev-parse", "--absolute-git-dir"),
            deadline_remaining,
        )
        if not git_dir_result.ok:
            raise WorkspacePreparationError("could not resolve source Git metadata")
        git_dir = Path(git_dir_result.stdout.strip()).resolve(strict=True)
        return {
            "base_sha": base_sha,
            "files": files,
            "git_dir": git_dir,
            "git_control": cls._git_control_snapshot(git_dir),
        }

    @staticmethod
    def _filesystem_snapshot(
        source: Path,
        *,
        deadline: float,
    ) -> dict[str, tuple[str, int, str]]:
        """Hash every source entry except Git internals without following symlinks."""

        snapshot: dict[str, tuple[str, int, str]] = {}
        for current, directories, filenames in os.walk(source, followlinks=False):
            if time.monotonic() >= deadline:
                raise TimeoutError("source repository snapshot exceeded wall-time budget")
            current_path = Path(current)
            relative_current = current_path.relative_to(source)
            if relative_current == Path("."):
                directories[:] = [name for name in directories if name != ".git"]
            for name in tuple(sorted(directories)):
                target = current_path / name
                relative = target.relative_to(source).as_posix()
                metadata = target.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    snapshot[relative] = (
                        "symlink",
                        stat.S_IMODE(metadata.st_mode),
                        hashlib.sha256(os.readlink(target).encode()).hexdigest(),
                    )
                    directories.remove(name)
                else:
                    snapshot[relative] = (
                        "directory",
                        stat.S_IMODE(metadata.st_mode),
                        "",
                    )
            for name in sorted(filenames):
                if time.monotonic() >= deadline:
                    raise TimeoutError("source repository snapshot exceeded wall-time budget")
                target = current_path / name
                relative = target.relative_to(source).as_posix()
                metadata = target.lstat()
                mode = stat.S_IMODE(metadata.st_mode)
                if stat.S_ISREG(metadata.st_mode):
                    digest = hashlib.sha256()
                    with target.open("rb") as handle:
                        while chunk := handle.read(1024 * 1024):
                            digest.update(chunk)
                            if time.monotonic() >= deadline:
                                raise TimeoutError(
                                    "source repository snapshot exceeded wall-time budget"
                                )
                    snapshot[relative] = ("file", mode, digest.hexdigest())
                elif stat.S_ISLNK(metadata.st_mode):
                    snapshot[relative] = (
                        "symlink",
                        mode,
                        hashlib.sha256(os.readlink(target).encode()).hexdigest(),
                    )
                else:
                    snapshot[relative] = ("other", mode, "")
        return snapshot

    @classmethod
    def _source_invariant_matches(
        cls,
        source: Path,
        invariant: dict[str, object],
        deadline_remaining: float,
    ) -> bool:
        git_dir = invariant["git_dir"]
        if not isinstance(git_dir, Path):
            return False
        try:
            if cls._git_control_snapshot(git_dir) != invariant["git_control"]:
                return False
            expected_files = invariant["files"]
            if not isinstance(expected_files, dict):
                return False
            actual_files = cls._filesystem_snapshot(
                source,
                deadline=time.monotonic() + deadline_remaining,
            )
            if actual_files != expected_files:
                return False
        except (OSError, WorkspacePreparationError):
            return False
        head = cls._source_git(source, ("rev-parse", "HEAD"), deadline_remaining)
        status = cls._source_git(
            source,
            ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
            deadline_remaining,
        )
        return (
            head.ok
            and head.stdout.strip() == invariant["base_sha"]
            and status.ok
            and not status.stdout_truncated
            and not status.stdout
        )

    @staticmethod
    def _isolate_git_metadata(
        workspace: Path,
        run_dir: Path,
        deadline_remaining: float,
    ) -> Path:
        source_git_dir = workspace / ".git"
        git_dir = run_dir / ".rivumi-git-metadata"
        if source_git_dir.is_symlink() or not source_git_dir.is_dir() or git_dir.exists():
            raise WorkspacePreparationError("disposable clone has unsafe Git metadata")
        os.replace(source_git_dir, git_dir)
        result = run_bounded_command(
            (
                "git",
                f"--git-dir={git_dir}",
                f"--work-tree={workspace}",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "remote",
                "remove",
                "origin",
            ),
            cwd=workspace,
            timeout_seconds=min(30.0, deadline_remaining),
            max_output_chars=2_000,
            env=sanitized_subprocess_env(task_home=workspace.parent / ".task-env"),
        )
        if not result.ok:
            raise WorkspacePreparationError("could not remove source origin from disposable clone")
        return git_dir

    @staticmethod
    def _git_control_snapshot(git_dir: Path) -> dict[str, str | None]:
        snapshot: dict[str, str | None] = {}
        for relative in ("HEAD", "config", "index", "packed-refs", "info/attributes"):
            path = git_dir / relative
            if not path.exists():
                snapshot[relative] = None
                continue
            if path.is_symlink() or not path.is_file():
                raise WorkspacePreparationError(f"unsafe Git control file: {relative}")
            snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return snapshot

    @staticmethod
    def _reject_untracked(
        workspace: Path,
        git_dir: Path,
        deadline_remaining: float,
    ) -> None:
        if (workspace / ".git").exists() or (workspace / ".git").is_symlink():
            raise ToolExecutionError("external backend created Git metadata in its working tree")
        result = run_bounded_command(
            (
                "git",
                f"--git-dir={git_dir}",
                f"--work-tree={workspace}",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ),
            cwd=workspace,
            timeout_seconds=min(30.0, deadline_remaining),
            max_output_chars=20_000,
            env=sanitized_subprocess_env(task_home=workspace.parent / ".task-env"),
        )
        if not result.ok or result.stdout_truncated:
            raise ToolExecutionError("could not obtain a complete external workspace status")
        untracked = tuple(
            entry[3:] for entry in result.stdout.split("\x00") if entry.startswith("?? ")
        )
        if untracked:
            raise ToolExecutionError(
                "external backends may edit tracked files only; untracked output: "
                + ", ".join(untracked)
            )

    @staticmethod
    def _validate_external_patch(
        workspace: Path,
        patch: str,
        changed_paths: tuple[str, ...],
    ) -> None:
        forbidden = (
            "GIT binary patch",
            "Binary files ",
            "new file mode 120000",
            "old mode 120000",
            "rename from ",
            "rename to ",
            "copy from ",
            "copy to ",
        )
        if any(line.startswith(forbidden) for line in patch.splitlines()):
            raise ToolExecutionError(
                "external patch contains a binary, symlink, rename, or copy change"
            )
        for relative in changed_paths:
            target = workspace / relative
            if not target.exists():
                continue
            mode = target.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise ToolExecutionError(f"external patch leaves a non-regular file: {relative}")

    async def _write_text(self, name: str, value: str) -> None:
        path = self.run_dir / name

        def write() -> None:
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    handle.write(value.encode("utf-8"))
                    handle.flush()
                    if self.durable_artifacts:
                        os.fsync(handle.fileno())
                os.replace(temporary, path)
                os.chmod(path, 0o600, follow_symlinks=False)
                if self.durable_artifacts:
                    directory = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

        await asyncio.to_thread(write)

    async def run(self) -> RunResult:
        self._validate_location()
        if self.run_dir.exists():
            raise FileExistsError(f"run directory already exists: {self.run_dir}")
        source = self.task.repository.resolve(strict=True)
        self._assert_clean_source(source, 30.0)
        try:
            await self._require_approval(
                ApprovalRequest(
                    run_id=self.run_id,
                    action_id="external-runtime",
                    effect=ToolEffect.MODIFY,
                    reason=ApprovalReason.MODEL_TOOL,
                    preview=(
                        f"Allow {self.backend.backend_name} to edit Rivumi's disposable clone. "
                        "The source repository remains read-only to the delegated workflow."
                    ),
                    tool_call=ToolCall(
                        name="external_agent",
                        arguments={"backend": self.backend.backend_name},
                    ),
                ),
                explicitly_allowed=self.allow_external_modify,
            )
            self._raise_if_cancelled()
        except _ExternalRunCancelled:
            return RunResult(
                run_id=self.run_id,
                task_id=self.task.task_id,
                status=RunStatus.CANCELLED,
                summary="External coding run cancelled before delegation.",
                terminal_reason="user_cancelled",
            )
        try:
            if self.event_sink is not None:
                await self.event_sink.emit(
                    ExternalAgentEvent(
                        sequence=0,
                        event_type="system",
                        text=(
                            f"{self.backend.backend_name} approved; preparing an isolated clone…"
                        ),
                        data={"source": "rivumi-external-runner"},
                    )
                )
                await asyncio.sleep(0)
            self._raise_if_cancelled()
        except _ExternalRunCancelled:
            return RunResult(
                run_id=self.run_id,
                task_id=self.task.task_id,
                status=RunStatus.CANCELLED,
                summary="External coding run cancelled before workspace preparation.",
                terminal_reason="user_cancelled",
            )
        self.run_dir.mkdir(parents=True, exist_ok=False)
        deadline = time.monotonic() + self.task.limits.wall_time_seconds
        base_sha = self._resolve_base_sha(deadline)
        source_invariant = self._capture_source_invariant(
            source,
            base_sha,
            self._remaining(deadline),
        )
        effective_task = self.task.model_copy(update={"base_sha": base_sha})
        await atomic_write_json(
            self.run_dir / "request.json",
            effective_task,
            durable=self.durable_artifacts,
        )
        await atomic_write_json(
            self.run_dir / "checkpoint.json",
            {
                "run_id": self.run_id,
                "task_id": effective_task.task_id,
                "status": "preparing",
                "backend_name": self.backend.backend_name,
                "resumable": False,
            },
            durable=self.durable_artifacts,
        )
        await self._write_text("events.jsonl", "")
        await self._write_text("changes.patch", "")
        await self._write_text("test.log", "")

        workspace_handle = LocalGitWorkspace(
            effective_task.repository,
            self.run_dir,
            base_sha,
        )
        workspace = await asyncio.to_thread(
            workspace_handle.prepare,
            timeout_seconds=self._remaining(deadline),
        )
        git_dir = await asyncio.to_thread(
            self._isolate_git_metadata,
            workspace,
            self.run_dir,
            self._remaining(deadline),
        )
        git_control_snapshot = self._git_control_snapshot(git_dir)
        policy = SafePathPolicy(workspace, effective_task.allowed_paths)
        executor = ToolExecutor(
            workspace,
            policy,
            effective_task.verification,
            effective_task.limits,
            git_dir=git_dir,
        )

        allowed = "\n".join(f"- {path}" for path in effective_task.allowed_paths)
        delegated_task = ExternalAgentTask(
            task_id=effective_task.task_id,
            instruction=(
                f"{effective_task.instruction}\n\n"
                "You are editing a disposable Git clone. Make the requested code change only; "
                "do not commit, push, or access the network. Rivumi will run final checks after "
                f"you exit. Allowed changed paths:\n{allowed}"
            ),
        )
        backend_result: ExternalAgentResult | None = None
        terminal_reason = "external_agent_error"
        summary = "External coding backend failed."
        verification = ()
        patch = ""
        changed_paths: tuple[str, ...] = ()
        try:
            self._raise_if_cancelled()
            if self.event_sink is not None:
                await self.event_sink.emit(
                    ExternalAgentEvent(
                        sequence=1,
                        event_type="system",
                        text=f"Starting {self.backend.backend_name} in an isolated clone…",
                        data={"source": "rivumi-external-runner"},
                    )
                )
            backend_result = await self._run_backend(
                delegated_task,
                workspace,
                self._remaining(deadline),
            )
            await atomic_write_json(
                self.run_dir / "backend-result.json",
                backend_result,
                durable=self.durable_artifacts,
            )
            event_lines = "".join(
                f"{json.dumps(event.model_dump(mode='json'), ensure_ascii=False, sort_keys=True)}\n"
                for event in backend_result.events
            )
            await self._write_text("events.jsonl", event_lines)

            if not await asyncio.to_thread(
                self._source_invariant_matches,
                source,
                source_invariant,
                self._SOURCE_INVARIANT_TIMEOUT,
            ):
                raise ToolExecutionError("source repository changed during delegation")
            if self._git_control_snapshot(git_dir) != git_control_snapshot:
                raise ToolExecutionError("isolated Git control state changed during delegation")
            await asyncio.to_thread(
                self._reject_untracked,
                workspace,
                git_dir,
                self._remaining(deadline),
            )
            review = await asyncio.to_thread(
                executor.reviewable_patch,
                timeout_seconds=self._remaining(deadline),
            )
            patch = review.content
            changed_paths = review.changed_paths
            self._validate_external_patch(workspace, patch, changed_paths)
            if backend_result.status != "completed":
                terminal_reason = backend_result.terminal_reason
                summary = backend_result.summary or summary
            elif not changed_paths:
                terminal_reason = "no_changes"
                summary = "External agent completed without a reviewable code change."
            else:
                outcomes = []
                for command in effective_task.verification:
                    self._raise_if_cancelled()
                    await self._require_approval(
                        ApprovalRequest(
                            run_id=self.run_id,
                            action_id=f"verification:{command.name}",
                            effect=ToolEffect.EXECUTE,
                            reason=ApprovalReason.FINAL_VERIFICATION,
                            preview="Run exact final verification: " + " ".join(command.argv),
                            command=command,
                        ),
                        explicitly_allowed=(
                            self.allow_unsafe_local_exec
                            or tuple(command.argv) == ("git", "diff", "--check")
                        ),
                    )
                    outcomes.append(
                        await asyncio.to_thread(
                            executor.run_check,
                            command.name,
                            timeout_seconds=self._remaining(deadline),
                        )
                    )
                verification = tuple(outcomes)
                post_verification_review = await asyncio.to_thread(
                    executor.reviewable_patch,
                    timeout_seconds=self._remaining(deadline),
                )
                self._validate_external_patch(
                    workspace,
                    post_verification_review.content,
                    post_verification_review.changed_paths,
                )
                if (
                    post_verification_review.content != patch
                    or post_verification_review.changed_paths != changed_paths
                ):
                    raise ToolExecutionError(
                        "final verification changed the external workspace patch"
                    )
                if all(outcome.ok for outcome in verification):
                    terminal_reason = "verified"
                    summary = backend_result.summary or "External code change verified."
                else:
                    terminal_reason = "verification_failed"
                    summary = "External code change failed final verification."
        except _ExternalRunCancelled:
            terminal_reason = "user_cancelled"
            summary = "External coding run cancelled safely."
        except TimeoutError:
            terminal_reason = "timeout"
            summary = "External coding run exceeded its wall-time budget."
        except (ToolExecutionError, ValueError, OSError) as exc:
            terminal_reason = "policy_or_artifact_error"
            summary = f"External code change was rejected: {exc}"

        try:
            source_unchanged = await asyncio.to_thread(
                self._source_invariant_matches,
                source,
                source_invariant,
                self._SOURCE_INVARIANT_TIMEOUT,
            )
        except TimeoutError:
            source_unchanged = False
        if not source_unchanged:
            terminal_reason = "source_repository_changed"
            summary = (
                "Source repository changed during external delegation; the run cannot be accepted."
            )

        await self._write_text("changes.patch", patch)
        await self._write_text(
            "test.log",
            "\n\n".join(outcome.output for outcome in verification),
        )
        completed = terminal_reason == "verified"
        cancelled = terminal_reason == "user_cancelled"
        artifacts = {
            "request": str(self.run_dir / "request.json"),
            "events": str(self.run_dir / "events.jsonl"),
            "checkpoint": str(self.run_dir / "checkpoint.json"),
            "patch": str(self.run_dir / "changes.patch"),
            "test_log": str(self.run_dir / "test.log"),
            "result": str(self.run_dir / "result.json"),
        }
        if backend_result is not None:
            artifacts["backend_result"] = str(self.run_dir / "backend-result.json")
        failure_error = external_failure_hint(terminal_reason, self.backend.backend_name)
        result = RunResult(
            run_id=self.run_id,
            task_id=effective_task.task_id,
            status=(
                RunStatus.COMPLETED
                if completed
                else (RunStatus.CANCELLED if cancelled else RunStatus.FAILED)
            ),
            summary=summary,
            changed_files=changed_paths,
            verification=verification,
            usage=Usage(),
            terminal_reason=terminal_reason,
            error=failure_error,
            artifacts=artifacts,
        )
        await atomic_write_json(
            self.run_dir / "checkpoint.json",
            {
                "run_id": self.run_id,
                "task_id": effective_task.task_id,
                "status": result.status.value,
                "backend_name": self.backend.backend_name,
                "resumable": False,
                "terminal_reason": terminal_reason,
            },
            durable=self.durable_artifacts,
        )
        await atomic_write_json(
            self.run_dir / "result.json",
            result,
            durable=self.durable_artifacts,
        )
        return result
