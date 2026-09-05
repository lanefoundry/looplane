"""Bounded rg search and its policy-filtered Python fallback."""

from __future__ import annotations

import fnmatch
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from looplane.execution.capture import bounded_text
from looplane.execution.environment import sanitized_subprocess_env
from looplane.execution.local_process import run_local_process
from looplane.execution.types import CommandResult
from looplane.policy import PathPolicyError
from looplane.tooling.filesystem import OutputLimits, ReadLimits, WorkspaceFiles
from looplane.tooling.types import ToolExecutionError


@dataclass
class SearchLimits:
    max_search_results: int = 100


class SearchCommand(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        max_output_chars: int,
        env: Mapping[str, str],
    ) -> CommandResult: ...


class SearchEnvironment(Protocol):
    def __call__(self, *, task_home: Path | None = None) -> dict[str, str]: ...


class WorkspaceSearch:
    def __init__(
        self,
        *,
        files: WorkspaceFiles,
        search_limits: SearchLimits,
        read_limits: ReadLimits,
        output_limits: OutputLimits,
        task_home: Path,
        run_command: SearchCommand = run_local_process,
        environment: SearchEnvironment = sanitized_subprocess_env,
        which: Callable[[str], str | None] = shutil.which,
        bound: Callable[[str, int], str] = bounded_text,
    ) -> None:
        self.files = files
        self.policy = files.policy
        self.workspace = files.workspace
        self.search_limits = search_limits
        self.read_limits = read_limits
        self.output_limits = output_limits
        self.task_home = task_home
        self.run_command = run_command
        self.environment = environment
        self.which = which
        self.bound = bound

    def search_text(
        self,
        query: str,
        path: str = ".",
        glob: str | None = None,
        case_sensitive: bool = True,
    ) -> str:
        if not isinstance(query, str) or not query:
            raise ToolExecutionError("search query must be a non-empty string")
        root = self.policy.resolve(path, allow_workspace_root=True)
        if not root.exists():
            raise ToolExecutionError(f"path does not exist: {path}")
        rg_result = self.search_with_rg(
            query=query,
            root=root,
            glob=glob,
            case_sensitive=case_sensitive,
        )
        if rg_result is not None:
            return rg_result
        needle = query if case_sensitive else query.casefold()
        matches: list[str] = []
        for file_path in self.files.walk(root):
            relative = file_path.relative_to(self.workspace).as_posix()
            if glob and not fnmatch.fnmatchcase(relative, glob):
                continue
            try:
                with file_path.open("rb") as handle:
                    data = handle.read(self.read_limits.max_read_bytes + 1)
            except OSError:
                continue
            if b"\x00" in data:
                continue
            for line_number, line in enumerate(
                data.decode("utf-8", errors="replace").splitlines(), 1
            ):
                haystack = line if case_sensitive else line.casefold()
                if needle in haystack:
                    matches.append(f"{relative}:{line_number}:{line}")
                    if len(matches) >= self.search_limits.max_search_results:
                        matches.append(
                            "... search truncated at "
                            f"{self.search_limits.max_search_results} matches ..."
                        )
                        return self.bound("\n".join(matches), self.output_limits.max_output_chars)
        return self.bound("\n".join(matches), self.output_limits.max_output_chars)

    def search_with_rg(
        self,
        *,
        query: str,
        root: Path,
        glob: str | None,
        case_sensitive: bool,
    ) -> str | None:
        if self.which("rg") is None:
            return None
        try:
            search_root = root.relative_to(self.workspace).as_posix()
        except ValueError:
            return None
        argv = [
            "rg",
            "--fixed-strings",
            "--line-number",
            "--no-heading",
            "--color",
            "never",
        ]
        if not case_sensitive:
            argv.append("--ignore-case")
        if glob:
            argv.extend(("--glob", glob))
        argv.extend(("--", query, search_root))
        result = self.run_command(
            tuple(argv),
            cwd=self.workspace,
            timeout_seconds=10.0,
            max_output_chars=self.output_limits.max_output_chars,
            env=self.environment(task_home=self.task_home),
        )
        if result.returncode not in {0, 1}:
            return None
        matches: list[str] = []
        for line in result.stdout.splitlines():
            relative, separator, _rest = line.partition(":")
            if not separator:
                continue
            try:
                self.policy.resolve(relative)
            except (PathPolicyError, ValueError):
                continue
            matches.append(line)
            if len(matches) >= self.search_limits.max_search_results:
                matches.append(
                    f"... search truncated at {self.search_limits.max_search_results} matches ..."
                )
                break
        return self.bound("\n".join(matches), self.output_limits.max_output_chars)
