"""Public Codex session: process/RPC shell and protocol-owner composition."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import tomllib
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from looplane.approvals import ApprovalDecision
from looplane.conversation_runtime import (
    ApprovalResolvedEvent,
    ConversationProtocolError,
    ConversationRuntimeEvent,
    RuntimeApprovalKind,
    RuntimeToolKind,
    RuntimeTurnStatus,
)
from looplane.conversation_runtime import RuntimeToolStatus as RuntimeToolStatus
from looplane.runtime_semantics import (
    ProposedChange,
    RuntimeCapabilities,
)
from looplane.runtimes.codex import approval_mapper as _codex_approvals
from looplane.runtimes.codex import parsing as _codex_parsing
from looplane.runtimes.codex import tool_mapper as _codex_tools
from looplane.runtimes.codex.approval_mapper import CodexApprovalMapper, PendingApproval
from looplane.runtimes.codex.correlation import CodexCorrelation
from looplane.runtimes.codex.event_mapper import _IGNORED_NOTIFICATIONS, CodexEventMapper

_LOG = logging.getLogger("looplane.codex_app_server")

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
        _new_id: Callable[[], str] | None = None,
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
        self._event_queue: asyncio.Queue[ConversationRuntimeEvent | BaseException | None] = (
            asyncio.Queue()
        )
        self._next_rpc_id = 1
        self._next_sequence = 0
        self._frame_count = 0
        self._stderr_tail: deque[bytes] = deque(maxlen=8)
        self._closed = False
        self._fatal: BaseException | None = None
        self._write_lock = asyncio.Lock()

        self._new_id = _new_id or (lambda: uuid4().hex)
        self.correlation = CodexCorrelation(
            new_id=lambda: self._new_id(),
            stderr_tail=self._stderr_tail_text,
        )
        self.event_mapper = CodexEventMapper(
            correlation=self.correlation,
            emit=self._emit,
            bounded=lambda value: self._bounded(value),
            new_id=lambda: self._new_id(),
            stderr_tail=self._stderr_tail_text,
            working_directory=self.working_directory,
        )
        self.approval_mapper = CodexApprovalMapper(
            correlation=self.correlation,
            emit=self._emit,
            bounded=lambda value: self._bounded(value),
            new_id=lambda: self._new_id(),
            action_context=self.event_mapper.approval_context,
        )

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
                        "name": "looplane",
                        "title": "looplane",
                        "version": "0.1.0",
                    },
                    # runtimeWorkspaceRoots is the safety boundary that keeps
                    # the native session inside looplane's disposable workspace.
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
            if not isinstance(thread, dict) or not _codex_parsing.safe_id(thread.get("id")):
                raise ConversationProtocolError("thread/start returned an invalid thread")
            self.correlation.native_thread_id = thread["id"]
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
        if self.correlation.active_turn is not None or self.correlation.starting_turn is not None:
            raise RuntimeError("a turn is already active")
        local_turn = self._new_id()
        self.correlation.starting_turn = local_turn
        try:
            response = await self._rpc(
                "turn/start",
                {
                    "threadId": self.correlation.native_thread_id,
                    "clientUserMessageId": self._new_id(),
                    "input": [{"type": "text", "text": normalized, "text_elements": []}],
                },
            )
            turn = response.get("turn")
            if not isinstance(turn, dict) or not _codex_parsing.safe_id(turn.get("id")):
                raise ConversationProtocolError("turn/start returned an invalid turn")
            self.correlation.bind_turn(turn["id"], local_turn)
            if local_turn not in self.correlation.completed_turns:
                self.correlation.active_turn = local_turn
            self.event_mapper.emit_turn_started(local_turn)
            return local_turn
        except BaseException:
            self.correlation.starting_turn = None
            raise
        finally:
            if self.correlation.starting_turn == local_turn:
                self.correlation.starting_turn = None

    async def compact_context(self, guidance: str | None = None) -> str:
        self._ensure_ready()
        if guidance is not None and guidance.strip():
            raise ValueError("Codex native compaction does not accept guidance")
        if self.correlation.active_turn is not None or self.correlation.starting_turn is not None:
            raise RuntimeError("a turn is already active")
        local_turn = self._new_id()
        self.correlation.starting_turn = local_turn
        self.correlation.compaction_turns.add(local_turn)
        binding: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.correlation.compaction_start_future = binding
        try:
            await self._rpc("thread/compact/start", {"threadId": self.correlation.native_thread_id})
            await asyncio.wait_for(asyncio.shield(binding), timeout=self.request_timeout_seconds)
            return local_turn
        except BaseException:
            self.correlation.compaction_turns.discard(local_turn)
            raise
        finally:
            if self.correlation.starting_turn == local_turn:
                self.correlation.starting_turn = None
            if self.correlation.compaction_start_future is binding:
                self.correlation.compaction_start_future = None

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
        pending = self.approval_mapper.pending.get(request_id)
        if pending is None:
            raise ConversationProtocolError("approval response is stale or duplicate")
        normalized = ApprovalDecision(decision)
        if normalized not in pending.available:
            raise ValueError("approval decision is unavailable for this request")
        result = self.approval_mapper.approval_result(pending, normalized)
        await self._write_frame({"id": pending.wire_id, "result": result})
        del self.approval_mapper.pending[request_id]
        self.approval_mapper.wire_ids.remove(pending.wire_id)
        self._emit(
            ApprovalResolvedEvent,
            turn_id=pending.turn_id,
            request_id=request_id,
            decision=normalized,
        )

    async def interrupt(self, turn_id: str) -> None:
        self._ensure_ready()
        native_turn = self.correlation.local_turns.get(turn_id)
        if native_turn is None or turn_id in self.correlation.completed_turns:
            raise ConversationProtocolError("turn is unknown or already terminal")
        await self._rpc(
            "turn/interrupt",
            {"threadId": self.correlation.native_thread_id, "turnId": native_turn},
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
        if self._closed or self._process is None or self.correlation.native_thread_id is None:
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
                frame = _codex_parsing.parse_frame(
                    raw,
                    frame_count=self._frame_count,
                    max_frames=self.max_frames,
                    max_frame_bytes=self.max_frame_bytes,
                )
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

    def _bounded(self, value: str) -> str:
        return _codex_parsing.bounded_text(value, max_frame_bytes=self.max_frame_bytes)

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

    @property
    def _native_thread_id(self):
        return self.correlation.native_thread_id

    @_native_thread_id.setter
    def _native_thread_id(self, value):
        self.correlation.native_thread_id = value

    @property
    def _native_turns(self):
        return self.correlation.native_turns

    @_native_turns.setter
    def _native_turns(self, value):
        self.correlation.native_turns = value

    @property
    def _local_turns(self):
        return self.correlation.local_turns

    @_local_turns.setter
    def _local_turns(self, value):
        self.correlation.local_turns = value

    @property
    def _starting_turn(self):
        return self.correlation.starting_turn

    @_starting_turn.setter
    def _starting_turn(self, value):
        self.correlation.starting_turn = value

    @property
    def _active_turn(self):
        return self.correlation.active_turn

    @_active_turn.setter
    def _active_turn(self, value):
        self.correlation.active_turn = value

    @property
    def _started_turns(self):
        return self.correlation.started_turns

    @_started_turns.setter
    def _started_turns(self, value):
        self.correlation.started_turns = value

    @property
    def _completed_turns(self):
        return self.correlation.completed_turns

    @_completed_turns.setter
    def _completed_turns(self, value):
        self.correlation.completed_turns = value

    @property
    def _native_actions(self):
        return self.correlation.native_actions

    @_native_actions.setter
    def _native_actions(self, value):
        self.correlation.native_actions = value

    @property
    def _compaction_turns(self):
        return self.correlation.compaction_turns

    @_compaction_turns.setter
    def _compaction_turns(self, value):
        self.correlation.compaction_turns = value

    @property
    def _started_compactions(self):
        return self.correlation.started_compactions

    @_started_compactions.setter
    def _started_compactions(self, value):
        self.correlation.started_compactions = value

    @property
    def _completed_compactions(self):
        return self.correlation.completed_compactions

    @_completed_compactions.setter
    def _completed_compactions(self, value):
        self.correlation.completed_compactions = value

    @property
    def _compaction_start_future(self):
        return self.correlation.compaction_start_future

    @_compaction_start_future.setter
    def _compaction_start_future(self, value):
        self.correlation.compaction_start_future = value

    @property
    def _started_actions(self):
        return self.event_mapper.started_actions

    @_started_actions.setter
    def _started_actions(self, value):
        self.event_mapper.started_actions = value

    @property
    def _action_approval_context(self):
        return self.event_mapper.action_approval_context

    @_action_approval_context.setter
    def _action_approval_context(self, value):
        self.event_mapper.action_approval_context = value

    @property
    def _action_previews(self):
        return self.event_mapper.action_previews

    @_action_previews.setter
    def _action_previews(self, value):
        self.event_mapper.action_previews = value

    @property
    def _preview_change_ids(self):
        return self.event_mapper.preview_change_ids

    @_preview_change_ids.setter
    def _preview_change_ids(self, value):
        self.event_mapper.preview_change_ids = value

    @property
    def _turn_diffs(self):
        return self.event_mapper.turn_diffs

    @_turn_diffs.setter
    def _turn_diffs(self, value):
        self.event_mapper.turn_diffs = value

    def _local_turn(self, native_turn: str, *, context: str) -> str:
        return self.correlation.local_turn(native_turn, context=context)

    def _bind_turn(self, native_turn: str, local_turn: str) -> None:
        return self.correlation.bind_turn(native_turn, local_turn)

    def _adopt_turn(self, native_turn: str, local_turn: str) -> None:
        return self.correlation.adopt_turn(native_turn, local_turn)

    def _local_action(self, native_turn: str, native_item: str) -> str:
        return self.correlation.local_action(native_turn, native_item)

    def _correlated_turn(self, params: dict[str, Any], *, context: str) -> str:
        return self.correlation.correlated_turn(params, context=context)

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        return self.event_mapper.handle_notification(method, params)

    def _observe_token_usage(self, params: dict[str, Any]) -> None:
        return self.event_mapper.observe_token_usage(params)

    def _observe_turn_diff(self, params: dict[str, Any]) -> None:
        return self.event_mapper.observe_turn_diff(params)

    def _observe_skills_changed(self, params: dict[str, Any]) -> None:
        return self.event_mapper.observe_skills_changed(params)

    def _skills_changed_source(self, params: dict[str, Any]) -> str | None:
        return self.event_mapper.skills_changed_source(params)

    def _skills_changed_summary(self, params: dict[str, Any]) -> str:
        return self.event_mapper.skills_changed_summary(params)

    def _skills_changed_names(self, raw: object) -> tuple[str, ...]:
        return self.event_mapper.skills_changed_names(raw)

    def _observe_warning(self, params: dict[str, Any]) -> None:
        return self.event_mapper.observe_warning(params)

    def _handle_item(self, method: str, params: dict[str, Any]) -> None:
        return self.event_mapper.handle_item(method, params)

    def _handle_tool_delta(self, method: str, params: dict[str, Any]) -> None:
        return self.event_mapper.handle_tool_delta(method, params)

    def _handle_file_change_preview(self, params: dict[str, Any]) -> None:
        return self.event_mapper.handle_file_change_preview(params)

    def _proposed_changes(self, action_id: str, raw_changes: object) -> tuple[ProposedChange, ...]:
        return self.event_mapper.proposed_changes(action_id, raw_changes)

    _preview_diff = staticmethod(_codex_parsing.preview_diff)

    def _file_change_grant_scope(self, action_id: str) -> str | None:
        return self.event_mapper.file_change_grant_scope(action_id)

    _context_telemetry = staticmethod(CodexEventMapper.context_telemetry)

    def _emit_compaction_started(self, turn: str) -> None:
        return self.event_mapper.emit_compaction_started(turn)

    def _emit_compaction_completed(self, turn: str) -> None:
        return self.event_mapper.emit_compaction_completed(turn)

    def _complete_turn(self, params: dict[str, Any]) -> None:
        return self.event_mapper.complete_turn(params)

    def _terminal(self, turn: str, status: RuntimeTurnStatus, error: str | None) -> None:
        return self.event_mapper.terminal(turn, status, error)

    def _emit_turn_started(self, turn: str) -> None:
        return self.event_mapper.emit_turn_started(turn)

    def _tool_approval_context(
        self, *, kind: RuntimeToolKind, summary: str, path: str | None, paths: tuple[str, ...]
    ) -> str:
        return self.event_mapper.tool_approval_context(
            kind=kind, summary=summary, path=path, paths=paths
        )

    _tool_description = staticmethod(_codex_tools.tool_description)

    def _tool_completion_summary(self, item_type: str, item: dict[str, Any]) -> str:
        return self.event_mapper.tool_completion_summary(item_type, item)

    def _tool_completion_output(self, item_type: str, item: dict[str, Any]) -> str | None:
        return self.event_mapper.tool_completion_output(item_type, item)

    def _tool_completion_diff(self, item_type: str, item: dict[str, Any]) -> str | None:
        return self.event_mapper.tool_completion_diff(item_type, item)

    _tool_status = staticmethod(_codex_tools.tool_status)

    _safe_id = staticmethod(_codex_parsing.safe_id)

    async def _handle_server_request(
        self, method: str, wire_id: object, params: dict[str, Any]
    ) -> None:
        return self.approval_mapper.handle_server_request(method, wire_id, params)

    def _approval_preview(
        self, kind: RuntimeApprovalKind, params: dict[str, Any], *, fallback: str = ""
    ) -> str:
        return self.approval_mapper.approval_preview(kind, params, fallback=fallback)

    _available_decisions = staticmethod(_codex_approvals.available_decisions)

    @staticmethod
    def _approval_result(pending: PendingApproval, decision: ApprovalDecision) -> dict[str, Any]:
        return CodexApprovalMapper.approval_result(pending, decision)
