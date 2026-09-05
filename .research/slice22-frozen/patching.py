"""File edits coordinated through policy, version, atomic-write and Git/review ports."""

from __future__ import annotations

import json
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from looplane.execution.types import CommandResult
from looplane.policy import SafePathPolicy
from looplane.tooling.filesystem import ReadLimits
from looplane.tooling.patch_validation import PatchLimits, UnifiedDiffValidator
from looplane.tooling.read_versions import ReadVersionStore
from looplane.tooling.snapshots import AtomicWrite
from looplane.tooling.types import ReviewablePatch, ToolExecutionError


class PatchGitCommand(Protocol):
    def __call__(
        self, argv: Sequence[str], *, stdin: str | None = None,
        timeout_seconds: float | None = None, max_output_bytes: int | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


class PatchReview(Protocol):
    def __call__(self, *, timeout_seconds: float | None = None) -> ReviewablePatch: ...


class PatchOperations:
    def __init__(
        self, *, policy: SafePathPolicy, versions: ReadVersionStore,
        validator: UnifiedDiffValidator, read_limits: ReadLimits, patch_limits: PatchLimits,
        atomic_write: AtomicWrite, git: PatchGitCommand, review: PatchReview,
        effective_timeout: Callable[[float, float | None], float],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy = policy
        self.workspace = policy.workspace_root
        self.versions = versions
        self.validator = validator
        self.read_limits = read_limits
        self.patch_limits = patch_limits
        self.atomic_write = atomic_write
        self.git = git
        self.review = review
        self.effective_timeout = effective_timeout
        self.clock = clock

    @staticmethod
    def quoted_diff_path(prefix: str, path: str) -> str:
        return json.dumps(f"{prefix}/{path}", ensure_ascii=False)


    def create_file(
        self,
        path: str,
        content: str,
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        """Create a UTF-8 text file through the validated patch transaction."""

        if not isinstance(content, str) or not content:
            raise ToolExecutionError("content must be a non-empty string")
        if "\x00" in content:
            raise ToolExecutionError("create_file accepts UTF-8 text without NUL")
        target = self.policy.resolve(path)
        if target.exists():
            raise ToolExecutionError(f"path already exists: {path}")
        relative = target.relative_to(self.workspace).as_posix()
        if any(character in relative for character in ("\n", "\r", "\x00")):
            raise ToolExecutionError("create_file path contains unsupported control characters")

        content_lines = content.split("\n")
        ends_with_newline = content.endswith("\n")
        if ends_with_newline:
            content_lines.pop()
        quoted_old = self.quoted_diff_path("a", relative)
        quoted_new = self.quoted_diff_path("b", relative)
        patch_lines = [
            f"diff --git {quoted_old} {quoted_new}",
            "new file mode 100644",
            "--- /dev/null",
            f"+++ {quoted_new}",
            f"@@ -0,0 +1,{len(content_lines)} @@",
            *(f"+{line}" for line in content_lines),
        ]
        if not ends_with_newline:
            patch_lines.append(r"\ No newline at end of file")
        patch = "\n".join(patch_lines) + "\n"
        result = self.apply_patch(patch, timeout_seconds=timeout_seconds)
        return f"created UTF-8 text file {relative}\n{result}"


    def apply_patch(self, patch: str, *, timeout_seconds: float | None = None) -> str:
        paths = self.validator.validate(patch)
        new_paths = tuple(path for path in paths if not (self.workspace / path).exists())
        budget = self.effective_timeout(30.0, timeout_seconds)
        deadline = self.clock() + budget

        def remaining() -> float:
            value = deadline - self.clock()
            if value <= 0:
                raise ToolExecutionError("apply_patch exceeded the harness timeout")
            return value

        checked = self.git(
            ("apply", "--check", "--whitespace=error-all", "-"),
            stdin=patch,
            timeout_seconds=remaining(),
        )
        if not checked.ok:
            raise ToolExecutionError(f"git apply --check failed: {checked.stderr.strip()}")
        applied = self.git(
            ("apply", "--whitespace=error-all", "-"),
            stdin=patch,
            timeout_seconds=remaining(),
        )
        if not applied.ok:
            raise ToolExecutionError(f"git apply failed: {applied.stderr.strip()}")
        for path in new_paths:
            try:
                intent = self.git(
                    ("add", "--intent-to-add", "--", path),
                    timeout_seconds=remaining(),
                )
            except ToolExecutionError as exc:
                self.rollback_patch(patch, new_paths)
                raise ToolExecutionError(
                    f"could not register new file for reviewable diff: {exc}"
                ) from exc
            if not intent.ok:
                self.rollback_patch(patch, new_paths)
                raise ToolExecutionError(
                    f"could not register new file for reviewable diff: {intent.stderr.strip()}"
                )
        try:
            self.review(timeout_seconds=remaining())
        except ToolExecutionError as exc:
            self.rollback_patch(patch, new_paths)
            raise ToolExecutionError(
                f"cumulative patch is not reviewable within the task limits: {exc}"
            ) from exc
        return f"applied unified diff to {len(paths)} file(s):\n" + "\n".join(paths)


    def replace_text(
        self,
        path: str,
        old_text: str,
        new_text: str,
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        """Atomically replace an exact fragment and retain a bounded reviewable patch."""

        if not isinstance(old_text, str) or not old_text:
            raise ToolExecutionError("old_text must be a non-empty string")
        if not isinstance(new_text, str):
            raise ToolExecutionError("new_text must be a string")
        if "\x00" in old_text or "\x00" in new_text:
            raise ToolExecutionError("replace_text accepts UTF-8 text fragments without NUL")
        if old_text == new_text:
            raise ToolExecutionError("old_text and new_text must differ")
        argument_bytes = len(old_text.encode("utf-8")) + len(new_text.encode("utf-8"))
        if argument_bytes > self.patch_limits.max_patch_bytes:
            raise ToolExecutionError(f"replacement arguments exceed {self.patch_limits.max_patch_bytes} bytes")

        target = self.policy.resolve(path)
        if not target.is_file():
            raise ToolExecutionError(f"not a regular file: {path}")
        with target.open("rb") as handle:
            original = handle.read(self.read_limits.max_read_bytes + 1)
        if len(original) > self.read_limits.max_read_bytes:
            raise ToolExecutionError(f"file exceeds {self.read_limits.max_read_bytes} readable bytes")
        if b"\x00" in original:
            raise ToolExecutionError("replace_text accepts UTF-8 text files only")
        try:
            source = original.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ToolExecutionError("replace_text accepts UTF-8 text files only") from exc
        relative = target.relative_to(self.workspace).as_posix()
        self.versions.require_current(relative, original)
        observed = source.count(old_text)
        if observed != 1:
            raise ToolExecutionError(f"exact replacement requires one match; observed {observed}")
        updated = source.replace(old_text, new_text, 1).encode("utf-8")
        if len(updated) > self.read_limits.max_read_bytes:
            raise ToolExecutionError(f"resulting file exceeds {self.read_limits.max_read_bytes} bytes")

        mode = stat.S_IMODE(target.stat().st_mode)
        budget = self.effective_timeout(30.0, timeout_seconds)
        deadline = self.clock() + budget

        def remaining() -> float:
            value = deadline - self.clock()
            if value <= 0:
                raise ToolExecutionError("replace_text exceeded the harness timeout")
            return value

        tracked = self.git(
            ("ls-files", "--error-unmatch", "--", relative),
            timeout_seconds=remaining(),
        )
        if not tracked.ok:
            raise ToolExecutionError(
                "replace_text requires a Git-tracked file; use apply_patch to create a file"
            )

        try:
            self.atomic_write(target, updated, mode)
            whitespace = self.git(
                ("diff", "--check", "--", relative),
                timeout_seconds=remaining(),
            )
            if not whitespace.ok:
                raise ToolExecutionError(
                    f"replacement introduces whitespace errors: {whitespace.stderr.strip()}"
                )
            self.review(timeout_seconds=remaining())
        except (OSError, ToolExecutionError) as exc:
            try:
                self.atomic_write(target, original, mode)
            except OSError as rollback_exc:
                try:
                    with target.open("rb") as handle:
                        restored = handle.read(self.read_limits.max_read_bytes + 1) == original
                    restored_mode = stat.S_IMODE(target.stat().st_mode) == mode
                except OSError:
                    restored = False
                    restored_mode = False
                if not restored or not restored_mode:
                    raise ToolExecutionError(
                        f"replacement rollback failed: {rollback_exc}"
                    ) from exc
            raise ToolExecutionError(f"replacement was refused and rolled back: {exc}") from exc
        self.versions.record(relative, updated)
        return f"replaced one exact text fragment in {path}"


    def rollback_patch(self, patch: str, new_paths: Sequence[str]) -> None:
        reversed_patch = self.git(
            ("apply", "--reverse", "--whitespace=nowarn", "-"),
            stdin=patch,
            timeout_seconds=5.0,
        )
        reset = self.git(
            ("reset", "--quiet", "HEAD", "--", *new_paths),
            timeout_seconds=5.0,
        )
        if not reversed_patch.ok or not reset.ok:
            details = "; ".join(
                detail
                for detail in (
                    reversed_patch.stderr.strip(),
                    reset.stderr.strip(),
                )
                if detail
            )
            raise ToolExecutionError(f"patch rollback failed: {details or 'unknown git error'}")


