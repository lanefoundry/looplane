"""Policy-bound traversal and reads with explicit byte and output limits."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from looplane.execution.capture import bounded_text
from looplane.policy import PathPolicyError, SafePathPolicy
from looplane.tooling.read_versions import ReadVersionStore
from looplane.tooling.types import ToolExecutionError


@dataclass
class OutputLimits:
    max_output_chars: int = 200_000


@dataclass
class ReadLimits:
    max_read_bytes: int = 100_000
    max_list_files: int = 500


class WorkspaceFiles:
    def __init__(
        self,
        *,
        policy: SafePathPolicy,
        versions: ReadVersionStore,
        read_limits: ReadLimits,
        output_limits: OutputLimits,
        bound: Callable[[str, int], str] = bounded_text,
    ) -> None:
        self.policy = policy
        self.workspace = policy.workspace_root
        self.versions = versions
        self.read_limits = read_limits
        self.output_limits = output_limits
        self.bound = bound

    def walk(self, root: Path) -> Iterator[Path]:
        if root.is_file():
            yield root
            return
        for current, directories, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            directories[:] = sorted(
                directory
                for directory in directories
                if directory != ".git" and not (current_path / directory).is_symlink()
            )
            for filename in sorted(filenames):
                path = current_path / filename
                try:
                    relative = path.relative_to(self.workspace).as_posix()
                    self.policy.resolve(relative)
                except (PathPolicyError, ValueError):
                    continue
                yield path

    def list_files(self, path: str = ".") -> str:
        root = self.policy.resolve(path, allow_workspace_root=True)
        if not root.exists():
            raise ToolExecutionError(f"path does not exist: {path}")
        files: list[str] = []
        for file_path in self.walk(root):
            files.append(file_path.relative_to(self.workspace).as_posix())
            if len(files) >= self.read_limits.max_list_files:
                files.append(
                    f"... file list truncated at {self.read_limits.max_list_files} entries ..."
                )
                break
        return self.bound("\n".join(files), self.output_limits.max_output_chars)

    def read_file(self, path: str) -> str:
        target = self.policy.resolve(path)
        if not target.is_file():
            raise ToolExecutionError(f"not a regular file: {path}")
        with target.open("rb") as handle:
            data = handle.read(self.read_limits.max_read_bytes + 1)
        truncated = len(data) > self.read_limits.max_read_bytes
        visible = data[: self.read_limits.max_read_bytes]
        text = visible.decode("utf-8", errors="replace")
        if not truncated:
            relative = target.relative_to(self.workspace).as_posix()
            self.versions.record(relative, visible)
        if truncated:
            text += f"\n... file truncated at {self.read_limits.max_read_bytes} bytes ..."
        return self.bound(text, self.output_limits.max_output_chars)
