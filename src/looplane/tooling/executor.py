"""Canonical tool composition, registry and observation dispatch."""

from __future__ import annotations

import shutil
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from looplane.contracts import (
    ToolCall,
    ToolDefinition,
    ToolObservation,
    VerificationCommand,
    VerificationOutcome,
)
from looplane.execution.capture import bounded_text
from looplane.execution.environment import sanitized_subprocess_env
from looplane.execution.local_process import run_local_process
from looplane.execution.types import CommandResult
from looplane.mcp_client import (
    HttpMcpClient,
    McpError,
    NativeMcpServerConfig,
    StdioMcpClient,
    native_mcp_prompt_tool_name,
    native_mcp_resource_tool_name,
    split_native_mcp_tool_name,
)
from looplane.policy import PathPolicyError, SafePathPolicy
from looplane.sandbox.policy import resolve_command_sandbox
from looplane.tooling.definitions import tool_definitions
from looplane.tooling.filesystem import OutputLimits, ReadLimits, WorkspaceFiles
from looplane.tooling.git import WorkspaceGit
from looplane.tooling.mcp_bridge import McpBridge, McpClient, McpToolNames
from looplane.tooling.patch_validation import PatchLimits, UnifiedDiffValidator
from looplane.tooling.patching import PatchOperations
from looplane.tooling.read_versions import ReadVersionStore
from looplane.tooling.search import SearchLimits, WorkspaceSearch
from looplane.tooling.snapshots import AtomicFileWriter, WorkspaceSnapshots
from looplane.tooling.timeouts import effective_timeout
from looplane.tooling.transactions import ProgramLimits, StructuredPrograms
from looplane.tooling.types import ReviewablePatch, ToolExecutionError, _PathSnapshot
from looplane.tooling.verification import AuthorizedChecks, VerificationSandboxSettings
from looplane.workspace.local_git import LocalGitWorkspace


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
        self._program_limits = ProgramLimits()
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
            limits, ("max_tool_output_bytes", "max_output_chars"), 200_000,
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
        self.git = WorkspaceGit(
            policy=self.policy,
            output_limits=self._output_limits,
            patch_limits=self._patch_limits,
            task_home=self._task_home,
            git_dir=self.git_dir,
            base_sha=self.base_sha,
            preexisting_dirty_paths=self._preexisting_dirty_paths,
            run_command=self._run_command,
            environment=self._environment,
            clock=self._clock,
            new_id=self._new_id,
        )
        self.checks = AuthorizedChecks(
            git=self.git,
            verification_commands=verification_commands,
            sandbox=VerificationSandboxSettings(
                enabled=self._sandbox_checks,
                profile=self._sandbox_profile,
                backend=self._sandbox_backend,
                read_roots=self._sandbox_read_roots,
            ),
            run_command=self._run_command,
            environment=self._environment,
            resolve_sandbox=self._resolve_sandbox,
            clock=self._clock,
            bound=self._bound,
        )
        self.atomic_writer = AtomicFileWriter(new_id=self._new_id)
        self.files = WorkspaceFiles(
            policy=self.policy,
            versions=self.read_versions,
            read_limits=self._read_limits,
            output_limits=self._output_limits,
            bound=self._bound,
        )
        self.search = WorkspaceSearch(
            files=self.files,
            search_limits=self._search_limits,
            read_limits=self._read_limits,
            output_limits=self._output_limits,
            task_home=self._task_home,
            run_command=self._run_command,
            environment=self._environment,
            which=self._which,
            bound=self._bound,
        )
        self.patch_validator = UnifiedDiffValidator(policy=self.policy, limits=self._patch_limits)
        self.snapshots = WorkspaceSnapshots(
            policy=self.policy,
            versions=self.read_versions,
            atomic_write=self.atomic_writer.replace,
            reset_index=self.git.reset_paths,
        )
        self.patching = PatchOperations(
            policy=self.policy,
            versions=self.read_versions,
            validator=self.patch_validator,
            read_limits=self._read_limits,
            patch_limits=self._patch_limits,
            atomic_write=self.atomic_writer.replace,
            git=self.git.run,
            review=self.git.reviewable_patch,
            effective_timeout=effective_timeout,
            clock=self._clock,
        )
        self.programs = StructuredPrograms(
            files=self.files,
            search=self.search,
            patching=self.patching,
            validator=self.patch_validator,
            snapshots=self.snapshots,
            git=self.git,
            checks=self.checks,
            limits=self._program_limits,
            output_limits=self._output_limits,
            bound=self._bound,
        )
        self.definitions = self._build_definitions()

    # Static hooks are functions, not callbacks retaining this composition object.
    # Defaults are canonical; the old facade supplies explicit legacy wrappers.
    _run_command = staticmethod(run_local_process)
    _environment = staticmethod(sanitized_subprocess_env)
    _resolve_sandbox = staticmethod(resolve_command_sandbox)
    _clock = staticmethod(time.monotonic)
    _which = staticmethod(shutil.which)
    _bound = staticmethod(bounded_text)

    @staticmethod
    def _new_id() -> str:
        return uuid4().hex

    @property
    def max_tool_program_steps(self) -> int:
        return self._program_limits.max_tool_program_steps

    @max_tool_program_steps.setter
    def max_tool_program_steps(self, value: int) -> None:
        self._program_limits.max_tool_program_steps = value

    @property
    def verification_commands(self) -> dict[str, VerificationCommand]:
        return self.checks.commands

    @verification_commands.setter
    def verification_commands(self, value: dict[str, VerificationCommand]) -> None:
        self.checks.commands = value

    @property
    def verification_outcomes(self) -> dict[str, VerificationOutcome]:
        return self.checks.outcomes

    @verification_outcomes.setter
    def verification_outcomes(self, value: dict[str, VerificationOutcome]) -> None:
        self.checks.outcomes = value

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
    _effective_timeout = staticmethod(effective_timeout)

    def _git(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
        max_output_bytes: int | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        return self.git.run(
            argv, stdin=stdin, timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes, extra_env=extra_env,
        )

    def apply_patch(self, patch: str, *, timeout_seconds: float | None = None) -> str:
        return self.patching.apply_patch(patch, timeout_seconds=timeout_seconds)
    @staticmethod
    def _atomic_replace_file(target: Path, payload: bytes, mode: int) -> None:
        AtomicFileWriter().replace(target, payload, mode)

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
    def run_check(
        self, name: str, *, timeout_seconds: float | None = None,
    ) -> VerificationOutcome:
        return self.checks.run_check(name, timeout_seconds=timeout_seconds)

    def reviewable_patch(
        self, *, timeout_seconds: float | None = None,
    ) -> ReviewablePatch:
        return self.git.reviewable_patch(timeout_seconds=timeout_seconds)

    def workspace_fingerprint(self, *, timeout_seconds: float | None = None) -> str:
        return self.git.workspace_fingerprint(timeout_seconds=timeout_seconds)

    def _reviewable_patch_pinned(
        self, *, timeout_seconds: float | None = None,
    ) -> ReviewablePatch:
        return self.git._reviewable_patch_pinned(timeout_seconds=timeout_seconds)

    def git_diff(self, *, timeout_seconds: float | None = None) -> str:
        return self.git.git_diff(timeout_seconds=timeout_seconds)

    def tool_program(
        self, steps: Sequence[Mapping[str, Any]], *,
        timeout_seconds: float | None = None,
    ) -> str:
        return self.programs.tool_program(steps, timeout_seconds=timeout_seconds)

    _nested_steps = staticmethod(StructuredPrograms.nested_steps)

    def _transaction_touched_paths(
        self, steps: Sequence[Mapping[str, Any]],
    ) -> tuple[str, ...]:
        return self.programs.touched_paths(steps)

    def _snapshot_paths(self, paths: Sequence[str]) -> dict[str, _PathSnapshot]:
        return self.snapshots.capture(paths)

    def _restore_snapshots(self, snapshots: Mapping[str, _PathSnapshot]) -> None:
        self.snapshots.restore(snapshots)

    def tool_transaction(
        self, steps: Sequence[Mapping[str, Any]], *,
        timeout_seconds: float | None = None,
    ) -> str:
        return self.programs.tool_transaction(steps, timeout_seconds=timeout_seconds)

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
                        content=self._bound(content, self.max_output_chars),
                        error=None,
                    )
                except (McpError, ToolExecutionError, TypeError, OSError) as exc:
                    error = self._bound(f"{type(exc).__name__}: {exc}", self.max_output_chars)
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
                        content=self._bound(content, self.max_output_chars),
                        error=None,
                    )
                except (McpError, ToolExecutionError, TypeError, OSError) as exc:
                    error = self._bound(f"{type(exc).__name__}: {exc}", self.max_output_chars)
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
                        content=self._bound(content, self.max_output_chars),
                        error=error,
                    )
                except (McpError, ToolExecutionError, TypeError, OSError) as exc:
                    error = self._bound(f"{type(exc).__name__}: {exc}", self.max_output_chars)
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
            error = self._bound(f"{type(exc).__name__}: {exc}", self.max_output_chars)
            return ToolObservation(
                tool_call_id=tool_call_id,
                name=name,
                ok=False,
                content="",
                error=error,
            )
        if isinstance(result, VerificationOutcome):
            content = self._bound(result.model_dump_json(), self.max_output_chars)
            if not result.ok:
                return ToolObservation(
                    tool_call_id=tool_call_id,
                    name=name,
                    ok=False,
                    content=content,
                    error=f"verification failed: {result.name} (exit {result.exit_code})",
                )
        else:
            content = self._bound(result, self.max_output_chars)
        return ToolObservation(
            tool_call_id=tool_call_id,
            name=name,
            ok=True,
            content=content,
            error=None,
        )
