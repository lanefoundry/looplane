"""Compatibility entry point for the canonical tooling executor.

Only explicit legacy dependency wrappers live here. Canonical defaults never
import this facade or consult its globals.
"""

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
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from looplane.contracts import (
    ToolCall as ToolCall,
)
from looplane.contracts import (
    ToolDefinition as ToolDefinition,
)
from looplane.contracts import (
    ToolObservation as ToolObservation,
)
from looplane.contracts import (
    VerificationCommand as VerificationCommand,
)
from looplane.contracts import (
    VerificationOutcome as VerificationOutcome,
)
from looplane.execution.types import CommandResult
from looplane.mcp_client import (
    HttpMcpClient as HttpMcpClient,
)
from looplane.mcp_client import (
    McpError as McpError,
)
from looplane.mcp_client import (
    NativeMcpServerConfig as NativeMcpServerConfig,
)
from looplane.mcp_client import (
    StdioMcpClient as StdioMcpClient,
)
from looplane.mcp_client import (
    native_mcp_prompt_tool_name as native_mcp_prompt_tool_name,
)
from looplane.mcp_client import (
    native_mcp_resource_tool_name as native_mcp_resource_tool_name,
)
from looplane.mcp_client import (
    split_native_mcp_tool_name as split_native_mcp_tool_name,
)
from looplane.policy import PathPolicyError as PathPolicyError
from looplane.policy import SafePathPolicy as SafePathPolicy
from looplane.runtime import (
    LocalGitWorkspace as LocalGitWorkspace,
)
from looplane.runtime import (
    bounded_text as bounded_text,
)
from looplane.runtime import (
    resolve_command_sandbox as resolve_command_sandbox,
)
from looplane.runtime import (
    run_bounded_command as run_bounded_command,
)
from looplane.runtime import (
    sanitized_subprocess_env as sanitized_subprocess_env,
)
from looplane.secret_scan import (
    redact_secrets as redact_secrets,
)
from looplane.secret_scan import (
    scan_text_for_secrets as scan_text_for_secrets,
)
from looplane.tooling.definitions import tool_definitions as tool_definitions
from looplane.tooling.executor import ToolExecutor as _ToolExecutor
from looplane.tooling.filesystem import (
    OutputLimits as OutputLimits,
)
from looplane.tooling.filesystem import (
    ReadLimits as ReadLimits,
)
from looplane.tooling.filesystem import (
    WorkspaceFiles as WorkspaceFiles,
)
from looplane.tooling.mcp_bridge import (
    McpBridge as McpBridge,
)
from looplane.tooling.mcp_bridge import (
    McpClient as McpClient,
)
from looplane.tooling.mcp_bridge import (
    McpToolNames as McpToolNames,
)
from looplane.tooling.patch_validation import (
    PatchLimits as PatchLimits,
)
from looplane.tooling.patch_validation import (
    UnifiedDiffValidator as UnifiedDiffValidator,
)
from looplane.tooling.patching import PatchOperations as PatchOperations
from looplane.tooling.read_versions import ReadVersionStore as ReadVersionStore
from looplane.tooling.search import SearchLimits as SearchLimits
from looplane.tooling.search import WorkspaceSearch as WorkspaceSearch
from looplane.tooling.snapshots import (
    AtomicFileWriter as AtomicFileWriter,
)
from looplane.tooling.snapshots import (
    WorkspaceSnapshots as WorkspaceSnapshots,
)
from looplane.tooling.types import (
    ReviewablePatch as ReviewablePatch,
)
from looplane.tooling.types import (
    ToolExecutionError as ToolExecutionError,
)
from looplane.tooling.types import (
    _PathSnapshot as _PathSnapshot,
)


class ToolExecutor(_ToolExecutor):
    """Old import path, preserving module-level dependency interception."""

    @staticmethod
    def _run_command(argv: Sequence[str], **options: Any) -> CommandResult:
        return run_bounded_command(argv, **options)

    @staticmethod
    def _environment(*, task_home: Path | None = None) -> dict[str, str]:
        return sanitized_subprocess_env(task_home=task_home)

    @staticmethod
    def _resolve_sandbox(**options: Any):
        return resolve_command_sandbox(**options)

    @staticmethod
    def _clock() -> float:
        return time.monotonic()

    @staticmethod
    def _which(name: str) -> str | None:
        return shutil.which(name)

    @staticmethod
    def _bound(value: str, limit: int) -> str:
        return bounded_text(value, limit)

    @staticmethod
    def _new_id() -> str:
        return uuid4().hex

    _tool_definitions = staticmethod(tool_definitions)

    @staticmethod
    def _atomic_replace_file(target: Path, payload: bytes, mode: int) -> None:
        AtomicFileWriter(new_id=lambda: uuid4().hex).replace(target, payload, mode)

    def _mcp_client(self, config: NativeMcpServerConfig) -> McpClient:
        if config.url is not None:
            return HttpMcpClient(config, max_output_chars=self.max_output_chars)
        return StdioMcpClient(
            config,
            cwd=self.workspace,
            task_home=self._task_home,
            max_output_chars=self.max_output_chars,
        )
