from __future__ import annotations

import fnmatch as fnmatch
import hashlib as hashlib
import json as json
import os as os
import re as re
import shlex as shlex
import shutil
import stat as stat
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from .contracts import (
    ToolCall,
    ToolDefinition,
    ToolObservation,
    VerificationCommand,
    VerificationOutcome,
)
from .mcp_client import (
    HttpMcpClient,
    McpError,
    NativeMcpServerConfig,
    StdioMcpClient,
    native_mcp_prompt_tool_name,
    native_mcp_resource_tool_name,
    split_native_mcp_tool_name,
)
from .policy import PathPolicyError, SafePathPolicy
from .runtime import (
    LocalGitWorkspace,
    bounded_text,
    resolve_command_sandbox,
    run_bounded_command,
    sanitized_subprocess_env,
)
from .secret_scan import redact_secrets, scan_text_for_secrets
from .tooling.definitions import tool_definitions
from .tooling.filesystem import OutputLimits, ReadLimits, WorkspaceFiles
from .tooling.patch_validation import PatchLimits, UnifiedDiffValidator
from .tooling.patching import PatchOperations
from .tooling.read_versions import ReadVersionStore
from .tooling.search import SearchLimits, WorkspaceSearch
from .tooling.snapshots import AtomicFileWriter, WorkspaceSnapshots
from .tooling.mcp_bridge import McpBridge, McpClient, McpToolNames
from .tooling.types import ReviewablePatch as ReviewablePatch
from .tooling.types import ToolExecutionError as ToolExecutionError
from .tooling.types import _PathSnapshot as _PathSnapshot


class ToolExecutor:
    _HUNK_HEADER = UnifiedDiffValidator.HUNK_HEADER
    def __init__(
        self,
        workspace: Path | LocalGitWorkspace,
        policy: SafePathPolicy,
        verification_commands: Sequence[VerificationCommand],
        limits: object | None = None,
        *,
        git_dir: Path | None = None,
        base_sha: str | None = None,
        task_home: Path | None = None,
        preexisting_dirty_paths: frozenset[str] = frozenset(),
        mcp_servers: Sequence[NativeMcpServerConfig] = (),
        sandbox_checks: bool = False,
        sandbox_profile: str | None = None,
        sandbox_backend: str | None = None,
        sandbox_read_roots: Sequence[Path] = (),
    ) -> None:
        self._output_limits = OutputLimits()
        self._read_limits = ReadLimits()
        self._search_limits = SearchLimits()
        self._patch_limits = PatchLimits()
        self.workspace = (
            workspace.workspace_path
            if isinstance(workspace, LocalGitWorkspace)
            else Path(workspace)
        ).resolve(strict=True)
        if self.workspace != policy.workspace_root:
            raise ValueError("ToolExecutor workspace and SafePathPolicy root must match")
        self.git_dir = Path(git_dir).resolve(strict=True) if git_dir is not None else None
        if self.git_dir is not None and not self.git_dir.is_dir():
            raise ValueError("git_dir must be an existing directory")
        self.policy = policy
        self.max_output_chars = self._limit_alias(
            limits,
            ("max_tool_output_bytes", "max_output_chars"),
            200_000,
        )
        self.max_read_bytes = self._limit(limits, "max_read_bytes", 100_000)
        self.max_patch_bytes = self._limit(limits, "max_patch_bytes", 100_000)
        self.max_patch_lines = self._limit(limits, "max_patch_lines", 4_000)
        self.max_changed_files = self._limit(limits, "max_changed_files", 50)
        self.max_list_files = self._limit(limits, "max_list_files", 500)
        self.max_search_results = self._limit(limits, "max_search_results", 100)
        self.max_tool_program_steps = self._limit(limits, "max_tool_program_steps", 8)
        self.base_sha = base_sha
        self._preexisting_dirty_paths = frozenset(preexisting_dirty_paths)
        self._task_home = (
            Path(task_home).resolve(strict=False)
            if task_home is not None
            else self.workspace.parent / ".check-task-env"
        )
        self._sandbox_checks = sandbox_checks
        self._sandbox_profile = sandbox_profile or "verification"
        self._sandbox_backend = sandbox_backend or "auto"
        self._sandbox_read_roots = tuple(Path(root) for root in sandbox_read_roots)
        self.read_versions = ReadVersionStore()
        self.mcp_bridge = McpBridge(
            mcp_servers,
            client_factory=self._mcp_client,
            names=McpToolNames(
                resource=native_mcp_resource_tool_name,
                prompt=native_mcp_prompt_tool_name,
                split_tool=split_native_mcp_tool_name,
            ),
        )

        self.verification_commands: dict[str, VerificationCommand] = {}
        self.verification_outcomes: dict[str, VerificationOutcome] = {}
        for command in verification_commands:
            name = str(command.name)
            argv = tuple(command.argv)
            timeout_seconds = float(command.timeout_seconds)
            if not name or not argv or not all(isinstance(arg, str) and arg for arg in argv):
                raise ValueError("verification commands require a name and exact non-empty argv")
            if timeout_seconds <= 0:
                raise ValueError("verification command timeout_seconds must be positive")
            if name in self.verification_commands:
                raise ValueError(f"duplicate verification command name: {name}")
            self.verification_commands[name] = command
        self.files = WorkspaceFiles(
            policy=self.policy,
            versions=self.read_versions,
            read_limits=self._read_limits,
            output_limits=self._output_limits,
            bound=lambda value, limit: bounded_text(value, limit),
        )
        self.search = WorkspaceSearch(
            files=self.files,
            search_limits=self._search_limits,
            read_limits=self._read_limits,
            output_limits=self._output_limits,
            task_home=self._task_home,
            run_command=lambda argv, **options: run_bounded_command(argv, **options),
            environment=lambda **options: sanitized_subprocess_env(**options),
            which=lambda name: shutil.which(name),
            bound=lambda value, limit: bounded_text(value, limit),
        )
        self.patch_validator = UnifiedDiffValidator(policy=self.policy, limits=self._patch_limits)
        self.snapshots = WorkspaceSnapshots(
            policy=self.policy,
            versions=self.read_versions,
            atomic_write=lambda target, payload, mode: self._atomic_replace_file(
                target, payload, mode
            ),
            reset_index=lambda paths, **options: self._git(
                ("reset", "--quiet", "HEAD", "--", *paths), **options
            ),
        )
        self.patching = PatchOperations(
            policy=self.policy,
            versions=self.read_versions,
            validator=self.patch_validator,
            read_limits=self._read_limits,
            patch_limits=self._patch_limits,
            atomic_write=lambda target, payload, mode: self._atomic_replace_file(
                target, payload, mode
            ),
            git=lambda argv, **options: self._git(argv, **options),
            review=lambda **options: self.reviewable_patch(**options),
            effective_timeout=lambda default, override: self._effective_timeout(default, override),
            clock=lambda: time.monotonic(),
        )
        self.definitions = self._build_definitions()

    @property
    def max_output_chars(self) -> int:
        return self._output_limits.max_output_chars

    @max_output_chars.setter
    def max_output_chars(self, value: int) -> None:
        self._output_limits.max_output_chars = value

    @property
    def max_read_bytes(self) -> int:
        return self._read_limits.max_read_bytes

    @max_read_bytes.setter
    def max_read_bytes(self, value: int) -> None:
        self._read_limits.max_read_bytes = value

    @property
    def max_list_files(self) -> int:
        return self._read_limits.max_list_files

    @max_list_files.setter
    def max_list_files(self, value: int) -> None:
        self._read_limits.max_list_files = value

    @property
    def max_search_results(self) -> int:
        return self._search_limits.max_search_results

    @max_search_results.setter
    def max_search_results(self, value: int) -> None:
        self._search_limits.max_search_results = value

    @property
    def max_patch_bytes(self) -> int:
        return self._patch_limits.max_patch_bytes

    @max_patch_bytes.setter
    def max_patch_bytes(self, value: int) -> None:
        self._patch_limits.max_patch_bytes = value

    @property
    def max_patch_lines(self) -> int:
        return self._patch_limits.max_patch_lines

    @max_patch_lines.setter
    def max_patch_lines(self, value: int) -> None:
        self._patch_limits.max_patch_lines = value

    @property
    def max_changed_files(self) -> int:
        return self._patch_limits.max_changed_files

    @max_changed_files.setter
    def max_changed_files(self, value: int) -> None:
        self._patch_limits.max_changed_files = value

    @property
    def _read_versions(self) -> dict[str, str]:
        return self.read_versions.versions

    @_read_versions.setter
    def _read_versions(self, value: dict[str, str]) -> None:
        self.read_versions.versions = value

    def _mcp_client(self, config: NativeMcpServerConfig) -> StdioMcpClient:
        if config.url is not None:
            return HttpMcpClient(config, max_output_chars=self.max_output_chars)
        return StdioMcpClient(
            config,
            cwd=self.workspace,
            task_home=self._task_home,
            max_output_chars=self.max_output_chars,
        )

    def _build_definitions(self) -> tuple[ToolDefinition, ...]:
        self.mcp_bridge.clear_routes()
        definitions = list(self._tool_definitions())
        definitions.extend(self._mcp_tool_definitions())
        run_check_index = next(
            index for index, definition in enumerate(definitions) if definition.name == "run_check"
        )
        run_check_definition = definitions[run_check_index]
        run_check_schema = dict(run_check_definition.input_schema)
        run_check_properties = dict(run_check_schema["properties"])
        run_check_name = dict(run_check_properties["name"])
        run_check_name["enum"] = sorted(self.verification_commands)
        run_check_properties["name"] = run_check_name
        run_check_schema["properties"] = run_check_properties
        definitions[run_check_index] = run_check_definition.model_copy(
            update={"input_schema": run_check_schema}
        )
        return tuple(definitions)

    def refresh_mcp_tool_definitions(self) -> bool:
        """Refresh MCP discovery and return whether provider-facing definitions changed."""

        before = tuple(
            definition.model_dump(mode="json")
            for definition in self.definitions
            if definition.name.startswith(("mcp__", "mcp_resource__", "mcp_prompt__"))
        )
        self.definitions = self._build_definitions()
        after = tuple(
            definition.model_dump(mode="json")
            for definition in self.definitions
            if definition.name.startswith(("mcp__", "mcp_resource__", "mcp_prompt__"))
        )
        return before != after

    def close(self) -> None:
        self.mcp_bridge.close()
    def _mcp_tool_definitions(self) -> tuple[ToolDefinition, ...]:
        return self.mcp_bridge.discover()
    def _mcp_bridge_definitions(self, client: McpClient) -> tuple[ToolDefinition, ...]:
        return self.mcp_bridge.bridge_definitions(client)

    @property
    def _mcp_clients(self) -> dict[str, McpClient]:
        return self.mcp_bridge.clients

    @property
    def _mcp_tools(self) -> dict[str, tuple[McpClient, str]]:
        return self.mcp_bridge.tools

    @property
    def _mcp_resource_tools(self) -> dict[str, tuple[McpClient, str]]:
        return self.mcp_bridge.resource_tools

    @property
    def _mcp_prompt_tools(self) -> dict[str, tuple[McpClient, str]]:
        return self.mcp_bridge.prompt_tools
    @staticmethod
    def _limit(limits: object | None, name: str, default: int) -> int:
        if limits is None:
            return default
        if isinstance(limits, Mapping):
            value = limits.get(name, default)
        else:
            value = getattr(limits, name, default)
        value = int(value)
        if value <= 0:
            raise ValueError(f"{name} must be positive")
        return value

    @classmethod
    def _limit_alias(cls, limits: object | None, names: tuple[str, ...], default: int) -> int:
        if limits is None:
            return default
        for name in names:
            if isinstance(limits, Mapping) and name in limits:
                return cls._limit(limits, name, default)
            if not isinstance(limits, Mapping) and hasattr(limits, name):
                return cls._limit(limits, name, default)
        return default

    _tool_definitions = staticmethod(tool_definitions)

    def _walk_files(self, root: Path):
        return self.files.walk(root)
    def list_files(self, path: str = ".") -> str:
        return self.files.list_files(path)
    def read_file(self, path: str) -> str:
        return self.files.read_file(path)
    def search_text(
        self,
        query: str,
        path: str = ".",
        glob: str | None = None,
        case_sensitive: bool = True,
    ) -> str:
        return self.search.search_text(query, path, glob, case_sensitive)
    def _search_text_with_rg(
        self,
        *,
        query: str,
        root: Path,
        glob: str | None,
        case_sensitive: bool,
    ) -> str | None:
        return self.search.search_with_rg(
            query=query,
            root=root,
            glob=glob,
            case_sensitive=case_sensitive,
        )
    _header_path = staticmethod(UnifiedDiffValidator.header_path)
    _diff_git_paths = staticmethod(UnifiedDiffValidator.diff_git_paths)
    def _validate_unified_diff(self, patch: str) -> tuple[str, ...]:
        return self.patch_validator.validate(patch)
    _quoted_diff_path = staticmethod(PatchOperations.quoted_diff_path)
    def create_file(
        self,
        path: str,
        content: str,
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        return self.patching.create_file(path, content, timeout_seconds=timeout_seconds)
    @staticmethod
    def _effective_timeout(default: float, override: float | None) -> float:
        if override is None:
            return default
        if override <= 0:
            raise ToolExecutionError("harness timeout budget is exhausted")
        return min(default, override)

    def _git(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
        max_output_bytes: int | None = None,
        extra_env: Mapping[str, str] | None = None,
    ):
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
        env = sanitized_subprocess_env(task_home=self._task_home)
        if extra_env:
            env.update(extra_env)
        return run_bounded_command(
            ("git", *prefix, *argv),
            cwd=self.workspace,
            timeout_seconds=self._effective_timeout(30.0, timeout_seconds),
            max_output_chars=max_output_bytes or self.max_output_chars,
            env=env,
            stdin=stdin,
        )

    def apply_patch(self, patch: str, *, timeout_seconds: float | None = None) -> str:
        return self.patching.apply_patch(patch, timeout_seconds=timeout_seconds)
    @staticmethod
    def _atomic_replace_file(target: Path, payload: bytes, mode: int) -> None:
        AtomicFileWriter(new_id=lambda: uuid4().hex).replace(target, payload, mode)
    def replace_text(
        self,
        path: str,
        old_text: str,
        new_text: str,
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        return self.patching.replace_text(path, old_text, new_text, timeout_seconds=timeout_seconds)
    def _rollback_patch(self, patch: str, new_paths: Sequence[str]) -> None:
        return self.patching.rollback_patch(patch, new_paths)
    def run_check(self, name: str, *, timeout_seconds: float | None = None) -> VerificationOutcome:
        command = self.verification_commands.get(name)
        if command is None:
            raise ToolExecutionError(f"verification command is not allowlisted: {name!r}")
        started_at = time.monotonic()
        effective_timeout = self._effective_timeout(float(command.timeout_seconds), timeout_seconds)
        if tuple(command.argv) == ("git", "diff", "--check"):
            result = self._git(
                ("diff", "--check"),
                timeout_seconds=effective_timeout,
                max_output_bytes=self.max_output_chars,
            )
        else:
            command_env = sanitized_subprocess_env(task_home=self._task_home)
            sandbox = (
                resolve_command_sandbox(
                    profile=self._sandbox_profile,
                    backend=self._sandbox_backend,
                    cwd=self.workspace,
                    task_home=self._task_home,
                    extra_read_roots=self._sandbox_read_roots,
                )
                if self._sandbox_checks
                else None
            )
            result = run_bounded_command(
                tuple(command.argv),
                cwd=self.workspace,
                timeout_seconds=effective_timeout,
                max_output_chars=self.max_output_chars,
                env=command_env,
                sandbox=sandbox,
            )
            if (
                sandbox is not None
                and self._sandbox_backend == "auto"
                and result.returncode == 126
                and (
                    result.stderr.startswith("macOS sandbox-exec is unavailable")
                    or result.stderr.startswith("OS command sandbox is unavailable")
                )
            ):
                result = run_bounded_command(
                    tuple(command.argv),
                    cwd=self.workspace,
                    timeout_seconds=effective_timeout,
                    max_output_chars=self.max_output_chars,
                    env=command_env,
                )
        duration = time.monotonic() - started_at
        status = "passed" if result.ok else "failed"
        sections = [f"check {name!r} {status} (exit {result.returncode})"]
        if result.timed_out:
            sections.append(f"timed out after {command.timeout_seconds} seconds")
        output_findings = (
            *scan_text_for_secrets(result.stdout, path=f"run_check:{name}:stdout"),
            *scan_text_for_secrets(result.stderr, path=f"run_check:{name}:stderr"),
        )
        if result.stdout:
            sections.append(f"stdout:\n{redact_secrets(result.stdout)}")
        if result.stderr:
            sections.append(f"stderr:\n{redact_secrets(result.stderr)}")
        findings = tuple(output_findings)
        if findings:
            sections.append(
                "secret scan: redacted " + ", ".join(finding.label() for finding in findings)
            )
        outcome = VerificationOutcome(
            name=name,
            argv=tuple(command.argv),
            ok=result.ok,
            exit_code=result.returncode,
            duration_seconds=duration,
            output=bounded_text("\n".join(sections), self.max_output_chars),
        )
        self.verification_outcomes[name] = outcome
        return outcome

    def reviewable_patch(self, *, timeout_seconds: float | None = None) -> ReviewablePatch:
        if self.base_sha is not None:
            return self._reviewable_patch_pinned(timeout_seconds=timeout_seconds)
        budget = self._effective_timeout(30.0, timeout_seconds)
        deadline = time.monotonic() + budget

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise ToolExecutionError("reviewable_patch exceeded the harness timeout")
            return value

        result = self._git(
            (
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--no-renames",
                "--",
            ),
            timeout_seconds=remaining(),
            max_output_bytes=self.max_patch_bytes + 1,
        )
        if not result.ok:
            raise ToolExecutionError(f"git diff failed: {result.stderr.strip()}")
        if result.stdout_bytes > self.max_patch_bytes or result.stdout_truncated:
            raise ToolExecutionError(
                f"final patch exceeds {self.max_patch_bytes} bytes; refusing truncated artifact"
            )
        if len(result.stdout.splitlines()) > self.max_patch_lines:
            raise ToolExecutionError(f"final patch exceeds {self.max_patch_lines} lines")

        names = self._git(
            ("diff", "--name-only", "--no-renames", "-z", "--"),
            timeout_seconds=remaining(),
            max_output_bytes=self.max_output_chars,
        )
        if not names.ok:
            raise ToolExecutionError(f"git diff --name-only failed: {names.stderr.strip()}")
        if names.stdout_truncated:
            raise ToolExecutionError("changed path list exceeded the tool output limit")
        changed_paths = tuple(sorted(path for path in names.stdout.split("\x00") if path))
        if len(changed_paths) > self.max_changed_files:
            raise ToolExecutionError(f"final patch exceeds {self.max_changed_files} changed files")
        for path in changed_paths:
            self.policy.resolve(path)
        return ReviewablePatch(content=result.stdout, changed_paths=changed_paths)

    def workspace_fingerprint(self, *, timeout_seconds: float | None = None) -> str:
        """Return a Git tree id for tracked and non-ignored untracked workspace state.

        A temporary index captures content and executable-bit changes without
        mutating the repository's real index. Ignored build/cache artifacts are
        excluded so routine checks do not invalidate themselves.
        """

        budget = self._effective_timeout(30.0, timeout_seconds)
        deadline = time.monotonic() + budget

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise ToolExecutionError("workspace fingerprint exceeded the harness timeout")
            return value

        git_dir_result = self._git(("rev-parse", "--git-dir"), timeout_seconds=remaining())
        if not git_dir_result.ok:
            raise ToolExecutionError(
                f"could not resolve git dir: {git_dir_result.stderr.strip()}"
            )
        git_dir = Path(git_dir_result.stdout.strip())
        if not git_dir.is_absolute():
            git_dir = (self.workspace / git_dir).resolve(strict=True)
        fingerprint_index = git_dir / f"looplane-fingerprint-index-{uuid4().hex}"
        extra_env = {"GIT_INDEX_FILE": str(fingerprint_index)}
        try:
            read_tree = self._git(
                ("read-tree", "HEAD"),
                timeout_seconds=remaining(),
                extra_env=extra_env,
            )
            if not read_tree.ok:
                raise ToolExecutionError(
                    "could not initialize the workspace fingerprint index: "
                    + read_tree.stderr.strip()
                )
            added = self._git(
                ("add", "-A", "--", "."),
                timeout_seconds=remaining(),
                extra_env=extra_env,
                max_output_bytes=20_000,
            )
            if not added.ok:
                raise ToolExecutionError(
                    f"could not fingerprint workspace changes: {added.stderr.strip()}"
                )
            tree = self._git(
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

    def _reviewable_patch_pinned(self, *, timeout_seconds: float | None = None) -> ReviewablePatch:
        """Diff against an isolated index pinned to base_sha, never touching the real index.

        Used when ``self.workspace`` is the caller's real repository rather than a
        disposable clone: an unpinned ``git diff`` there would pick up whatever the
        real repo's ambient index happens to contain (unrelated staged/unstaged
        changes), so this reproduces every change since ``base_sha`` in a throwaway
        index instead.

        Paths present in ``self._preexisting_dirty_paths`` are excluded wholesale, not
        just from the reported patch content but from ``changed_paths`` and the
        allowed_paths check too: pre-existing dirt outside allowed_paths must not fail
        every subsequent tool call. The tradeoff is that if the agent further edits an
        already-dirty file, that file's changes are excluded from the report entirely
        rather than partially reported — a known limitation of diffing against a
        single base_sha snapshot rather than the file's content at run start.
        """

        assert self.base_sha is not None
        budget = self._effective_timeout(30.0, timeout_seconds)
        deadline = time.monotonic() + budget

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise ToolExecutionError("reviewable_patch exceeded the harness timeout")
            return value

        git_dir_result = self._git(("rev-parse", "--git-dir"), timeout_seconds=remaining())
        if not git_dir_result.ok:
            raise ToolExecutionError(f"could not resolve git dir: {git_dir_result.stderr.strip()}")
        git_dir = Path(git_dir_result.stdout.strip())
        if not git_dir.is_absolute():
            git_dir = (self.workspace / git_dir).resolve(strict=True)
        review_index = git_dir / f"looplane-review-index-{uuid4().hex}"
        extra_env = {"GIT_INDEX_FILE": str(review_index)}
        try:
            read_tree = self._git(
                ("read-tree", self.base_sha),
                timeout_seconds=remaining(),
                extra_env=extra_env,
            )
            if not read_tree.ok:
                raise ToolExecutionError(
                    f"could not initialize the isolated review index: {read_tree.stderr.strip()}"
                )
            added = self._git(
                ("add", "-A", "-f", "--", "."),
                timeout_seconds=remaining(),
                extra_env=extra_env,
                max_output_bytes=20_000,
            )
            if not added.ok:
                raise ToolExecutionError(
                    f"could not index workspace changes: {added.stderr.strip()}"
                )

            names = self._git(
                ("diff", "--cached", "--name-only", "--no-renames", "-z", self.base_sha, "--"),
                timeout_seconds=remaining(),
                extra_env=extra_env,
                max_output_bytes=self.max_output_chars,
            )
            if not names.ok:
                raise ToolExecutionError(f"git diff --name-only failed: {names.stderr.strip()}")
            if names.stdout_truncated:
                raise ToolExecutionError("changed path list exceeded the tool output limit")
            all_changed_paths = tuple(sorted(path for path in names.stdout.split("\x00") if path))
            # Pre-existing dirt (present before this run started) is not this agent's
            # change to report or be held to allowed_paths for: exclude it entirely.
            changed_paths = tuple(
                path for path in all_changed_paths if path not in self._preexisting_dirty_paths
            )
            if len(changed_paths) > self.max_changed_files:
                raise ToolExecutionError(
                    f"final patch exceeds {self.max_changed_files} changed files"
                )
            for path in changed_paths:
                self.policy.resolve(path)
            if not changed_paths:
                return ReviewablePatch(content="", changed_paths=())

            result = self._git(
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
                max_output_bytes=self.max_patch_bytes + 1,
            )
            if not result.ok:
                raise ToolExecutionError(f"git diff failed: {result.stderr.strip()}")
            if result.stdout_bytes > self.max_patch_bytes or result.stdout_truncated:
                raise ToolExecutionError(
                    f"final patch exceeds {self.max_patch_bytes} bytes; "
                    "refusing truncated artifact"
                )
            if len(result.stdout.splitlines()) > self.max_patch_lines:
                raise ToolExecutionError(f"final patch exceeds {self.max_patch_lines} lines")

            return ReviewablePatch(content=result.stdout, changed_paths=changed_paths)
        finally:
            review_index.unlink(missing_ok=True)

    def git_diff(self, *, timeout_seconds: float | None = None) -> str:
        return self.reviewable_patch(timeout_seconds=timeout_seconds).content

    def tool_program(
        self,
        steps: Sequence[Mapping[str, Any]],
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)):
            raise ToolExecutionError("steps must be an array")
        if not steps:
            raise ToolExecutionError("steps must not be empty")
        if len(steps) > self.max_tool_program_steps:
            raise ToolExecutionError(f"tool program exceeds {self.max_tool_program_steps} steps")
        handlers = {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "search_text": self.search_text,
            "git_diff": self.git_diff,
        }
        sections = ["[tool-program-v1]"]
        self._execute_structured_steps(
            steps,
            handlers=handlers,
            sections=sections,
            label="tool program",
            timeout_seconds=timeout_seconds,
        )
        return bounded_text("\n\n".join(sections), self.max_output_chars)

    def _nested_steps(self, value: Any, *, label: str) -> Sequence[Mapping[str, Any]]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ToolExecutionError(f"{label} must be an array")
        for step in value:
            if not isinstance(step, Mapping):
                raise ToolExecutionError(f"each {label} step must be an object")
        return value

    def _execute_structured_steps(
        self,
        steps: Sequence[Mapping[str, Any]],
        *,
        handlers: Mapping[str, Any],
        sections: list[str],
        label: str,
        timeout_seconds: float | None,
    ) -> None:
        remaining = self.max_tool_program_steps
        step_index = 0
        last_output = ""

        def consume(steps_to_run: Sequence[Mapping[str, Any]], *, depth: int) -> None:
            nonlocal last_output, remaining, step_index
            if depth > 3:
                raise ToolExecutionError(f"{label} control flow exceeds maximum depth")
            for step in steps_to_run:
                if not isinstance(step, Mapping):
                    raise ToolExecutionError(f"each {label} step must be an object")
                op = step.get("op")
                if not isinstance(op, str):
                    raise ToolExecutionError(f"{label} step op must be a string")
                if op == "repeat":
                    count = step.get("count")
                    if not isinstance(count, int) or count < 1:
                        raise ToolExecutionError(f"{label} repeat requires a positive count")
                    if count > self.max_tool_program_steps:
                        raise ToolExecutionError(
                            f"{label} repeat exceeds {self.max_tool_program_steps} iterations"
                        )
                    nested = self._nested_steps(step.get("steps"), label=f"{label} repeat")
                    for _ in range(count):
                        consume(nested, depth=depth + 1)
                    continue
                if op == "if_contains":
                    needle = step.get("contains")
                    if not isinstance(needle, str):
                        raise ToolExecutionError(f"{label} if_contains requires contains")
                    matched = needle in last_output
                    branch_key = "then_steps" if matched else "else_steps"
                    branch = step.get(branch_key, ())
                    nested = self._nested_steps(branch, label=f"{label} {branch_key}")
                    sections.append(f"## branch: if_contains\nmatched: {str(matched).lower()}")
                    consume(nested, depth=depth + 1)
                    continue
                if op not in handlers:
                    raise ToolExecutionError(f"unsupported {label} op: {op!r}")
                args = step.get("args", {})
                if not isinstance(args, Mapping):
                    raise ToolExecutionError(f"{label} step args must be an object")
                if "timeout_seconds" in args:
                    raise ToolExecutionError("timeout_seconds is controlled by the harness")
                if remaining <= 0:
                    raise ToolExecutionError(f"{label} exceeds {self.max_tool_program_steps} steps")
                remaining -= 1
                step_index += 1
                handler = handlers[op]
                if op in {
                    "create_file",
                    "replace_text",
                    "apply_patch",
                    "run_check",
                    "git_diff",
                }:
                    output = handler(**dict(args), timeout_seconds=timeout_seconds)
                else:
                    output = handler(**dict(args))
                if isinstance(output, VerificationOutcome):
                    content = output.model_dump_json()
                    if not output.ok:
                        raise ToolExecutionError(
                            f"verification failed: {output.name} (exit {output.exit_code})"
                        )
                else:
                    content = str(output)
                last_output = content
                sections.append(
                    f"## step {step_index}: {op}\n{bounded_text(content, self.max_output_chars)}"
                )

        consume(steps, depth=0)

    def _transaction_touched_paths(self, steps: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
        paths: set[str] = set()
        for step in steps:
            if not isinstance(step, Mapping):
                raise ToolExecutionError("each tool transaction step must be an object")
            op = step.get("op")
            args = step.get("args", {})
            if not isinstance(args, Mapping):
                raise ToolExecutionError("tool transaction step args must be an object")
            if op == "create_file":
                path = args.get("path")
                if not isinstance(path, str) or not path:
                    raise ToolExecutionError("create_file transaction step requires path")
                target = self.policy.resolve(path)
                paths.add(target.relative_to(self.workspace).as_posix())
            elif op == "replace_text":
                path = args.get("path")
                if not isinstance(path, str) or not path:
                    raise ToolExecutionError("replace_text transaction step requires path")
                target = self.policy.resolve(path)
                paths.add(target.relative_to(self.workspace).as_posix())
            elif op == "apply_patch":
                patch = args.get("patch")
                if not isinstance(patch, str):
                    raise ToolExecutionError("apply_patch transaction step requires patch")
                paths.update(self._validate_unified_diff(patch))
            elif op == "repeat":
                nested = self._nested_steps(step.get("steps"), label="tool transaction repeat")
                paths.update(self._transaction_touched_paths(nested))
            elif op == "if_contains":
                then_steps = self._nested_steps(
                    step.get("then_steps", ()), label="tool transaction then_steps"
                )
                else_steps = self._nested_steps(
                    step.get("else_steps", ()), label="tool transaction else_steps"
                )
                paths.update(self._transaction_touched_paths(then_steps))
                paths.update(self._transaction_touched_paths(else_steps))
        return tuple(sorted(paths))

    def _snapshot_paths(self, paths: Sequence[str]) -> dict[str, _PathSnapshot]:
        return self.snapshots.capture(paths)
    def _restore_snapshots(self, snapshots: Mapping[str, _PathSnapshot]) -> None:
        return self.snapshots.restore(snapshots)
    def tool_transaction(
        self,
        steps: Sequence[Mapping[str, Any]],
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)):
            raise ToolExecutionError("steps must be an array")
        if not steps:
            raise ToolExecutionError("steps must not be empty")
        if len(steps) > self.max_tool_program_steps:
            raise ToolExecutionError(
                f"tool transaction exceeds {self.max_tool_program_steps} steps"
            )
        handlers = {
            "read_file": self.read_file,
            "create_file": self.create_file,
            "replace_text": self.replace_text,
            "apply_patch": self.apply_patch,
            "run_check": self.run_check,
            "git_diff": self.git_diff,
        }
        touched_paths = self._transaction_touched_paths(steps)
        snapshots = self._snapshot_paths(touched_paths)
        sections = ["[tool-transaction-v1]"]
        try:
            self._execute_structured_steps(
                steps,
                handlers=handlers,
                sections=sections,
                label="tool transaction",
                timeout_seconds=timeout_seconds,
            )
        except (PathPolicyError, ToolExecutionError, OSError, TypeError, UnicodeError) as exc:
            try:
                self._restore_snapshots(snapshots)
            except (PathPolicyError, ToolExecutionError, OSError) as rollback_exc:
                raise ToolExecutionError(
                    f"tool transaction failed and rollback failed: {rollback_exc}"
                ) from exc
            raise ToolExecutionError(
                f"tool transaction failed and rolled back touched paths: {exc}"
            ) from exc
        return bounded_text("\n\n".join(sections), self.max_output_chars)

    def execute(self, call: ToolCall, *, timeout_seconds: float | None = None) -> ToolObservation:
        tool_call_id = str(call.tool_call_id)
        name = str(call.name)
        arguments: Any = call.arguments
        if not isinstance(arguments, Mapping):
            raise TypeError("ToolCall.arguments must be a mapping")

        handlers = {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "search_text": self.search_text,
            "create_file": self.create_file,
            "replace_text": self.replace_text,
            "apply_patch": self.apply_patch,
            "run_check": self.run_check,
            "git_diff": self.git_diff,
            "tool_program": self.tool_program,
            "tool_transaction": self.tool_transaction,
        }
        handler = handlers.get(name)
        if handler is None:
            mcp_resource_tool = self.mcp_bridge.resource_tools.get(name)
            if mcp_resource_tool is not None:
                try:
                    if "timeout_seconds" in arguments:
                        raise ToolExecutionError("timeout_seconds is controlled by the harness")
                    client, operation = mcp_resource_tool
                    if operation == "list":
                        content = client.list_resources(
                            timeout_seconds=self._effective_timeout(10.0, timeout_seconds)
                        )
                    else:
                        uri = arguments.get("uri")
                        if not isinstance(uri, str) or not uri:
                            raise ToolExecutionError("uri must be a non-empty string")
                        content = client.read_resource(
                            uri,
                            timeout_seconds=self._effective_timeout(30.0, timeout_seconds),
                        )
                    return ToolObservation(
                        tool_call_id=tool_call_id,
                        name=name,
                        ok=True,
                        content=bounded_text(content, self.max_output_chars),
                        error=None,
                    )
                except (McpError, ToolExecutionError, TypeError, OSError) as exc:
                    error = bounded_text(f"{type(exc).__name__}: {exc}", self.max_output_chars)
                    return ToolObservation(
                        tool_call_id=tool_call_id,
                        name=name,
                        ok=False,
                        content="",
                        error=error,
                    )
            mcp_prompt_tool = self.mcp_bridge.prompt_tools.get(name)
            if mcp_prompt_tool is not None:
                try:
                    if "timeout_seconds" in arguments:
                        raise ToolExecutionError("timeout_seconds is controlled by the harness")
                    client, operation = mcp_prompt_tool
                    if operation == "list":
                        content = client.list_prompts(
                            timeout_seconds=self._effective_timeout(10.0, timeout_seconds)
                        )
                    else:
                        prompt_name = arguments.get("name")
                        if not isinstance(prompt_name, str) or not prompt_name:
                            raise ToolExecutionError("name must be a non-empty string")
                        prompt_arguments = arguments.get("arguments")
                        if prompt_arguments is not None and not isinstance(
                            prompt_arguments, Mapping
                        ):
                            raise ToolExecutionError("arguments must be an object")
                        content = client.get_prompt(
                            prompt_name,
                            prompt_arguments,
                            timeout_seconds=self._effective_timeout(30.0, timeout_seconds),
                        )
                    return ToolObservation(
                        tool_call_id=tool_call_id,
                        name=name,
                        ok=True,
                        content=bounded_text(content, self.max_output_chars),
                        error=None,
                    )
                except (McpError, ToolExecutionError, TypeError, OSError) as exc:
                    error = bounded_text(f"{type(exc).__name__}: {exc}", self.max_output_chars)
                    return ToolObservation(
                        tool_call_id=tool_call_id,
                        name=name,
                        ok=False,
                        content="",
                        error=error,
                    )
            mcp_tool = self.mcp_bridge.tools.get(name)
            if mcp_tool is not None:
                try:
                    if "timeout_seconds" in arguments:
                        raise ToolExecutionError("timeout_seconds is controlled by the harness")
                    client, remote_tool = mcp_tool
                    ok, content, error = client.call_tool(
                        remote_tool,
                        arguments,
                        timeout_seconds=self._effective_timeout(30.0, timeout_seconds),
                    )
                    return ToolObservation(
                        tool_call_id=tool_call_id,
                        name=name,
                        ok=ok,
                        content=bounded_text(content, self.max_output_chars),
                        error=error,
                    )
                except (McpError, ToolExecutionError, TypeError, OSError) as exc:
                    error = bounded_text(f"{type(exc).__name__}: {exc}", self.max_output_chars)
                    return ToolObservation(
                        tool_call_id=tool_call_id,
                        name=name,
                        ok=False,
                        content="",
                        error=error,
                    )
            return ToolObservation(
                tool_call_id=tool_call_id,
                name=name,
                ok=False,
                content="",
                error=f"unknown tool: {name}",
            )
        try:
            call_arguments = dict(arguments)
            if "timeout_seconds" in call_arguments:
                raise ToolExecutionError("timeout_seconds is controlled by the harness")
            if name in {
                "create_file",
                "replace_text",
                "apply_patch",
                "run_check",
                "git_diff",
                "tool_program",
                "tool_transaction",
            }:
                result = handler(**call_arguments, timeout_seconds=timeout_seconds)
            else:
                result = handler(**call_arguments)
        except (PathPolicyError, ToolExecutionError, OSError, TypeError, UnicodeError) as exc:
            error = bounded_text(f"{type(exc).__name__}: {exc}", self.max_output_chars)
            return ToolObservation(
                tool_call_id=tool_call_id,
                name=name,
                ok=False,
                content="",
                error=error,
            )
        if isinstance(result, VerificationOutcome):
            content = bounded_text(result.model_dump_json(), self.max_output_chars)
            if not result.ok:
                return ToolObservation(
                    tool_call_id=tool_call_id,
                    name=name,
                    ok=False,
                    content=content,
                    error=f"verification failed: {result.name} (exit {result.exit_code})",
                )
        else:
            content = bounded_text(result, self.max_output_chars)
        return ToolObservation(
            tool_call_id=tool_call_id,
            name=name,
            ok=True,
            content=content,
            error=None,
        )
