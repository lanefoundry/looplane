from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from functools import cache
from pathlib import Path, PurePosixPath, PureWindowsPath


class PathPolicyError(ValueError):
    """Raised when a tool path crosses the configured workspace boundary."""


class SafePathPolicy:
    """Resolve model-supplied paths without allowing workspace escapes."""

    def __init__(
        self,
        workspace_root: Path,
        allowed_paths: Sequence[str] = ("**",),
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        if not self.workspace_root.is_dir():
            raise ValueError(f"workspace is not a directory: {self.workspace_root}")

        patterns = tuple(self._normalize_pattern(pattern) for pattern in allowed_paths)
        if not patterns:
            raise ValueError("allowed_paths must contain at least one path or glob")
        self.allowed_paths = patterns

    @staticmethod
    def _normalize_pattern(pattern: str) -> str:
        if not isinstance(pattern, str) or not pattern.strip():
            raise ValueError("allowed path patterns must be non-empty strings")
        raw = pattern.strip().replace("\\", "/")
        candidate = PurePosixPath(raw)
        if candidate.is_absolute() or ".." in candidate.parts or raw.startswith("~"):
            raise ValueError(f"unsafe allowed path pattern: {pattern!r}")
        normalized = raw.removeprefix("./").rstrip("/")
        return normalized or "."

    @staticmethod
    def _validate_relative_input(path: str | Path) -> str:
        raw = str(path)
        if not raw or "\x00" in raw:
            raise PathPolicyError("path must be a non-empty relative path")
        if "\\" in raw:
            raise PathPolicyError("backslashes are not accepted in tool paths")

        posix_path = PurePosixPath(raw)
        windows_path = PureWindowsPath(raw)
        if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
            raise PathPolicyError(f"absolute paths are forbidden: {raw!r}")
        if ".." in posix_path.parts:
            raise PathPolicyError(f"path traversal is forbidden: {raw!r}")
        if any(part == ".git" for part in posix_path.parts):
            raise PathPolicyError("the workspace .git directory is never tool-accessible")
        return raw

    def _matches_allowed(self, relative: str) -> bool:
        for pattern in self.allowed_paths:
            if pattern in {".", "**"}:
                return True
            if not any(character in pattern for character in "*?["):
                if relative == pattern or relative.startswith(f"{pattern}/"):
                    return True
            elif self._match_path_glob(relative, pattern):
                return True
        return False

    @staticmethod
    def _match_path_glob(relative: str, pattern: str) -> bool:
        """Match path segments; only a complete ``**`` segment crosses directories."""

        path_parts = tuple(PurePosixPath(relative).parts)
        pattern_parts = tuple(PurePosixPath(pattern).parts)

        @cache
        def match(path_index: int, pattern_index: int) -> bool:
            if pattern_index == len(pattern_parts):
                return path_index == len(path_parts)
            current = pattern_parts[pattern_index]
            if current == "**":
                return match(path_index, pattern_index + 1) or (
                    path_index < len(path_parts) and match(path_index + 1, pattern_index)
                )
            return (
                path_index < len(path_parts)
                and fnmatch.fnmatchcase(path_parts[path_index], current)
                and match(path_index + 1, pattern_index + 1)
            )

        return match(0, 0)

    def resolve(
        self,
        path: str | Path,
        *,
        allow_workspace_root: bool = False,
    ) -> Path:
        raw = self._validate_relative_input(path)
        candidate = (self.workspace_root / raw).resolve(strict=False)
        try:
            relative = candidate.relative_to(self.workspace_root).as_posix()
        except ValueError as exc:
            raise PathPolicyError(f"path escapes workspace through a symlink: {raw!r}") from exc

        if relative == ".":
            if allow_workspace_root:
                return candidate
            raise PathPolicyError("the workspace root is not a file target")
        if not self._matches_allowed(relative):
            raise PathPolicyError(f"path is outside allowed_paths: {relative!r}")
        return candidate

    def is_allowed(self, path: str | Path, *, allow_workspace_root: bool = False) -> bool:
        try:
            self.resolve(path, allow_workspace_root=allow_workspace_root)
        except PathPolicyError:
            return False
        return True

    def relative_path(self, path: str | Path) -> str:
        candidate = Path(path).resolve(strict=False)
        try:
            relative = candidate.relative_to(self.workspace_root).as_posix()
        except ValueError as exc:
            raise PathPolicyError(f"path is outside workspace: {candidate}") from exc
        if relative == ".git" or relative.startswith(".git/"):
            raise PathPolicyError("the workspace .git directory is never tool-accessible")
        return relative
