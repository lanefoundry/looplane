"""Long-lived Codex app-server adapter for unified Rivumi conversations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import tomllib
from collections import deque
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from rivumi.approvals import ApprovalDecision, ToolEffect
from rivumi.conversation_runtime import (
    ActionPreviewUpdatedEvent,
    ApprovalRequestedEvent,
    ApprovalResolvedEvent,
    CompactionCompletedEvent,
    CompactionStartedEvent,
    ContextUsageUpdatedEvent,
    ConversationProtocolError,
    ConversationRuntimeEvent,
    NoticeEvent,
    RuntimeApprovalKind,
    RuntimeApprovalRequest,
    RuntimeToolKind,
    RuntimeToolStatus,
    RuntimeTurnStatus,
    TextDeltaEvent,
    ToolCompletedEvent,
    ToolOutputDeltaEvent,
    ToolStartedEvent,
    TurnCompletedEvent,
    TurnStartedEvent,
)
from rivumi.runtime_semantics import (
    ContextTelemetry,
    ContextTelemetryAccuracy,
    ProposedChange,
    ProposedChangeKind,
    RuntimeCapabilities,
)

_SAFE_ENV_KEYS = {
    "CODEX_HOME",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "SHELL",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "TZ",
    "USER",
}
_SECRET_ENV_MARKERS = ("API_KEY", "AUTH", "CREDENTIAL", "PASSWORD", "SECRET", "TOKEN")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOML_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_LOG = logging.getLogger(__name__)
_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval": RuntimeApprovalKind.COMMAND,
    "item/fileChange/requestApproval": RuntimeApprovalKind.FILE_CHANGE,
    "item/permissions/requestApproval": RuntimeApprovalKind.PERMISSIONS,
}
_IGNORED_NOTIFICATIONS = {
    "account/rateLimits/updated",
    "account/updated",
    "app/list/updated",
    "remoteControl/status/changed",
    "mcpServer/startupStatus/updated",
    "thread/started",
    "thread/status/changed",
    "thread/name/updated",
    "turn/plan/updated",
    "item/plan/delta",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/summaryPartAdded",
    "item/reasoning/textDelta",
    "serverRequest/resolved",
    "skills/changed",
}
_TOOL_ITEM_TYPES = {
    "collabAgentToolCall",
    "commandExecution",
    "dynamicToolCall",
    "fileChange",
    "mcpToolCall",
    "webSearch",
}
_NON_TOOL_ITEM_TYPES = {
    "agentMessage",
    "contextCompaction",
    "enteredReviewMode",
    "exitedReviewMode",
    "hookPrompt",
    "imageView",
    "plan",
    "reasoning",
    "sleep",
    "subAgentActivity",
    "userMessage",
}


@dataclass(frozen=True)
class _PendingApproval:
    wire_id: int | str
    method: str
    turn_id: str
    requested_permissions: dict[str, Any] | None
    available: tuple[ApprovalDecision, ...]


class CodexAppServerSession:
    """Manage one ephemeral Codex thread over the app-server JSONL protocol."""

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
        if request_timeout_seconds <= 0 or shutdown_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        if min(max_input_bytes, max_frame_bytes, max_frames) <= 0:
            raise ValueError("protocol bounds must be positive")
        if sandbox_mode not in {"read-only", "workspace-write"}:
            raise ValueError("sandbox_mode must be read-only or workspace-write")
        candidate = Path(working_directory).expanduser()
        if candidate.is_symlink():
            raise ValueError("working_directory cannot be a symlink")
        self.working_directory = candidate.resolve(strict=True)
        if not self.working_directory.is_dir():
            raise ValueError("working_directory must be an existing directory")
        raw_roots = runtime_workspace_roots or (self.working_directory,)
        resolved_roots: list[Path] = []
        for raw_root in raw_roots:
            root = Path(raw_root).expanduser()
            if root.is_symlink():
                raise ValueError("runtime workspace roots cannot be symlinks")
            resolved = root.resolve(strict=True)
            if not resolved.is_dir():
                raise ValueError("runtime workspace roots must be directories")
            resolved_roots.append(resolved)
        if not any(
            self.working_directory == root or self.working_directory.is_relative_to(root)
            for root in resolved_roots
        ):
            raise ValueError("working_directory must be inside a runtime workspace root")
        self.runtime_workspace_roots = tuple(dict.fromkeys(resolved_roots))
        self.executable = str(executable)
        self.model = self._validate_model(model)
        self.sandbox_mode = sandbox_mode
        self.request_timeout_seconds = request_timeout_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.max_input_bytes = max_input_bytes
        self.max_frame_bytes = max_frame_bytes
        self.max_frames = max_frames
        self._host_env = host_env
        self.allowed_mcp_servers = tuple(dict.fromkeys(allowed_mcp_servers))
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending_rpc: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._pending_approvals: dict[str, _PendingApproval] = {}
        self._wire_approval_ids: set[int | str] = set()
        self._event_queue: asyncio.Queue[ConversationRuntimeEvent | BaseException | None] = (
            asyncio.Queue()
        )
        self._next_rpc_id = 1
        self._next_sequence = 0
        self._frame_count = 0
        self._native_thread_id: str | None = None
        self._native_turns: dict[str, str] = {}
        self._stderr_tail: deque[bytes] = deque(maxlen=8)
        self._local_turns: dict[str, str] = {}
        self._starting_turn: str | None = None
        self._active_turn: str | None = None
        self._started_turns: set[str] = set()
        self._completed_turns: set[str] = set()
        self._native_actions: dict[tuple[str, str], str] = {}
        self._started_actions: set[str] = set()
        self._action_approval_context: dict[str, str] = {}
        self._action_previews: dict[str, tuple[ProposedChange, ...]] = {}
        self._preview_change_ids: dict[tuple[str, tuple[str, ...]], str] = {}
        self._turn_diffs: dict[str, str] = {}
        self._compaction_turns: set[str] = set()
        self._started_compactions: set[str] = set()
        self._completed_compactions: set[str] = set()
        self._compaction_start_future: asyncio.Future[str] | None = None
        self._closed = False
        self._fatal: BaseException | None = None
        self._write_lock = asyncio.Lock()

    @staticmethod
    def _validate_model(model: str | None) -> str | None:
        if model is None:
            return None
        normalized = model.strip()
        if not normalized or len(normalized) > 256 or not normalized.isprintable():
            raise ValueError("model must be a printable model name")
        return normalized

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            token_usage=True,
            native_compaction=True,
            proposed_file_preview=True,
        )

    def _resolve_executable(self) -> str:
        if os.path.dirname(self.executable):
            path = Path(self.executable).expanduser().resolve(strict=False)
            if not path.is_file() or not os.access(path, os.X_OK):
                raise FileNotFoundError("Codex executable is unavailable")
            return str(path)
        source = os.environ if self._host_env is None else self._host_env
        resolved = shutil.which(self.executable, path=source.get("PATH"))
        if resolved is None:
            raise FileNotFoundError("Codex executable is unavailable")
        return resolved

    def _controlled_env(self) -> dict[str, str]:
        source = os.environ if self._host_env is None else self._host_env
        env = {key: value for key, value in source.items() if key in _SAFE_ENV_KEYS}
        forwarded_credentials: set[str] = set()
        for name, config in self._configured_mcp_servers().items():
            if name not in self.allowed_mcp_servers:
                continue
            variable = config.get("bearer_token_env_var")
            if isinstance(variable, str) and _ENV_NAME.fullmatch(variable) and variable in source:
                env[variable] = source[variable]
                forwarded_credentials.add(variable)
        env["PATH"] = env.get("PATH", os.defpath)
        env.update(
            {
                "GIT_ASKPASS": "/usr/bin/false",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "NO_COLOR": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        assert not any(
            marker in key.upper()
            for key in env
            if key not in forwarded_credentials
            for marker in _SECRET_ENV_MARKERS
        )
        return env

    def _configured_mcp_servers(self) -> dict[str, Mapping[str, Any]]:
        source = os.environ if self._host_env is None else self._host_env
        codex_home = source.get("CODEX_HOME")
        if codex_home:
            config_path = Path(codex_home).expanduser() / "config.toml"
        else:
            home = source.get("HOME")
            if not home:
                return {}
            config_path = Path(home).expanduser() / ".codex" / "config.toml"
        try:
            with config_path.open("rb") as config_file:
                config = tomllib.load(config_file)
        except (OSError, tomllib.TOMLDecodeError):
            return {}
        servers = config.get("mcp_servers")
        if not isinstance(servers, dict):
            return {}
        return {
            name: server
            for name, server in servers.items()
            if isinstance(name, str) and isinstance(server, dict)
        }

    def _mcp_configuration_args(self) -> tuple[str, ...]:
        args: list[str] = []
        allowed = set(self.allowed_mcp_servers)
        for name in self._configured_mcp_servers():
            if not _TOML_BARE_KEY.fullmatch(name):
                raise ConversationProtocolError(
                    "Codex MCP server names must be TOML bare keys for safe isolation"
                )
            enabled = "true" if name in allowed else "false"
            args.extend(("-c", f"mcp_servers.{name}.enabled={enabled}"))
        return tuple(args)

    async def start(self) -> None:
        if self._process is not None or self._closed:
            raise RuntimeError("session cannot be started more than once")
        executable = self._resolve_executable()
        self._process = await asyncio.create_subprocess_exec(
            executable,
            "app-server",
            "--disable",
            "hooks",
            "--disable",
            "plugins",
            "--disable",
            "remote_plugin",
            "-c",
            "hooks.state={}",
            *self._mcp_configuration_args(),
            "--listen",
            "stdio://",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.working_directory,
            env=self._controlled_env(),
            start_new_session=os.name == "posix",
            limit=self.max_frame_bytes + 1,
        )
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            await self._rpc(
                "initialize",
                {
                    "clientInfo": {
                        "name": "rivumi",
                        "title": "Rivumi",
                        "version": "0.1.0",
                    },
                    # runtimeWorkspaceRoots is the safety boundary that keeps
                    # the native session inside Rivumi's disposable workspace.
                    # Codex gates that field behind this explicit capability.
                    "capabilities": {
                        "experimentalApi": True,
                        "optOutNotificationMethods": sorted(_IGNORED_NOTIFICATIONS),
                    },
                },
            )
            await self._write_frame({"method": "initialized"})
            params: dict[str, Any] = {
                "cwd": str(self.working_directory),
                "runtimeWorkspaceRoots": [str(root) for root in self.runtime_workspace_roots],
                "approvalPolicy": "untrusted",
                "approvalsReviewer": "user",
                "sandbox": self.sandbox_mode,
                "ephemeral": True,
            }
            if self.model is not None:
                params["model"] = self.model
            response = await self._rpc("thread/start", params)
            thread = response.get("thread")
            if not isinstance(thread, dict) or not self._safe_id(thread.get("id")):
                raise ConversationProtocolError("thread/start returned an invalid thread")
            self._native_thread_id = thread["id"]
        except BaseException:
            await self.aclose()
            raise

    async def send_turn(self, text: str) -> str:
        self._ensure_ready()
        normalized = text.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("turn text must be non-blank and NUL-free")
        if len(normalized.encode("utf-8")) > self.max_input_bytes:
            raise ValueError("turn text exceeds max_input_bytes")
        if self._active_turn is not None or self._starting_turn is not None:
            raise RuntimeError("a turn is already active")
        local_turn = uuid4().hex
        self._starting_turn = local_turn
        try:
            response = await self._rpc(
                "turn/start",
                {
                    "threadId": self._native_thread_id,
                    "clientUserMessageId": uuid4().hex,
                    "input": [{"type": "text", "text": normalized, "text_elements": []}],
                },
            )
            turn = response.get("turn")
            if not isinstance(turn, dict) or not self._safe_id(turn.get("id")):
                raise ConversationProtocolError("turn/start returned an invalid turn")
            self._bind_turn(turn["id"], local_turn)
            if local_turn not in self._completed_turns:
                self._active_turn = local_turn
            self._emit_turn_started(local_turn)
            return local_turn
        except BaseException:
            self._starting_turn = None
            raise
        finally:
            if self._starting_turn == local_turn:
                self._starting_turn = None

    async def compact_context(self, guidance: str | None = None) -> str:
        self._ensure_ready()
        if guidance is not None and guidance.strip():
            raise ValueError("Codex native compaction does not accept guidance")
        if self._active_turn is not None or self._starting_turn is not None:
            raise RuntimeError("a turn is already active")
        local_turn = uuid4().hex
        self._starting_turn = local_turn
        self._compaction_turns.add(local_turn)
        binding: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._compaction_start_future = binding
        try:
            await self._rpc("thread/compact/start", {"threadId": self._native_thread_id})
            await asyncio.wait_for(asyncio.shield(binding), timeout=self.request_timeout_seconds)
            return local_turn
        except BaseException:
            self._compaction_turns.discard(local_turn)
            raise
        finally:
            if self._starting_turn == local_turn:
                self._starting_turn = None
            if self._compaction_start_future is binding:
                self._compaction_start_future = None

    def events(self) -> AsyncIterator[ConversationRuntimeEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[ConversationRuntimeEvent]:
        while True:
            value = await self._event_queue.get()
            if value is None:
                return
            if isinstance(value, BaseException):
                raise value
            yield value

    async def respond_approval(self, request_id: str, decision: ApprovalDecision) -> None:
        self._ensure_ready()
        pending = self._pending_approvals.get(request_id)
        if pending is None:
            raise ConversationProtocolError("approval response is stale or duplicate")
        normalized = ApprovalDecision(decision)
        if normalized not in pending.available:
            raise ValueError("approval decision is unavailable for this request")
        result = self._approval_result(pending, normalized)
        await self._write_frame({"id": pending.wire_id, "result": result})
        del self._pending_approvals[request_id]
        self._wire_approval_ids.remove(pending.wire_id)
        self._emit(
            ApprovalResolvedEvent,
            turn_id=pending.turn_id,
            request_id=request_id,
            decision=normalized,
        )

    async def interrupt(self, turn_id: str) -> None:
        self._ensure_ready()
        native_turn = self._local_turns.get(turn_id)
        if native_turn is None or turn_id in self._completed_turns:
            raise ConversationProtocolError("turn is unknown or already terminal")
        await self._rpc(
            "turn/interrupt",
            {"threadId": self._native_thread_id, "turnId": native_turn},
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is not None and process.returncode is None:
            self._terminate_process(process)
            try:
                await asyncio.wait_for(process.wait(), self.shutdown_timeout_seconds)
            except TimeoutError:
                self._kill_process(process)
                with suppress(Exception):
                    await process.wait()
        current = asyncio.current_task()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and task is not current and not task.done():
                task.cancel()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and task is not current:
                with suppress(asyncio.CancelledError, Exception):
                    await task
        error = ConversationProtocolError("Codex app-server session closed")
        for future in self._pending_rpc.values():
            if not future.done():
                future.set_exception(error)
        self._pending_rpc.clear()
        await self._event_queue.put(None)

    def _ensure_ready(self) -> None:
        if self._fatal is not None:
            raise ConversationProtocolError("Codex app-server session failed") from self._fatal
        if self._closed or self._process is None or self._native_thread_id is None:
            raise RuntimeError("session is not started")
        if self._process.returncode is not None:
            raise ConversationProtocolError("Codex app-server exited")

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._fatal is not None:
            raise ConversationProtocolError("Codex app-server session failed") from self._fatal
        request_id = self._next_rpc_id
        self._next_rpc_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_rpc[request_id] = future
        await self._write_frame({"method": method, "id": request_id, "params": params})
        try:
            return await asyncio.wait_for(
                asyncio.shield(future), timeout=self.request_timeout_seconds
            )
        except TimeoutError as exc:
            self._pending_rpc.pop(request_id, None)
            raise ConversationProtocolError(f"Codex request timed out: {method}") from exc

    async def _write_frame(self, frame: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise ConversationProtocolError("Codex app-server stdin is unavailable")
        payload = json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        if len(payload) > self.max_frame_bytes:
            raise ConversationProtocolError("outbound app-server frame exceeds bound")
        async with self._write_lock:
            process.stdin.write(payload)
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise ConversationProtocolError("Codex app-server pipe closed") from exc

    async def _reader_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while True:
                raw = await self._process.stdout.readline()
                if not raw:
                    if not self._closed:
                        raise ConversationProtocolError("Codex app-server closed stdout")
                    return
                self._frame_count += 1
                if self._frame_count > self.max_frames or len(raw) > self.max_frame_bytes:
                    raise ConversationProtocolError("app-server output exceeded protocol bounds")
                try:
                    frame = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ConversationProtocolError("app-server emitted invalid JSON") from exc
                if not isinstance(frame, dict):
                    raise ConversationProtocolError("app-server frame must be an object")
                await self._handle_frame(frame)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self._fail(exc)

    async def _handle_frame(self, frame: dict[str, Any]) -> None:
        method = frame.get("method")
        has_id = "id" in frame
        if isinstance(method, str):
            params = frame.get("params", {})
            if not isinstance(params, dict):
                raise ConversationProtocolError("app-server params must be an object")
            if has_id:
                await self._handle_server_request(method, frame["id"], params)
            else:
                self._handle_notification(method, params)
            return
        if not has_id or not isinstance(frame["id"], int):
            raise ConversationProtocolError("unrecognized app-server frame")
        request_id = frame["id"]
        future = self._pending_rpc.get(request_id)
        if future is None:
            raise ConversationProtocolError("response id is stale, duplicate, or unknown")
        if ("result" in frame) == ("error" in frame):
            raise ConversationProtocolError("response requires exactly one result or error")
        if "error" in frame:
            del self._pending_rpc[request_id]
            error = frame["error"]
            future.set_exception(ConversationProtocolError(f"Codex request failed: {error!r}"))
            return
        result = frame["result"]
        if not isinstance(result, dict):
            raise ConversationProtocolError("app-server result must be an object")
        del self._pending_rpc[request_id]
        future.set_result(result)

    async def _handle_server_request(
        self, method: str, wire_id: object, params: dict[str, Any]
    ) -> None:
        if method not in _APPROVAL_METHODS:
            raise ConversationProtocolError(f"unsupported server request: {method}")
        if not isinstance(wire_id, (int, str)) or isinstance(wire_id, bool):
            raise ConversationProtocolError("server request has invalid id")
        if wire_id in self._wire_approval_ids:
            raise ConversationProtocolError("duplicate approval request id")
        if params.get("threadId") != self._native_thread_id:
            raise ConversationProtocolError("approval request references the wrong thread")
        native_turn = params.get("turnId")
        native_item = params.get("itemId")
        if not self._safe_id(native_turn) or not self._safe_id(native_item):
            raise ConversationProtocolError("approval request has invalid correlation ids")
        local_turn = self._local_turn(native_turn, context=f"server request {method}")
        action_id = self._local_action(native_turn, native_item)
        kind = _APPROVAL_METHODS[method]
        available = self._available_decisions(kind, params.get("availableDecisions"))
        request_id = uuid4().hex
        requested_permissions = (
            params.get("permissions") if kind == RuntimeApprovalKind.PERMISSIONS else None
        )
        if requested_permissions is not None and not isinstance(requested_permissions, dict):
            raise ConversationProtocolError("permissions request is malformed")
        pending = _PendingApproval(
            wire_id=wire_id,
            method=method,
            turn_id=local_turn,
            requested_permissions=requested_permissions,
            available=available,
        )
        self._pending_approvals[request_id] = pending
        self._wire_approval_ids.add(wire_id)
        preview = self._approval_preview(
            kind,
            params,
            fallback=self._action_approval_context.get(action_id, ""),
        )
        approval = RuntimeApprovalRequest(
            request_id=request_id,
            turn_id=local_turn,
            action_id=action_id,
            kind=kind,
            effect=(
                ToolEffect.MODIFY if kind == RuntimeApprovalKind.FILE_CHANGE else ToolEffect.EXECUTE
            ),
            preview=preview,
            proposed_changes=self._action_previews.get(action_id, ()),
            grant_scope=(
                self._file_change_grant_scope(action_id)
                if kind == RuntimeApprovalKind.FILE_CHANGE
                else None
            ),
            available_decisions=available,
        )
        self._emit(ApprovalRequestedEvent, turn_id=local_turn, approval=approval)

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method in _IGNORED_NOTIFICATIONS:
            return
        if method == "turn/started":
            turn = params.get("turn")
            if not isinstance(turn, dict) or not self._safe_id(turn.get("id")):
                raise ConversationProtocolError("turn/started is malformed")
            native_turn = turn["id"]
            if (
                native_turn not in self._native_turns
                and self._starting_turn is None
                and self._active_turn is not None
            ):
                # Codex 0.149+ can replace its internal turn (for example after a
                # failed collab spawn) and announce a brand-new native turn id on
                # the same connection.  The user's logical turn is still the one
                # Rivumi started, so adopt the replacement instead of failing the
                # whole conversation.
                _LOG.warning(
                    "codex app-server: adopting server-initiated turn %r into the "
                    "active local turn (previous native binding retained)",
                    native_turn,
                )
                self._adopt_turn(native_turn, self._active_turn)
            self._emit_turn_started(
                self._local_turn(native_turn, context="turn/started")
            )
            return
        if method == "turn/completed":
            self._complete_turn(params)
            return
        if method == "thread/tokenUsage/updated":
            self._observe_token_usage(params)
            return
        if method == "thread/compacted":
            self._emit_compaction_completed(
                self._correlated_turn(params, context="thread/compacted")
            )
            return
        if method == "turn/diff/updated":
            self._observe_turn_diff(params)
            return
        if method == "item/agentMessage/delta":
            turn = self._correlated_turn(params, context=method)
            delta = params.get("delta")
            if not isinstance(delta, str) or not delta:
                raise ConversationProtocolError("agent message delta is malformed")
            self._emit(TextDeltaEvent, turn_id=turn, text=self._bounded(delta))
            return
        if method in {"item/started", "item/completed"}:
            self._handle_item(method, params)
            return
        if method == "item/fileChange/patchUpdated":
            self._handle_file_change_preview(params)
            return
        if method in {
            "item/commandExecution/outputDelta",
            "item/mcpToolCall/progress",
        }:
            self._handle_tool_delta(method, params)
            return
        if method == "error":
            if params.get("willRetry") is True:
                return
            turn = self._correlated_turn(params, context="error")
            error = self._bounded(str(params.get("error", "Codex turn failed")))
            self._terminal(turn, RuntimeTurnStatus.FAILED, error)
            return
        if method == "warning":
            self._observe_warning(params)
            return
        raise ConversationProtocolError(f"unsupported server notification: {method}")

    def _observe_token_usage(self, params: dict[str, Any]) -> None:
        # Telemetry only: a drifted payload must not end the conversation.
        try:
            turn = self._correlated_turn(params, context="thread/tokenUsage/updated")
            self._emit(
                ContextUsageUpdatedEvent,
                turn_id=turn,
                telemetry=self._context_telemetry(params.get("tokenUsage")),
            )
        except ConversationProtocolError:
            _LOG.warning(
                "codex app-server: dropping malformed token usage notification; "
                "recent stderr: %s",
                self._stderr_tail_text(),
            )

    def _observe_turn_diff(self, params: dict[str, Any]) -> None:
        # Display-only delta text: a drifted payload must not end the conversation.
        try:
            turn = self._correlated_turn(params, context="turn/diff/updated")
            diff = params.get("diff")
            if not isinstance(diff, str):
                raise ConversationProtocolError("turn diff update is malformed")
            self._turn_diffs[turn] = self._bounded(diff)[:64000]
        except ConversationProtocolError:
            _LOG.warning(
                "codex app-server: dropping malformed turn diff notification; "
                "recent stderr: %s",
                self._stderr_tail_text(),
            )

    def _observe_warning(self, params: dict[str, Any]) -> None:
        # Secondary notice: a drifted payload must not end the conversation.
        message = params.get("message")
        try:
            if not isinstance(message, str) or not message:
                raise ConversationProtocolError("warning notification is malformed")
            thread_id = params.get("threadId")
            if thread_id is not None:
                if not self._safe_id(thread_id):
                    raise ConversationProtocolError("warning notification has invalid thread")
                if thread_id != self._native_thread_id:
                    raise ConversationProtocolError("warning notification has foreign thread")
        except ConversationProtocolError:
            _LOG.warning(
                "codex app-server: dropping malformed warning notification; "
                "recent stderr: %s",
                self._stderr_tail_text(),
            )
            return
        turn = self._active_turn or self._starting_turn
        if turn is not None and isinstance(message, str):
            self._emit(
                NoticeEvent,
                turn_id=turn,
                level="warning",
                text=self._bounded(message)[:16000],
            )

    def _handle_item(self, method: str, params: dict[str, Any]) -> None:
        turn = self._correlated_turn(params, context=method)
        item = params.get("item")
        if not isinstance(item, dict) or not self._safe_id(item.get("id")):
            raise ConversationProtocolError("item lifecycle notification is malformed")
        item_type = item.get("type")
        if item_type == "contextCompaction":
            if method == "item/started":
                self._emit_compaction_started(turn)
            else:
                self._emit_compaction_completed(turn)
            return
        if item_type in _NON_TOOL_ITEM_TYPES:
            return
        if item_type not in _TOOL_ITEM_TYPES:
            raise ConversationProtocolError(f"unsupported Codex item type: {item_type!r}")
        native_turn = params["turnId"]
        action = self._local_action(native_turn, item["id"])
        if method == "item/started":
            if action in self._started_actions:
                raise ConversationProtocolError("duplicate tool start")
            self._started_actions.add(action)
            kind, name, effect, summary, path, paths = self._tool_description(item_type, item)
            self._action_approval_context[action] = self._tool_approval_context(
                kind=kind,
                summary=summary,
                path=path,
                paths=paths,
            )
            if item_type == "fileChange":
                changes = self._proposed_changes(action, item.get("changes"))
                if changes:
                    self._action_previews[action] = changes
            self._emit(
                ToolStartedEvent,
                turn_id=turn,
                action_id=action,
                kind=kind,
                tool_name=name,
                effect=effect,
                summary=summary,
                path=path,
                paths=paths,
            )
            return
        if action not in self._started_actions:
            raise ConversationProtocolError("tool completed before it started")
        self._started_actions.remove(action)
        self._action_approval_context.pop(action, None)
        status = self._tool_status(item.get("status"))
        self._emit(
            ToolCompletedEvent,
            turn_id=turn,
            action_id=action,
            status=status,
            summary=self._tool_completion_summary(item_type, item),
            output=self._tool_completion_output(item_type, item),
            diff=self._tool_completion_diff(item_type, item),
        )

    def _handle_tool_delta(self, method: str, params: dict[str, Any]) -> None:
        turn = self._correlated_turn(params, context=method)
        native_item = params.get("itemId")
        if not self._safe_id(native_item):
            raise ConversationProtocolError("tool delta has invalid item id")
        action = self._native_actions.get((params["turnId"], native_item))
        if action is None or action not in self._started_actions:
            raise ConversationProtocolError("tool delta preceded tool start")
        value = params.get("delta" if method.endswith("outputDelta") else "message")
        if not isinstance(value, str) or not value:
            raise ConversationProtocolError("tool output delta is malformed")
        self._emit(
            ToolOutputDeltaEvent,
            turn_id=turn,
            action_id=action,
            text=self._bounded(value),
        )

    def _handle_file_change_preview(self, params: dict[str, Any]) -> None:
        turn = self._correlated_turn(params, context="item/fileChange/patchUpdated")
        native_item = params.get("itemId")
        if not self._safe_id(native_item):
            raise ConversationProtocolError("file change preview has invalid item id")
        action = self._native_actions.get((params["turnId"], native_item))
        if action is None or action not in self._started_actions:
            raise ConversationProtocolError("file change preview preceded tool start")
        changes = self._proposed_changes(action, params.get("changes"))
        if not changes:
            raise ConversationProtocolError("file change preview contains no changes")
        self._action_previews[action] = changes
        self._emit(
            ActionPreviewUpdatedEvent,
            turn_id=turn,
            action_id=action,
            proposed_changes=changes,
        )

    def _proposed_changes(self, action_id: str, raw_changes: object) -> tuple[ProposedChange, ...]:
        if not isinstance(raw_changes, list):
            raise ConversationProtocolError("file changes are malformed")
        proposed: list[ProposedChange] = []
        for raw in raw_changes:
            if not isinstance(raw, dict):
                raise ConversationProtocolError("file change entry is malformed")
            path = raw.get("path")
            diff = raw.get("diff")
            raw_kind = raw.get("kind")
            if not isinstance(path, str) or not path or len(path) > 4096 or "\x00" in path:
                raise ConversationProtocolError("file change path is malformed")
            if diff is not None and not isinstance(diff, str):
                raise ConversationProtocolError("file change diff is malformed")
            kind_name: object = raw_kind
            move_path: object = None
            if isinstance(raw_kind, dict):
                kind_name = raw_kind.get("type")
                move_path = raw_kind.get("move_path")
            mapping = {
                "add": ProposedChangeKind.CREATE,
                "update": ProposedChangeKind.UPDATE,
                "delete": ProposedChangeKind.DELETE,
            }
            kind = mapping.get(kind_name)
            if kind is None:
                raise ConversationProtocolError("file change kind is malformed")
            paths = (path,)
            if kind_name == "update" and move_path is not None:
                if (
                    not isinstance(move_path, str)
                    or not move_path
                    or len(move_path) > 4096
                    or "\x00" in move_path
                ):
                    raise ConversationProtocolError("file move path is malformed")
                kind = ProposedChangeKind.MOVE
                paths = (path, move_path)
            shown_diff, original_bytes, truncated = self._preview_diff(diff)
            key = (action_id, paths)
            change_id = self._preview_change_ids.setdefault(key, uuid4().hex)
            proposed.append(
                ProposedChange(
                    change_id=change_id,
                    action_id=action_id,
                    kind=kind,
                    paths=paths,
                    summary=f"{kind.value} {' -> '.join(paths)}",
                    unified_diff=shown_diff,
                    original_diff_bytes=original_bytes,
                    truncated=truncated,
                )
            )
        return tuple(proposed)

    @staticmethod
    def _preview_diff(diff: str | None) -> tuple[str | None, int | None, bool]:
        if diff is None:
            return None, None, False
        encoded = diff.encode("utf-8")
        shown = encoded[:64000].decode("utf-8", errors="ignore")
        return shown, len(encoded), len(shown.encode("utf-8")) < len(encoded)

    def _file_change_grant_scope(self, action_id: str) -> str | None:
        changes = self._action_previews.get(action_id, ())
        if not changes:
            return None
        fingerprint = hashlib.sha256()
        normalized = sorted((change.kind.value, change.paths) for change in changes)
        for kind, paths in normalized:
            fingerprint.update(kind.encode("utf-8"))
            fingerprint.update(b"\0")
            for path in paths:
                fingerprint.update(path.encode("utf-8"))
                fingerprint.update(b"\0")
        return f"file_change:{fingerprint.hexdigest()}"

    @staticmethod
    def _context_telemetry(raw: object) -> ContextTelemetry:
        if not isinstance(raw, dict) or not isinstance(raw.get("last"), dict):
            raise ConversationProtocolError("token usage update is malformed")
        last = raw["last"]

        def count(name: str) -> int:
            value = last.get(name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ConversationProtocolError("token usage update is malformed")
            return value

        context_window = raw.get("modelContextWindow")
        if context_window is not None and (
            not isinstance(context_window, int)
            or isinstance(context_window, bool)
            or context_window <= 0
        ):
            raise ConversationProtocolError("token usage context window is malformed")
        try:
            return ContextTelemetry(
                accuracy=ContextTelemetryAccuracy.EXACT,
                input_tokens=count("inputTokens"),
                cached_input_tokens=count("cachedInputTokens"),
                output_tokens=count("outputTokens"),
                reasoning_output_tokens=count("reasoningOutputTokens"),
                total_tokens=count("totalTokens"),
                context_window=context_window,
            )
        except ValueError as exc:
            raise ConversationProtocolError("token usage update is incoherent") from exc

    def _emit_compaction_started(self, turn: str) -> None:
        if turn in self._started_compactions:
            return
        self._compaction_turns.add(turn)
        self._started_compactions.add(turn)
        self._active_turn = turn
        self._emit(CompactionStartedEvent, turn_id=turn, guidance=None)

    def _emit_compaction_completed(self, turn: str) -> None:
        self._emit_compaction_started(turn)
        if turn in self._completed_compactions:
            return
        self._completed_compactions.add(turn)
        self._emit(CompactionCompletedEvent, turn_id=turn, checkpoint=None)

    def _complete_turn(self, params: dict[str, Any]) -> None:
        turn = params.get("turn")
        if not isinstance(turn, dict) or not self._safe_id(turn.get("id")):
            raise ConversationProtocolError("turn/completed is malformed")
        local = self._local_turn(turn["id"], context="turn/completed")
        native_turn = turn["id"]
        if any(
            action in self._started_actions
            for (action_turn, _), action in self._native_actions.items()
            if action_turn == native_turn
        ):
            raise ConversationProtocolError("turn completed with an unfinished tool")
        raw_status = turn.get("status")
        statuses = {
            "completed": RuntimeTurnStatus.COMPLETED,
            "failed": RuntimeTurnStatus.FAILED,
            "interrupted": RuntimeTurnStatus.INTERRUPTED,
        }
        if raw_status not in statuses:
            raise ConversationProtocolError("turn/completed has invalid status")
        if local in self._compaction_turns:
            if local in self._completed_turns:
                raise ConversationProtocolError("duplicate terminal turn event")
            if raw_status == "completed":
                self._emit_compaction_completed(local)
            self._completed_turns.add(local)
            if self._active_turn == local:
                self._active_turn = None
            self._compaction_turns.discard(local)
            return
        error: str | None = None
        if raw_status == "failed":
            error = self._bounded(str(turn.get("error") or "Codex turn failed"))
        self._terminal(local, statuses[raw_status], error)

    def _terminal(self, turn: str, status: RuntimeTurnStatus, error: str | None) -> None:
        if turn in self._completed_turns:
            raise ConversationProtocolError("duplicate terminal turn event")
        self._completed_turns.add(turn)
        if self._active_turn == turn:
            self._active_turn = None
        self._emit(TurnCompletedEvent, turn_id=turn, status=status, error=error)

    def _emit_turn_started(self, turn: str) -> None:
        if turn in self._compaction_turns:
            self._emit_compaction_started(turn)
            return
        if turn in self._started_turns:
            return
        self._started_turns.add(turn)
        self._emit(TurnStartedEvent, turn_id=turn)

    def _emit(self, cls: type[Any], **kwargs: Any) -> None:
        event = cls(sequence=self._next_sequence, **kwargs)
        self._next_sequence += 1
        self._event_queue.put_nowait(event)

    async def _fail(self, exc: BaseException) -> None:
        if self._fatal is not None or self._closed:
            return
        self._fatal = exc
        wrapped = (
            exc
            if isinstance(exc, ConversationProtocolError)
            else ConversationProtocolError("Codex app-server protocol failed")
        )
        for future in self._pending_rpc.values():
            if not future.done():
                future.set_exception(wrapped)
        self._pending_rpc.clear()
        self._event_queue.put_nowait(wrapped)
        process = self._process
        if process is not None and process.returncode is None:
            self._terminate_process(process)

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        retained = 0
        while True:
            chunk = await self._process.stderr.read(64 * 1024)
            if not chunk:
                return
            self._stderr_tail.append(chunk)
            retained += len(chunk)
            if retained > self.max_frame_bytes:
                retained = self.max_frame_bytes

    def _stderr_tail_text(self, *, limit: int = 1_000) -> str:
        if not self._stderr_tail:
            return "(empty)"
        text = b"".join(self._stderr_tail).decode(errors="replace")
        text = " ".join(text.split())
        return text[-limit:]

    def _local_turn(self, native_turn: str, *, context: str) -> str:
        existing = self._native_turns.get(native_turn)
        if existing is not None:
            return existing
        if self._starting_turn is None:
            if self._active_turn is not None:
                # Codex 0.149+ may abandon its internal turn and continue the
                # same logical turn under a fresh id (see the turn/started
                # adoption); item-level notifications then reference an id
                # Rivumi never bound.  Adopt instead of failing the whole
                # conversation.
                _LOG.warning(
                    "codex app-server: %s adopts replacement native turn %r "
                    "into the active local turn",
                    context,
                    native_turn,
                )
                self._adopt_turn(native_turn, self._active_turn)
                return self._active_turn
            known = len(self._native_turns)
            _LOG.warning(
                "codex app-server: %s references an unbound turn "
                "(native_turn=%r, bound_turns=%d, thread=%r); recent stderr: %s",
                context,
                native_turn,
                known,
                self._native_thread_id,
                self._stderr_tail_text(),
            )
            raise ConversationProtocolError(
                f"{context} references an unknown turn "
                f"(native_turn={native_turn!r}, bound_turns={known})"
            )
        _LOG.debug(
            "codex app-server: %s binds native_turn=%r to the starting turn",
            context,
            native_turn,
        )
        self._bind_turn(native_turn, self._starting_turn)
        return self._starting_turn

    def _bind_turn(self, native_turn: str, local_turn: str) -> None:
        existing = self._native_turns.get(native_turn)
        if existing is not None and existing != local_turn:
            raise ConversationProtocolError("native turn id was rebound")
        reverse = self._local_turns.get(local_turn)
        if reverse is not None and reverse != native_turn:
            raise ConversationProtocolError("local turn id was rebound")
        self._native_turns[native_turn] = local_turn
        self._local_turns[local_turn] = native_turn
        if local_turn in self._compaction_turns:
            binding = self._compaction_start_future
            if binding is not None and not binding.done():
                binding.set_result(local_turn)

    def _adopt_turn(self, native_turn: str, local_turn: str) -> None:
        """Map a replacement native id onto an already-active local turn.

        Unlike :meth:`_bind_turn` this intentionally allows several native ids
        to share one local turn: Codex 0.149+ may abandon its internal turn and
        continue the same logical turn under a fresh id.  The original reverse
        binding is preserved so interrupts still target the id Codex knows.
        """

        existing = self._native_turns.get(native_turn)
        if existing is not None and existing != local_turn:
            raise ConversationProtocolError("native turn id was rebound")
        self._native_turns[native_turn] = local_turn

    def _local_action(self, native_turn: str, native_item: str) -> str:
        key = (native_turn, native_item)
        value = self._native_actions.get(key)
        if value is None:
            value = uuid4().hex
            self._native_actions[key] = value
        return value

    def _correlated_turn(self, params: dict[str, Any], *, context: str) -> str:
        thread = params.get("threadId")
        native_turn = params.get("turnId")
        # An absent thread id is tolerated the same way as in the warning
        # handler: the app-server pipe is dedicated to one ephemeral thread,
        # so a missing field cannot point at a foreign conversation.  A
        # present but different id still fails closed.
        if (thread is not None and thread != self._native_thread_id) or not self._safe_id(
            native_turn
        ):
            _LOG.warning(
                "codex app-server: %s has invalid correlation "
                "(thread=%r, expected=%r, turnId=%r); recent stderr: %s",
                context,
                thread,
                self._native_thread_id,
                native_turn,
                self._stderr_tail_text(),
            )
            raise ConversationProtocolError(
                f"{context} notification correlation is invalid"
            )
        return self._local_turn(native_turn, context=context)

    @staticmethod
    def _safe_id(value: object) -> bool:
        return isinstance(value, str) and 0 < len(value) <= 256 and "\x00" not in value

    def _bounded(self, value: str) -> str:
        encoded = value.encode("utf-8")
        if len(encoded) <= self.max_frame_bytes:
            return value
        return encoded[: self.max_frame_bytes].decode("utf-8", errors="ignore")

    def _approval_preview(
        self,
        kind: RuntimeApprovalKind,
        params: dict[str, Any],
        *,
        fallback: str = "",
    ) -> str:
        if kind == RuntimeApprovalKind.COMMAND:
            labels = (
                ("Command", params.get("command")),
                ("Working directory", params.get("cwd")),
                ("Reason", params.get("reason")),
            )
        elif kind == RuntimeApprovalKind.FILE_CHANGE:
            labels = (
                ("Reason", params.get("reason")),
                ("Grant root", params.get("grantRoot")),
            )
        else:
            labels = (
                ("Reason", params.get("reason")),
                ("Working directory", params.get("cwd")),
            )
        details = [
            f"{label}: {value}" for label, value in labels if isinstance(value, str) and value
        ]
        if fallback:
            details.insert(0, fallback)
        if not details:
            details = [
                f"Action: {kind.value.replace('_', ' ')}",
                "Details: The runtime did not identify the command, files, or working directory.",
            ]
        return self._bounded("\n".join(details))[:16000]

    def _tool_approval_context(
        self,
        *,
        kind: RuntimeToolKind,
        summary: str,
        path: str | None,
        paths: tuple[str, ...],
    ) -> str:
        if kind == RuntimeToolKind.COMMAND:
            lines = [
                "Action: Execute command",
                f"Command: {summary}",
                f"Working directory: {path or self.working_directory}",
                "Impact: Runs a command in the workspace.",
            ]
        elif kind == RuntimeToolKind.FILE_CHANGE:
            lines = [
                "Action: Modify files",
                f"Working directory: {self.working_directory}",
                f"Impact: {summary} in the workspace.",
            ]
            if paths:
                lines.append("Files:")
                lines.extend(f"- {item}" for item in paths)
            elif path:
                lines.append(f"File: {path}")
            else:
                lines.append("Files: The runtime did not identify the affected paths.")
        else:
            lines = [
                f"Action: {kind.value.replace('_', ' ')}",
                f"Working directory: {self.working_directory}",
            ]
            if summary:
                lines.append(f"Details: {summary}")
        return self._bounded("\n".join(lines))[:16000]

    @staticmethod
    def _available_decisions(
        kind: RuntimeApprovalKind, raw: object
    ) -> tuple[ApprovalDecision, ...]:
        if kind == RuntimeApprovalKind.PERMISSIONS:
            return (
                ApprovalDecision.ALLOW_ONCE,
                ApprovalDecision.ALLOW_SESSION,
                ApprovalDecision.DENY,
                ApprovalDecision.CANCEL,
            )
        mapping = {
            "accept": ApprovalDecision.ALLOW_ONCE,
            "acceptForSession": ApprovalDecision.ALLOW_SESSION,
            "decline": ApprovalDecision.DENY,
            "cancel": ApprovalDecision.CANCEL,
        }
        if raw is None:
            return tuple(mapping.values())
        if not isinstance(raw, list):
            raise ConversationProtocolError("availableDecisions must be a list")
        result: list[ApprovalDecision] = []
        for value in raw:
            if isinstance(value, str) and value in mapping and mapping[value] not in result:
                result.append(mapping[value])
        if not result:
            raise ConversationProtocolError("approval exposes no supported decision")
        return tuple(result)

    @staticmethod
    def _approval_result(pending: _PendingApproval, decision: ApprovalDecision) -> dict[str, Any]:
        if pending.method == "item/permissions/requestApproval":
            permissions: dict[str, Any] = {}
            if decision in {ApprovalDecision.ALLOW_ONCE, ApprovalDecision.ALLOW_SESSION}:
                for key, value in (pending.requested_permissions or {}).items():
                    if key in {"network", "fileSystem"} and value is not None:
                        permissions[key] = value
            return {
                "permissions": permissions,
                "scope": ("session" if decision == ApprovalDecision.ALLOW_SESSION else "turn"),
                "strictAutoReview": True,
            }
        mapping = {
            ApprovalDecision.ALLOW_ONCE: "accept",
            ApprovalDecision.ALLOW_SESSION: "acceptForSession",
            ApprovalDecision.DENY: "decline",
            ApprovalDecision.CANCEL: "cancel",
        }
        return {"decision": mapping[decision]}

    @staticmethod
    def _tool_description(
        item_type: str, item: dict[str, Any]
    ) -> tuple[
        RuntimeToolKind,
        str,
        ToolEffect,
        str,
        str | None,
        tuple[str, ...],
    ]:
        if item_type == "commandExecution":
            command = item.get("command")
            if not isinstance(command, str):
                raise ConversationProtocolError("command item has no command")
            cwd = item.get("cwd")
            return (
                RuntimeToolKind.COMMAND,
                "shell",
                ToolEffect.EXECUTE,
                command[:16000],
                cwd[:4096] if isinstance(cwd, str) else None,
                (),
            )
        if item_type == "fileChange":
            changes = item.get("changes")
            if not isinstance(changes, list):
                raise ConversationProtocolError("file change item is malformed")
            paths = [change.get("path") for change in changes if isinstance(change, dict)]
            safe_paths = tuple(
                dict.fromkeys(path[:4096] for path in paths if isinstance(path, str) and path)
            )
            path = safe_paths[0][:4096] if safe_paths else None
            summary = f"{len(changes)} file change(s)"
            return (
                RuntimeToolKind.FILE_CHANGE,
                "file_change",
                ToolEffect.MODIFY,
                summary,
                path,
                safe_paths,
            )
        if item_type == "mcpToolCall":
            server, tool = item.get("server"), item.get("tool")
            if not isinstance(server, str) or not isinstance(tool, str):
                raise ConversationProtocolError("MCP item is malformed")
            return (
                RuntimeToolKind.MCP,
                f"{server}/{tool}"[:256],
                ToolEffect.EXECUTE,
                "",
                None,
                (),
            )
        if item_type == "dynamicToolCall":
            tool = item.get("tool")
            if not isinstance(tool, str):
                raise ConversationProtocolError("dynamic tool item is malformed")
            return RuntimeToolKind.MCP, tool[:256], ToolEffect.EXECUTE, "", None, ()
        if item_type == "collabAgentToolCall":
            return RuntimeToolKind.AGENT, "agent", ToolEffect.EXECUTE, "", None, ()
        return RuntimeToolKind.WEB, "web_search", ToolEffect.EXECUTE, "", None, ()

    def _tool_completion_summary(self, item_type: str, item: dict[str, Any]) -> str:
        if item_type == "commandExecution":
            exit_code = item.get("exitCode")
            return f"exit {exit_code}" if isinstance(exit_code, int) else "command finished"
        status = item.get("status")
        return self._bounded(status)[:16000] if isinstance(status, str) else "tool finished"

    def _tool_completion_output(self, item_type: str, item: dict[str, Any]) -> str | None:
        if item_type != "commandExecution":
            return None
        output = item.get("aggregatedOutput")
        return self._bounded(output)[:64000] if isinstance(output, str) else None

    def _tool_completion_diff(self, item_type: str, item: dict[str, Any]) -> str | None:
        if item_type != "fileChange":
            return None
        changes = item.get("changes")
        if not isinstance(changes, list):
            return None
        diffs = [change.get("diff") for change in changes if isinstance(change, dict)]
        rendered = "\n".join(diff for diff in diffs if isinstance(diff, str))
        if not rendered:
            return None
        return self._bounded(rendered)[:64000]

    @staticmethod
    def _tool_status(raw: object) -> RuntimeToolStatus:
        mapping = {
            "completed": RuntimeToolStatus.COMPLETED,
            "failed": RuntimeToolStatus.FAILED,
            "declined": RuntimeToolStatus.DECLINED,
            "interrupted": RuntimeToolStatus.INTERRUPTED,
        }
        if raw not in mapping:
            raise ConversationProtocolError("tool completed with an invalid status")
        return mapping[raw]

    @staticmethod
    def _terminate_process(process: asyncio.subprocess.Process) -> None:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
                return
            except (PermissionError, ProcessLookupError):
                pass
        with suppress(ProcessLookupError):
            if process.returncode is None:
                process.terminate()

    @staticmethod
    def _kill_process(process: asyncio.subprocess.Process) -> None:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
                return
            except (PermissionError, ProcessLookupError):
                pass
        with suppress(ProcessLookupError):
            if process.returncode is None:
                process.kill()
