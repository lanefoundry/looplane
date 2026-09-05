"""Compatibility imports and patchable ID factory for the canonical Codex session."""

import json as json
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from uuid import uuid4

from looplane.approvals import ApprovalDecision as ApprovalDecision
from looplane.conversation_runtime import ConversationProtocolError as ConversationProtocolError
from looplane.conversation_runtime import RuntimeToolStatus as RuntimeToolStatus
from looplane.runtimes.codex.approval_mapper import PendingApproval
from looplane.runtimes.codex.session import CodexAppServerSession as _CanonicalSession


class _PendingApproval(PendingApproval):
    """Historical name retained for callers constructing pending wire results."""


class CodexAppServerSession(_CanonicalSession):
    """Compatibility constructor retaining the module-level UUID patch point."""

    def __init__(
        self,
        *,
        working_directory: str | Path,
        runtime_workspace_roots: tuple[str | Path, ...] | None = None,
        executable: str | Path = "codex",
        model: str | None = None,
        sandbox_mode: Literal["read-only", "workspace-write"] = "read-only",
        request_timeout_seconds: float = 30.0,
        shutdown_timeout_seconds: float = 3.0,
        max_input_bytes: int = 128_000,
        max_frame_bytes: int = 256_000,
        max_frames: int = 20_000,
        host_env: Mapping[str, str] | None = None,
        allowed_mcp_servers: tuple[str, ...] = ("groundlane",),
    ) -> None:
        super().__init__(
            working_directory=working_directory,
            runtime_workspace_roots=runtime_workspace_roots,
            executable=executable,
            model=model,
            sandbox_mode=sandbox_mode,
            request_timeout_seconds=request_timeout_seconds,
            shutdown_timeout_seconds=shutdown_timeout_seconds,
            max_input_bytes=max_input_bytes,
            max_frame_bytes=max_frame_bytes,
            max_frames=max_frames,
            host_env=host_env,
            allowed_mcp_servers=allowed_mcp_servers,
            _new_id=lambda: uuid4().hex,
        )
