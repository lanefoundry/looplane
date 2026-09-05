"""Unified text diff validation with shared path policy and explicit limits."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from looplane.policy import SafePathPolicy
from looplane.tooling.types import ToolExecutionError


@dataclass
class PatchLimits:
    max_patch_bytes: int = 100_000
    max_patch_lines: int = 4_000
    max_changed_files: int = 50


class UnifiedDiffValidator:
    HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")

    def __init__(self, *, policy: SafePathPolicy, limits: PatchLimits) -> None:
        self.policy = policy
        self.limits = limits

    @staticmethod
    def header_path(line: str, marker: str) -> str | None:
        value = line[len(marker) :].strip()
        if value == "/dev/null":
            return None
        try:
            fields = shlex.split(value)
        except ValueError as exc:
            raise ToolExecutionError(f"invalid unified diff header: {line!r}") from exc
        if not fields:
            raise ToolExecutionError(f"missing path in unified diff header: {line!r}")
        path = fields[0]
        if path.startswith(("a/", "b/")):
            path = path[2:]
        return path


    @staticmethod
    def diff_git_paths(line: str) -> tuple[str, str]:
        try:
            fields = shlex.split(line[len("diff --git ") :])
        except ValueError as exc:
            raise ToolExecutionError(f"invalid diff --git header: {line!r}") from exc
        if len(fields) != 2 or not fields[0].startswith("a/") or not fields[1].startswith("b/"):
            raise ToolExecutionError(f"invalid diff --git header: {line!r}")
        return fields[0][2:], fields[1][2:]


    def validate(self, patch: str) -> tuple[str, ...]:
        if not isinstance(patch, str) or not patch.strip():
            raise ToolExecutionError("patch must be a non-empty unified diff")
        if len(patch.encode("utf-8")) > self.limits.max_patch_bytes:
            raise ToolExecutionError(f"patch exceeds {self.limits.max_patch_bytes} bytes")
        lines = patch.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        if len(lines) > self.limits.max_patch_lines:
            raise ToolExecutionError(f"patch exceeds {self.limits.max_patch_lines} lines")
        forbidden_markers = (
            "GIT binary patch",
            "Binary files ",
            "literal ",
            "delta ",
            "new file mode 120000",
            "old mode 120000",
            "rename from ",
            "rename to ",
            "copy from ",
            "copy to ",
        )
        if any(line.startswith(forbidden_markers) for line in lines):
            raise ToolExecutionError("binary, symlink, rename, and copy patches are forbidden")

        paths: set[str] = set()
        seen_file_paths: set[str] = set()
        index = 0
        file_count = 0
        while index < len(lines):
            if not lines[index].startswith("diff --git "):
                raise ToolExecutionError(
                    f"invalid unified diff at line {index + 1}: expected 'diff --git'"
                )
            diff_old_path, diff_new_path = self.diff_git_paths(lines[index])
            file_count += 1
            index += 1

            while index < len(lines) and not lines[index].startswith("--- "):
                metadata = lines[index]
                if metadata.startswith(("diff --git ", "@@ ")):
                    break
                if not metadata.startswith(
                    ("index ", "new file mode ", "deleted file mode ", "old mode ", "new mode ")
                ):
                    raise ToolExecutionError(
                        f"invalid unified diff metadata at line {index + 1}: {metadata!r}"
                    )
                index += 1

            if index >= len(lines) or not lines[index].startswith("--- "):
                raise ToolExecutionError(
                    f"invalid unified diff at line {index + 1}: missing old-file header"
                )
            old_header = lines[index]
            index += 1
            if index >= len(lines) or not lines[index].startswith("+++ "):
                raise ToolExecutionError(
                    f"invalid unified diff at line {index + 1}: missing new-file header"
                )
            new_header = lines[index]
            index += 1

            old_path = self.header_path(old_header, "--- ")
            new_path = self.header_path(new_header, "+++ ")
            if old_path is None and new_path is None:
                raise ToolExecutionError("unified diff cannot use /dev/null for both paths")
            if old_path is not None and new_path is not None and old_path != new_path:
                raise ToolExecutionError("rename-style unified diffs are forbidden")
            if old_path is not None and old_path != diff_old_path:
                raise ToolExecutionError("diff --git and old-file header paths do not match")
            if new_path is not None and new_path != diff_new_path:
                raise ToolExecutionError("diff --git and new-file header paths do not match")
            target_path = new_path if new_path is not None else old_path
            assert target_path is not None
            if diff_old_path != target_path or diff_new_path != target_path:
                raise ToolExecutionError("diff --git paths must name the modified file")
            if target_path in seen_file_paths:
                raise ToolExecutionError(f"duplicate unified diff section for path: {target_path}")
            seen_file_paths.add(target_path)
            self.policy.resolve(target_path)
            paths.add(target_path)

            hunk_count = 0
            old_closed = False
            new_closed = False
            while index < len(lines) and not lines[index].startswith("diff --git "):
                match = self.HUNK_HEADER.fullmatch(lines[index])
                if match is None:
                    raise ToolExecutionError(
                        f"invalid unified diff at line {index + 1}: expected hunk header"
                    )
                hunk_count += 1
                old_expected = int(match.group(2) or "1")
                new_expected = int(match.group(4) or "1")
                old_seen = 0
                new_seen = 0
                index += 1
                last_prefix: str | None = None

                while old_seen < old_expected or new_seen < new_expected:
                    if index >= len(lines):
                        raise ToolExecutionError(
                            f"invalid unified diff hunk at line {index + 1}: "
                            f"expected {old_expected} old/{new_expected} new lines, "
                            f"observed {old_seen} old/{new_seen} new"
                        )
                    body = lines[index]
                    if body == r"\ No newline at end of file":
                        if last_prefix is None:
                            raise ToolExecutionError(
                                f"invalid unified diff marker at line {index + 1}"
                            )
                        if last_prefix in " -":
                            old_closed = True
                        if last_prefix in " +":
                            new_closed = True
                        last_prefix = None
                        index += 1
                        continue
                    if not body or body[0] not in " +-":
                        raise ToolExecutionError(
                            f"invalid unified diff body prefix at line {index + 1}"
                        )
                    if body[0] in " -" and old_closed:
                        raise ToolExecutionError(
                            f"unified diff contributes to closed old side at line {index + 1}"
                        )
                    if body[0] in " +" and new_closed:
                        raise ToolExecutionError(
                            f"unified diff contributes to closed new side at line {index + 1}"
                        )
                    if body[0] in " -":
                        old_seen += 1
                    if body[0] in " +":
                        new_seen += 1
                    if old_seen > old_expected or new_seen > new_expected:
                        raise ToolExecutionError(
                            f"unified diff hunk exceeds declared line count at line {index + 1}"
                        )
                    last_prefix = body[0]
                    index += 1

                if index < len(lines) and lines[index] == r"\ No newline at end of file":
                    if last_prefix is None:
                        raise ToolExecutionError(
                            f"invalid unified diff marker at line {index + 1}"
                        )
                    if last_prefix in " -":
                        old_closed = True
                    if last_prefix in " +":
                        new_closed = True
                    index += 1
                if index < len(lines) and not (
                    lines[index].startswith("@@ ") or lines[index].startswith("diff --git ")
                ):
                    raise ToolExecutionError(
                        f"unexpected trailing unified diff content at line {index + 1}"
                    )

            if hunk_count == 0:
                raise ToolExecutionError("apply_patch requires at least one hunk per file")

        if file_count == 0:
            raise ToolExecutionError("apply_patch accepts unified text diffs only")
        if not paths:
            raise ToolExecutionError("patch does not name a workspace file")
        if len(paths) > self.limits.max_changed_files:
            raise ToolExecutionError(f"patch exceeds {self.limits.max_changed_files} changed files")
        return tuple(sorted(paths))


