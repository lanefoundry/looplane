from __future__ import annotations

import fnmatch
import hashlib
import os
import shlex
import shutil
import stat
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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


class ToolExecutionError(RuntimeError):
    """A bounded, user-visible tool failure."""


@dataclass(frozen=True)
class ReviewablePatch:
    content: str
    changed_paths: tuple[str, ...]


class ToolExecutor:
    def __init__(
        self,
        workspace: Path | LocalGitWorkspace,
        policy: SafePathPolicy,
        verification_commands: Sequence[VerificationCommand],
        limits: object | None = None,
        *,
        git_dir: Path | None = None,
        mcp_servers: Sequence[NativeMcpServerConfig] = (),
        sandbox_checks: bool = False,
        sandbox_profile: str | None = None,
        sandbox_read_roots: Sequence[Path] = (),
    ) -> None:
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
        self._task_home = self.workspace.parent / ".check-task-env"
        self._sandbox_checks = sandbox_checks
        self._sandbox_profile = sandbox_profile or "verification"
        self._sandbox_read_roots = tuple(Path(root) for root in sandbox_read_roots)
        self._read_versions: dict[str, str] = {}
        self._mcp_clients: dict[str, StdioMcpClient] = {
            config.name: self._mcp_client(config)
            for config in mcp_servers
        }
        self._mcp_tools: dict[str, tuple[StdioMcpClient, str]] = {}
        self._mcp_resource_tools: dict[str, tuple[StdioMcpClient, str]] = {}
        self._mcp_prompt_tools: dict[str, tuple[StdioMcpClient, str]] = {}

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
        self.definitions = self._build_definitions()

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
        self._mcp_tools.clear()
        self._mcp_resource_tools.clear()
        self._mcp_prompt_tools.clear()
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
        for client in self._mcp_clients.values():
            client.close()

    def _mcp_tool_definitions(self) -> tuple[ToolDefinition, ...]:
        definitions: list[ToolDefinition] = []
        for client in self._mcp_clients.values():
            definitions.extend(self._mcp_bridge_definitions(client))
            for definition in client.tool_definitions():
                split = split_native_mcp_tool_name(definition.name)
                if split is None:
                    continue
                _server, remote_tool = split
                self._mcp_tools[definition.name] = (client, remote_tool)
                definitions.append(definition)
        return tuple(definitions)

    def _mcp_bridge_definitions(self, client: StdioMcpClient) -> tuple[ToolDefinition, ...]:
        server_name = client.config.name
        resource_list = native_mcp_resource_tool_name(server_name, "list")
        resource_read = native_mcp_resource_tool_name(server_name, "read")
        prompt_list = native_mcp_prompt_tool_name(server_name, "list")
        prompt_get = native_mcp_prompt_tool_name(server_name, "get")
        self._mcp_resource_tools[resource_list] = (client, "list")
        self._mcp_resource_tools[resource_read] = (client, "read")
        self._mcp_prompt_tools[prompt_list] = (client, "list")
        self._mcp_prompt_tools[prompt_get] = (client, "get")
        return (
            ToolDefinition(
                name=resource_list,
                description=f"List MCP resources exposed by server {server_name!r}.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                read_only=True,
                concurrency_safe=True,
            ),
            ToolDefinition(
                name=resource_read,
                description=f"Read one MCP resource URI from server {server_name!r}.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "uri": {
                            "type": "string",
                            "minLength": 1,
                            "description": "MCP resource URI returned by the server.",
                        }
                    },
                    "required": ["uri"],
                    "additionalProperties": False,
                },
                read_only=True,
                concurrency_safe=True,
            ),
            ToolDefinition(
                name=prompt_list,
                description=f"List MCP prompts exposed by server {server_name!r}.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                read_only=True,
                concurrency_safe=True,
            ),
            ToolDefinition(
                name=prompt_get,
                description=f"Get one MCP prompt from server {server_name!r}.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "minLength": 1},
                        "arguments": {
                            "type": "object",
                            "additionalProperties": True,
                            "default": {},
                        },
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
                read_only=True,
                concurrency_safe=True,
            ),
        )

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

    @staticmethod
    def _tool_definitions() -> tuple[ToolDefinition, ...]:
        path = {"type": "string", "description": "Workspace-relative path."}
        return (
            ToolDefinition(
                name="list_files",
                description=(
                    "List allowed files below a workspace-relative path. Use this to discover "
                    "file names before reading. It is read-only and bounded; do not use it when "
                    "you already know the exact file and can call read_file directly."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"path": {**path, "default": "."}},
                    "additionalProperties": False,
                },
                read_only=True,
                concurrency_safe=True,
            ),
            ToolDefinition(
                name="read_file",
                description=(
                    "Read one allowed UTF-8 text file with a bounded result. Use this before "
                    "replace_text and whenever exact source text matters. Do not use shell "
                    "commands to inspect file contents."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"path": path},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                read_only=True,
                concurrency_safe=True,
            ),
            ToolDefinition(
                name="search_text",
                description=(
                    "Search allowed files for a literal text string, respecting .gitignore when "
                    "ripgrep is available. Use it to locate symbols or exact snippets before "
                    "reading files. It is not a regex search and returns bounded path:line:text "
                    "matches."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1},
                        "path": {**path, "default": "."},
                        "glob": {"type": ["string", "null"]},
                        "case_sensitive": {"type": "boolean", "default": True},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                read_only=True,
                concurrency_safe=True,
            ),
            ToolDefinition(
                name="replace_text",
                description=(
                    "Replace an exact text fragment in one existing UTF-8 file. Read the file "
                    "first. Prefer this for small edits; old_text must occur exactly once. "
                    "Correct example: copy old_text directly from read_file, preserving spaces "
                    "and newlines. Do not use it for new files, deletions, multi-hunk edits, or "
                    "guessed text."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": path,
                        "old_text": {"type": "string", "minLength": 1},
                        "new_text": {"type": "string"},
                    },
                    "required": ["path", "old_text", "new_text"],
                    "additionalProperties": False,
                },
            ),
            ToolDefinition(
                name="apply_patch",
                description=(
                    "Apply one bounded unified text diff after path and git checks. Use this "
                    "for multi-hunk edits, new files, and deletions. The patch must include "
                    "diff --git, ---/+++ file headers, and @@ hunks; do not use it for a small "
                    "single exact replacement where replace_text is safer."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"patch": {"type": "string", "minLength": 1}},
                    "required": ["patch"],
                    "additionalProperties": False,
                },
            ),
            ToolDefinition(
                name="run_check",
                description=(
                    "Run one exact argv verification command selected by its allowlisted name. "
                    "The allowed names come from the task contract, and the harness controls "
                    "timeouts. Do not invent commands or pass shell syntax."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "enum": [],
                            "description": "Allowlisted verification command name.",
                        }
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            ),
            ToolDefinition(
                name="git_diff",
                description=(
                    "Return the bounded uncommitted workspace patch for review. Use it after "
                    "edits when you need to inspect the cumulative diff; it is read-only."
                ),
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                read_only=True,
                concurrency_safe=True,
            ),
        )

    def _walk_files(self, root: Path):
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
        for file_path in self._walk_files(root):
            files.append(file_path.relative_to(self.workspace).as_posix())
            if len(files) >= self.max_list_files:
                files.append(f"... file list truncated at {self.max_list_files} entries ...")
                break
        return bounded_text("\n".join(files), self.max_output_chars)

    def read_file(self, path: str) -> str:
        target = self.policy.resolve(path)
        if not target.is_file():
            raise ToolExecutionError(f"not a regular file: {path}")
        with target.open("rb") as handle:
            data = handle.read(self.max_read_bytes + 1)
        truncated = len(data) > self.max_read_bytes
        visible = data[: self.max_read_bytes]
        text = visible.decode("utf-8", errors="replace")
        if not truncated:
            relative = target.relative_to(self.workspace).as_posix()
            self._read_versions[relative] = hashlib.sha256(visible).hexdigest()
        if truncated:
            text += f"\n... file truncated at {self.max_read_bytes} bytes ..."
        return bounded_text(text, self.max_output_chars)

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
        rg_result = self._search_text_with_rg(
            query=query,
            root=root,
            glob=glob,
            case_sensitive=case_sensitive,
        )
        if rg_result is not None:
            return rg_result
        needle = query if case_sensitive else query.casefold()
        matches: list[str] = []
        for file_path in self._walk_files(root):
            relative = file_path.relative_to(self.workspace).as_posix()
            if glob and not fnmatch.fnmatchcase(relative, glob):
                continue
            try:
                with file_path.open("rb") as handle:
                    data = handle.read(self.max_read_bytes + 1)
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
                    if len(matches) >= self.max_search_results:
                        matches.append(
                            f"... search truncated at {self.max_search_results} matches ..."
                        )
                        return bounded_text("\n".join(matches), self.max_output_chars)
        return bounded_text("\n".join(matches), self.max_output_chars)

    def _search_text_with_rg(
        self,
        *,
        query: str,
        root: Path,
        glob: str | None,
        case_sensitive: bool,
    ) -> str | None:
        if shutil.which("rg") is None:
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
        result = run_bounded_command(
            tuple(argv),
            cwd=self.workspace,
            timeout_seconds=10.0,
            max_output_chars=self.max_output_chars,
            env=sanitized_subprocess_env(task_home=self._task_home),
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
            if len(matches) >= self.max_search_results:
                matches.append(f"... search truncated at {self.max_search_results} matches ...")
                break
        return bounded_text("\n".join(matches), self.max_output_chars)

    @staticmethod
    def _header_path(line: str, marker: str) -> str | None:
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

    def _validate_unified_diff(self, patch: str) -> tuple[str, ...]:
        if not isinstance(patch, str) or not patch.strip():
            raise ToolExecutionError("patch must be a non-empty unified diff")
        if len(patch.encode("utf-8")) > self.max_patch_bytes:
            raise ToolExecutionError(f"patch exceeds {self.max_patch_bytes} bytes")
        lines = patch.splitlines()
        if len(lines) > self.max_patch_lines:
            raise ToolExecutionError(f"patch exceeds {self.max_patch_lines} lines")
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

        old_headers = [line for line in lines if line.startswith("--- ")]
        new_headers = [line for line in lines if line.startswith("+++ ")]
        if (
            not old_headers
            or len(old_headers) != len(new_headers)
            or not any(line.startswith("@@ ") for line in lines)
        ):
            raise ToolExecutionError("apply_patch accepts unified text diffs only")

        paths: set[str] = set()
        for line in (*old_headers, *new_headers):
            marker = "--- " if line.startswith("--- ") else "+++ "
            path = self._header_path(line, marker)
            if path is not None:
                self.policy.resolve(path)
                paths.add(path)
        if not paths:
            raise ToolExecutionError("patch does not name a workspace file")
        if len(paths) > self.max_changed_files:
            raise ToolExecutionError(f"patch exceeds {self.max_changed_files} changed files")
        return tuple(sorted(paths))

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
        return run_bounded_command(
            ("git", *prefix, *argv),
            cwd=self.workspace,
            timeout_seconds=self._effective_timeout(30.0, timeout_seconds),
            max_output_chars=max_output_bytes or self.max_output_chars,
            env=sanitized_subprocess_env(task_home=self._task_home),
            stdin=stdin,
        )

    def apply_patch(self, patch: str, *, timeout_seconds: float | None = None) -> str:
        paths = self._validate_unified_diff(patch)
        new_paths = tuple(path for path in paths if not (self.workspace / path).exists())
        budget = self._effective_timeout(30.0, timeout_seconds)
        deadline = time.monotonic() + budget

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise ToolExecutionError("apply_patch exceeded the harness timeout")
            return value

        checked = self._git(
            ("apply", "--check", "--whitespace=error-all", "-"),
            stdin=patch,
            timeout_seconds=remaining(),
        )
        if not checked.ok:
            raise ToolExecutionError(f"git apply --check failed: {checked.stderr.strip()}")
        applied = self._git(
            ("apply", "--whitespace=error-all", "-"),
            stdin=patch,
            timeout_seconds=remaining(),
        )
        if not applied.ok:
            raise ToolExecutionError(f"git apply failed: {applied.stderr.strip()}")
        for path in new_paths:
            try:
                intent = self._git(
                    ("add", "--intent-to-add", "--", path),
                    timeout_seconds=remaining(),
                )
            except ToolExecutionError as exc:
                self._rollback_patch(patch, new_paths)
                raise ToolExecutionError(
                    f"could not register new file for reviewable diff: {exc}"
                ) from exc
            if not intent.ok:
                self._rollback_patch(patch, new_paths)
                raise ToolExecutionError(
                    f"could not register new file for reviewable diff: {intent.stderr.strip()}"
                )
        try:
            self.reviewable_patch(timeout_seconds=remaining())
        except ToolExecutionError as exc:
            self._rollback_patch(patch, new_paths)
            raise ToolExecutionError(
                f"cumulative patch is not reviewable within the task limits: {exc}"
            ) from exc
        return f"applied unified diff to {len(paths)} file(s):\n" + "\n".join(paths)

    @staticmethod
    def _atomic_replace_file(target: Path, payload: bytes, mode: int) -> None:
        temporary = target.with_name(f".{target.name}.rivumi-replace-{uuid4().hex}")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode, follow_symlinks=False)
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

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
        if argument_bytes > self.max_patch_bytes:
            raise ToolExecutionError(f"replacement arguments exceed {self.max_patch_bytes} bytes")

        target = self.policy.resolve(path)
        if not target.is_file():
            raise ToolExecutionError(f"not a regular file: {path}")
        with target.open("rb") as handle:
            original = handle.read(self.max_read_bytes + 1)
        if len(original) > self.max_read_bytes:
            raise ToolExecutionError(f"file exceeds {self.max_read_bytes} readable bytes")
        if b"\x00" in original:
            raise ToolExecutionError("replace_text accepts UTF-8 text files only")
        try:
            source = original.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ToolExecutionError("replace_text accepts UTF-8 text files only") from exc
        relative = target.relative_to(self.workspace).as_posix()
        read_version = self._read_versions.get(relative)
        current_version = hashlib.sha256(original).hexdigest()
        if read_version is None:
            raise ToolExecutionError("read_file must be called before replace_text")
        if read_version != current_version:
            raise ToolExecutionError("file changed after read_file; read it again before editing")
        observed = source.count(old_text)
        if observed != 1:
            raise ToolExecutionError(f"exact replacement requires one match; observed {observed}")
        updated = source.replace(old_text, new_text, 1).encode("utf-8")
        if len(updated) > self.max_read_bytes:
            raise ToolExecutionError(f"resulting file exceeds {self.max_read_bytes} bytes")

        mode = stat.S_IMODE(target.stat().st_mode)
        budget = self._effective_timeout(30.0, timeout_seconds)
        deadline = time.monotonic() + budget

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise ToolExecutionError("replace_text exceeded the harness timeout")
            return value

        tracked = self._git(
            ("ls-files", "--error-unmatch", "--", relative),
            timeout_seconds=remaining(),
        )
        if not tracked.ok:
            raise ToolExecutionError(
                "replace_text requires a Git-tracked file; use apply_patch to create a file"
            )

        try:
            self._atomic_replace_file(target, updated, mode)
            whitespace = self._git(
                ("diff", "--check", "--", relative),
                timeout_seconds=remaining(),
            )
            if not whitespace.ok:
                raise ToolExecutionError(
                    f"replacement introduces whitespace errors: {whitespace.stderr.strip()}"
                )
            self.reviewable_patch(timeout_seconds=remaining())
        except (OSError, ToolExecutionError) as exc:
            try:
                self._atomic_replace_file(target, original, mode)
            except OSError as rollback_exc:
                try:
                    with target.open("rb") as handle:
                        restored = handle.read(self.max_read_bytes + 1) == original
                    restored_mode = stat.S_IMODE(target.stat().st_mode) == mode
                except OSError:
                    restored = False
                    restored_mode = False
                if not restored or not restored_mode:
                    raise ToolExecutionError(
                        f"replacement rollback failed: {rollback_exc}"
                    ) from exc
            raise ToolExecutionError(f"replacement was refused and rolled back: {exc}") from exc
        self._read_versions[relative] = hashlib.sha256(updated).hexdigest()
        return f"replaced one exact text fragment in {path}"

    def _rollback_patch(self, patch: str, new_paths: Sequence[str]) -> None:
        reversed_patch = self._git(
            ("apply", "--reverse", "--whitespace=nowarn", "-"),
            stdin=patch,
            timeout_seconds=5.0,
        )
        reset = self._git(
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
            result = run_bounded_command(
                tuple(command.argv),
                cwd=self.workspace,
                timeout_seconds=effective_timeout,
                max_output_chars=self.max_output_chars,
                env=sanitized_subprocess_env(task_home=self._task_home),
                sandbox=(
                    resolve_command_sandbox(
                        profile=self._sandbox_profile,
                        cwd=self.workspace,
                        task_home=self._task_home,
                        extra_read_roots=self._sandbox_read_roots,
                    )
                    if self._sandbox_checks
                    else None
                ),
            )
        duration = time.monotonic() - started_at
        status = "passed" if result.ok else "failed"
        sections = [f"check {name!r} {status} (exit {result.returncode})"]
        if result.timed_out:
            sections.append(f"timed out after {command.timeout_seconds} seconds")
        if result.stdout:
            sections.append(f"stdout:\n{result.stdout}")
        if result.stderr:
            sections.append(f"stderr:\n{result.stderr}")
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

    def git_diff(self, *, timeout_seconds: float | None = None) -> str:
        return self.reviewable_patch(timeout_seconds=timeout_seconds).content

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
            "replace_text": self.replace_text,
            "apply_patch": self.apply_patch,
            "run_check": self.run_check,
            "git_diff": self.git_diff,
        }
        handler = handlers.get(name)
        if handler is None:
            mcp_resource_tool = self._mcp_resource_tools.get(name)
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
            mcp_prompt_tool = self._mcp_prompt_tools.get(name)
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
            mcp_tool = self._mcp_tools.get(name)
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
            if name in {"replace_text", "apply_patch", "run_check", "git_diff"}:
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
