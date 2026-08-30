"""Long-lived Claude Agent SDK adapter for unified looplane conversations.

The official SDK is TypeScript-only in the supported local installation. looplane
therefore talks to a small pinned Node sidecar over a closed JSONL protocol.  All
Claude session/tool identifiers remain inside the sidecar; only looplane-generated
turn, action, and approval identifiers cross this module's public boundary.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from looplane.approvals import ApprovalDecision, ToolEffect
from looplane.conversation_runtime import (
    ActionPreviewUpdatedEvent,
    ApprovalRequestedEvent,
    ApprovalResolvedEvent,
    ContextUsageUpdatedEvent,
    ConversationProtocolError,
    ConversationRuntimeEvent,
    RuntimeApprovalKind,
    RuntimeApprovalRequest,
    RuntimeModelUpdatedEvent,
    RuntimeToolKind,
    RuntimeToolStatus,
    RuntimeTurnStatus,
    TextDeltaEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
    TurnCompletedEvent,
    TurnStartedEvent,
)
from looplane.runtime_semantics import ContextTelemetry, ProposedChange, RuntimeCapabilities

_SDK_VERSION = "0.1.77"
_SAFE_ENV_KEYS = {
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
_SECRET_ENV_MARKERS = ("API", "AUTH", "CREDENTIAL", "PASSWORD", "SECRET", "TOKEN")
_TOOL_CONTRACTS = {
    "Read": (RuntimeToolKind.READ, ToolEffect.READ),
    "Glob": (RuntimeToolKind.SEARCH, ToolEffect.READ),
    "Grep": (RuntimeToolKind.SEARCH, ToolEffect.READ),
    "Bash": (RuntimeToolKind.COMMAND, ToolEffect.EXECUTE),
    "Edit": (RuntimeToolKind.FILE_CHANGE, ToolEffect.MODIFY),
    "Write": (RuntimeToolKind.FILE_CHANGE, ToolEffect.MODIFY),
}


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ConversationProtocolError("sidecar frame contains duplicate keys")
        value[key] = item
    return value


@dataclass(frozen=True)
class _PendingApproval:
    turn_id: str
    action_id: str
    available: tuple[ApprovalDecision, ...]


class ClaudeAgentSession:
    """One ephemeral multi-turn session backed by Agent SDK 0.1.77."""

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            token_usage=True,
            native_compaction=False,
            proposed_file_preview=True,
            structured_approvals=True,
            queued_submissions=False,
            steer_active_turn=False,
            background_task_management=False,
        )

    def __init__(
        self,
        *,
        working_directory: str | Path,
        model: str | None = None,
        node_executable: str | Path = "node",
        sidecar_path: str | Path | None = None,
        sdk_path: str | Path | None = None,
        request_timeout_seconds: float = 30.0,
        shutdown_timeout_seconds: float = 3.0,
        max_input_bytes: int = 128_000,
        max_frame_bytes: int = 256_000,
        max_frames: int = 20_000,
        host_env: Mapping[str, str] | None = None,
    ) -> None:
        if request_timeout_seconds <= 0 or shutdown_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        if min(max_input_bytes, max_frame_bytes, max_frames) <= 0:
            raise ValueError("protocol bounds must be positive")
        candidate = Path(working_directory).expanduser()
        if candidate.is_symlink():
            raise ValueError("working_directory cannot be a symlink")
        self.working_directory = candidate.resolve(strict=True)
        if not self.working_directory.is_dir():
            raise ValueError("working_directory must be an existing directory")
        self.model = self._validate_model(model)
        self.node_executable = str(node_executable)
        self.sidecar_path = Path(sidecar_path).expanduser() if sidecar_path is not None else None
        self.sdk_path = Path(sdk_path).expanduser() if sdk_path is not None else None
        self.request_timeout_seconds = request_timeout_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.max_input_bytes = max_input_bytes
        self.max_frame_bytes = max_frame_bytes
        self.max_frames = max_frames
        self._host_env = host_env
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._turn_acks: dict[str, asyncio.Future[None]] = {}
        self._approval_acks: dict[str, tuple[asyncio.Future[None], ApprovalDecision]] = {}
        self._pending_approvals: dict[str, _PendingApproval] = {}
        self._event_queue: asyncio.Queue[ConversationRuntimeEvent | BaseException | None] = (
            asyncio.Queue()
        )
        self._active_turn: str | None = None
        self._started_turns: set[str] = set()
        self._terminal_turns: set[str] = set()
        self._started_actions: dict[str, tuple[str, str]] = {}
        self._next_sequence = 0
        self._frame_count = 0
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

    def _source_env(self) -> Mapping[str, str]:
        return os.environ if self._host_env is None else self._host_env

    def _resolve_node(self) -> str:
        if os.path.dirname(self.node_executable):
            candidate = Path(self.node_executable).expanduser().resolve(strict=False)
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                raise FileNotFoundError("Node executable is unavailable")
            return str(candidate)
        resolved = shutil.which(self.node_executable, path=self._source_env().get("PATH"))
        if resolved is None:
            raise FileNotFoundError("Node executable is unavailable")
        return resolved

    def _resolve_sidecar(self) -> Path:
        candidates: list[Path] = []
        if self.sidecar_path is not None:
            candidates.append(self.sidecar_path)
        else:
            configured = self._source_env().get(
                "LOOPLANE_CLAUDE_AGENT_SIDECAR"
            ) or self._source_env().get("PCA_CLAUDE_AGENT_SIDECAR")
            if configured:
                candidates.append(Path(configured).expanduser())
            candidates.append(
                Path(__file__).resolve().parents[2] / "scripts" / "claude-agent-session.mjs"
            )
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if resolved.is_file():
                return resolved
        raise FileNotFoundError(
            "Claude Agent SDK sidecar is unavailable; set LOOPLANE_CLAUDE_AGENT_SIDECAR"
        )

    def _resolve_sdk(self) -> Path:
        candidates: list[Path] = []
        if self.sdk_path is not None:
            candidates.append(self.sdk_path)
        else:
            configured = self._source_env().get(
                "LOOPLANE_CLAUDE_AGENT_SDK_PATH"
            ) or self._source_env().get("PCA_CLAUDE_AGENT_SDK_PATH")
            if configured:
                candidates.append(Path(configured).expanduser())
            candidates.append(
                Path(
                    "/opt/homebrew/lib/node_modules/oh-my-claude-sisyphus/node_modules/"
                    "@anthropic-ai/claude-agent-sdk"
                )
            )
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
                package = json.loads((resolved / "package.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                isinstance(package, dict)
                and package.get("name") == "@anthropic-ai/claude-agent-sdk"
                and package.get("version") == _SDK_VERSION
                and (resolved / "sdk.mjs").is_file()
            ):
                return resolved
        raise FileNotFoundError(
            "Claude Agent SDK 0.1.77 was not found; set LOOPLANE_CLAUDE_AGENT_SDK_PATH"
        )

    def _controlled_env(self) -> dict[str, str]:
        source = self._source_env()
        env = {key: value for key, value in source.items() if key in _SAFE_ENV_KEYS}
        env["PATH"] = env.get("PATH", os.defpath)
        env.update(
            {
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "GIT_ASKPASS": "/usr/bin/false",
                "GIT_TERMINAL_PROMPT": "0",
                "NO_COLOR": "1",
            }
        )
        assert not any(
            marker in key.upper()
            for key in env
            for marker in _SECRET_ENV_MARKERS
            if key != "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"
        )
        return env

    async def start(self) -> None:
        if self._process is not None or self._closed:
            raise RuntimeError("session cannot be started more than once")
        argv = [
            self._resolve_node(),
            str(self._resolve_sidecar()),
            "--sdk-path",
            str(self._resolve_sdk()),
            "--cwd",
            str(self.working_directory),
        ]
        if self.model is not None:
            argv.extend(("--model", self.model))
        self._process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.working_directory,
            env=self._controlled_env(),
            start_new_session=os.name == "posix",
            limit=self.max_frame_bytes + 1,
        )
        self._ready = asyncio.get_running_loop().create_future()
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            await asyncio.wait_for(asyncio.shield(self._ready), self.request_timeout_seconds)
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
        if self._active_turn is not None:
            raise RuntimeError("a turn is already active")
        turn_id = uuid4().hex
        ack: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._turn_acks[turn_id] = ack
        self._active_turn = turn_id
        try:
            await self._write_frame({"type": "turn", "turn_id": turn_id, "text": normalized})
            await asyncio.wait_for(asyncio.shield(ack), self.request_timeout_seconds)
        except BaseException:
            self._turn_acks.pop(turn_id, None)
            if self._active_turn == turn_id:
                self._active_turn = None
            raise
        self._emit_turn_started(turn_id)
        return turn_id

    def events(self) -> AsyncIterator[ConversationRuntimeEvent]:
        return self._events()

    async def compact_context(self, guidance: str | None = None) -> str:
        del guidance
        raise ConversationProtocolError("Claude native context compaction is unavailable")

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
        ack: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._approval_acks[request_id] = (ack, normalized)
        try:
            await self._write_frame(
                {"type": "approval", "request_id": request_id, "decision": normalized.value}
            )
            await asyncio.wait_for(asyncio.shield(ack), self.request_timeout_seconds)
        except BaseException:
            self._approval_acks.pop(request_id, None)
            raise

    async def interrupt(self, turn_id: str) -> None:
        self._ensure_ready()
        if turn_id != self._active_turn or turn_id in self._terminal_turns:
            raise ConversationProtocolError("turn is unknown or already terminal")
        await self._write_frame({"type": "interrupt", "turn_id": turn_id})

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is not None and process.returncode is None:
            with suppress(Exception):
                await self._write_frame({"type": "close"})
            # The sidecar and every SDK/Claude descendant share a dedicated
            # process group. Signal it while the group leader is still alive;
            # waiting for a graceful leader exit first can orphan descendants.
            self._terminate_process(process)
            try:
                await asyncio.wait_for(process.wait(), min(self.shutdown_timeout_seconds, 0.5))
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
        error = ConversationProtocolError("Claude Agent SDK session closed")
        if self._ready is not None and not self._ready.done():
            self._ready.set_exception(error)
        for future in self._turn_acks.values():
            if not future.done():
                future.set_exception(error)
        self._turn_acks.clear()
        for future, _ in self._approval_acks.values():
            if not future.done():
                future.set_exception(error)
        self._approval_acks.clear()
        await self._event_queue.put(None)

    def _ensure_ready(self) -> None:
        if self._fatal is not None:
            raise ConversationProtocolError("Claude Agent SDK session failed") from self._fatal
        if self._closed or self._process is None or self._ready is None or not self._ready.done():
            raise RuntimeError("session is not started")
        if self._process.returncode is not None:
            raise ConversationProtocolError("Claude Agent SDK sidecar exited")

    async def _write_frame(self, frame: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise ConversationProtocolError("Claude Agent SDK sidecar stdin is unavailable")
        payload = json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        if len(payload) > self.max_frame_bytes:
            raise ConversationProtocolError("outbound sidecar frame exceeds bound")
        async with self._write_lock:
            process.stdin.write(payload)
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise ConversationProtocolError("Claude Agent SDK sidecar pipe closed") from exc

    async def _reader_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while True:
                raw = await self._process.stdout.readline()
                if not raw:
                    if not self._closed:
                        raise ConversationProtocolError("Claude Agent SDK sidecar closed stdout")
                    return
                self._frame_count += 1
                if self._frame_count > self.max_frames or len(raw) > self.max_frame_bytes:
                    raise ConversationProtocolError("sidecar output exceeded protocol bounds")
                try:
                    frame = json.loads(
                        raw,
                        object_pairs_hook=_strict_json_object,
                        parse_constant=lambda _value: (_ for _ in ()).throw(
                            ConversationProtocolError("sidecar frame contains a non-finite number")
                        ),
                    )
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ConversationProtocolError("sidecar emitted invalid JSON") from exc
                if not isinstance(frame, dict):
                    raise ConversationProtocolError("sidecar frame must be an object")
                self._handle_frame(frame)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if not isinstance(exc, ConversationProtocolError):
                exc = ConversationProtocolError("Claude sidecar protocol failed")
            await self._fail(exc)

    def _handle_frame(self, frame: dict[str, Any]) -> None:
        frame_type = frame.get("type")
        if frame_type == "ready":
            self._exact_keys(frame, {"type", "sdk_version", "setting_sources"})
            if frame["sdk_version"] != _SDK_VERSION or frame["setting_sources"] != []:
                raise ConversationProtocolError("sidecar isolation handshake is invalid")
            if self._ready is None or self._ready.done():
                raise ConversationProtocolError("duplicate sidecar ready frame")
            self._ready.set_result(None)
            return
        if frame_type == "turn_accepted":
            self._exact_keys(frame, {"type", "turn_id"})
            turn_id = self._safe_id(frame["turn_id"], "turn_id")
            future = self._turn_acks.pop(turn_id, None)
            if future is None or future.done():
                raise ConversationProtocolError("turn acknowledgement is stale or unknown")
            future.set_result(None)
            return
        if frame_type == "approval_accepted":
            self._exact_keys(frame, {"type", "request_id"})
            request_id = self._safe_id(frame["request_id"], "request_id")
            acknowledged = self._approval_acks.pop(request_id, None)
            pending = self._pending_approvals.pop(request_id, None)
            if acknowledged is None or pending is None:
                raise ConversationProtocolError("approval acknowledgement is stale or unknown")
            future, decision = acknowledged
            self._emit(
                ApprovalResolvedEvent,
                turn_id=pending.turn_id,
                request_id=request_id,
                decision=decision,
            )
            future.set_result(None)
            return
        turn_id = self._frame_turn(frame)
        if frame_type == "text_delta":
            self._exact_keys(frame, {"type", "turn_id", "text"})
            text = self._safe_text(frame["text"], "text", 64_000)
            self._emit(TextDeltaEvent, turn_id=turn_id, text=text)
        elif frame_type == "tool_started":
            self._handle_tool_started(frame, turn_id)
        elif frame_type == "tool_completed":
            self._handle_tool_completed(frame, turn_id)
        elif frame_type == "action_preview_updated":
            self._handle_action_preview(frame, turn_id)
        elif frame_type == "approval_requested":
            self._handle_approval(frame, turn_id)
        elif frame_type == "context_usage_updated":
            self._handle_context_usage(frame, turn_id)
        elif frame_type == "runtime_model_updated":
            self._exact_keys(frame, {"type", "turn_id", "model"})
            self._emit(
                RuntimeModelUpdatedEvent,
                turn_id=turn_id,
                model=self._safe_text(frame["model"], "model", 256),
            )
        elif frame_type == "turn_completed":
            self._handle_turn_completed(frame, turn_id)
        elif frame_type == "fatal":
            self._exact_keys(frame, {"type", "turn_id", "error"})
            raise ConversationProtocolError(self._safe_text(frame["error"], "error", 4_000))
        else:
            raise ConversationProtocolError("sidecar emitted an unknown frame type")

    def _handle_tool_started(self, frame: dict[str, Any], turn_id: str) -> None:
        self._exact_keys(frame, {"type", "turn_id", "action_id", "tool_name", "summary", "path"})
        action_id = self._safe_id(frame["action_id"], "action_id")
        tool_name = self._safe_text(frame["tool_name"], "tool_name", 256)
        contract = _TOOL_CONTRACTS.get(tool_name)
        if contract is None or tool_name.startswith("mcp__") or tool_name in {"Agent", "Task"}:
            raise ConversationProtocolError("sidecar exposed a forbidden or unknown tool")
        if action_id in self._started_actions:
            raise ConversationProtocolError("duplicate tool action")
        self._started_actions[action_id] = (turn_id, tool_name)
        summary = self._safe_text(frame["summary"], "summary", 16_000, allow_blank=True)
        path = frame["path"]
        if path is not None:
            path = self._safe_text(path, "path", 4_096)
        self._emit(
            ToolStartedEvent,
            turn_id=turn_id,
            action_id=action_id,
            kind=contract[0],
            tool_name=tool_name,
            effect=contract[1],
            summary=summary,
            path=path,
        )

    def _handle_tool_completed(self, frame: dict[str, Any], turn_id: str) -> None:
        self._exact_keys(
            frame, {"type", "turn_id", "action_id", "status", "summary", "output", "diff"}
        )
        action_id = self._safe_id(frame["action_id"], "action_id")
        action = self._started_actions.get(action_id)
        if action is None or action[0] != turn_id:
            raise ConversationProtocolError("tool completion has no matching start")
        status = RuntimeToolStatus(frame["status"])
        summary = self._safe_text(frame["summary"], "summary", 16_000, allow_blank=True)
        output = self._optional_text(frame["output"], "output", 64_000)
        diff = self._optional_text(frame["diff"], "diff", 64_000)
        self._emit(
            ToolCompletedEvent,
            turn_id=turn_id,
            action_id=action_id,
            status=status,
            summary=summary,
            output=output,
            diff=diff,
        )
        del self._started_actions[action_id]

    def _handle_action_preview(self, frame: dict[str, Any], turn_id: str) -> None:
        self._exact_keys(frame, {"type", "turn_id", "action_id", "proposed_changes"})
        action_id = self._safe_id(frame["action_id"], "action_id")
        action = self._started_actions.get(action_id)
        if action is None or action[0] != turn_id:
            raise ConversationProtocolError("action preview has no matching tool action")
        changes = self._proposed_changes(frame["proposed_changes"], action_id)
        if not changes:
            raise ConversationProtocolError("action preview must contain a proposed change")
        self._emit(
            ActionPreviewUpdatedEvent,
            turn_id=turn_id,
            action_id=action_id,
            proposed_changes=changes,
        )

    def _handle_context_usage(self, frame: dict[str, Any], turn_id: str) -> None:
        self._exact_keys(frame, {"type", "turn_id", "telemetry"})
        try:
            telemetry = ContextTelemetry.model_validate(frame["telemetry"])
        except (TypeError, ValueError) as exc:
            raise ConversationProtocolError("context telemetry is malformed") from exc
        self._emit(ContextUsageUpdatedEvent, turn_id=turn_id, telemetry=telemetry)

    def _handle_approval(self, frame: dict[str, Any], turn_id: str) -> None:
        self._exact_keys(
            frame,
            {
                "type",
                "turn_id",
                "request_id",
                "action_id",
                "preview",
                "proposed_changes",
                "grant_scope",
            },
        )
        request_id = self._safe_id(frame["request_id"], "request_id")
        action_id = self._safe_id(frame["action_id"], "action_id")
        if request_id in self._pending_approvals:
            raise ConversationProtocolError("duplicate approval request")
        action = self._started_actions.get(action_id)
        if action is None or action[0] != turn_id:
            raise ConversationProtocolError("approval has no matching tool action")
        effect = _TOOL_CONTRACTS[action[1]][1]
        if effect not in {ToolEffect.MODIFY, ToolEffect.EXECUTE}:
            raise ConversationProtocolError("read-only tool requested approval")
        kind = (
            RuntimeApprovalKind.COMMAND
            if effect == ToolEffect.EXECUTE
            else RuntimeApprovalKind.FILE_CHANGE
        )
        proposed_changes = self._proposed_changes(frame["proposed_changes"], action_id)
        grant_scope = frame["grant_scope"]
        if grant_scope is not None:
            grant_scope = self._safe_text(grant_scope, "grant_scope", 4_096)
        available = (
            ApprovalDecision.ALLOW_ONCE,
            *((ApprovalDecision.ALLOW_SESSION,) if grant_scope is not None else ()),
            ApprovalDecision.DENY,
            ApprovalDecision.CANCEL,
        )
        self._pending_approvals[request_id] = _PendingApproval(turn_id, action_id, available)
        approval = RuntimeApprovalRequest(
            request_id=request_id,
            turn_id=turn_id,
            action_id=action_id,
            kind=kind,
            effect=effect,
            preview=self._safe_text(frame["preview"], "preview", 16_000),
            proposed_changes=proposed_changes,
            grant_scope=grant_scope,
            available_decisions=available,
        )
        self._emit(ApprovalRequestedEvent, turn_id=turn_id, approval=approval)

    @staticmethod
    def _proposed_changes(value: Any, action_id: str) -> tuple[ProposedChange, ...]:
        if not isinstance(value, list) or len(value) > 1_000:
            raise ConversationProtocolError("proposed changes are malformed")
        try:
            changes = tuple(ProposedChange.model_validate(item) for item in value)
        except (TypeError, ValueError) as exc:
            raise ConversationProtocolError("proposed changes are malformed") from exc
        if any(change.action_id != action_id for change in changes):
            raise ConversationProtocolError("proposed change references another action")
        return changes

    def _handle_turn_completed(self, frame: dict[str, Any], turn_id: str) -> None:
        self._exact_keys(frame, {"type", "turn_id", "status", "error"})
        if turn_id in self._terminal_turns:
            raise ConversationProtocolError("duplicate terminal turn")
        status = RuntimeTurnStatus(frame["status"])
        error = self._optional_text(frame["error"], "error", 16_000)
        if status == RuntimeTurnStatus.FAILED and not error:
            raise ConversationProtocolError("failed turn requires an error")
        if status != RuntimeTurnStatus.FAILED:
            error = None
        if any(action_turn == turn_id for action_turn, _ in self._started_actions.values()):
            raise ConversationProtocolError("turn completed with an unterminated tool action")
        self._terminal_turns.add(turn_id)
        if self._active_turn == turn_id:
            self._active_turn = None
        for request_id, pending in tuple(self._pending_approvals.items()):
            if pending.turn_id == turn_id:
                del self._pending_approvals[request_id]
        self._emit(TurnCompletedEvent, turn_id=turn_id, status=status, error=error)

    def _frame_turn(self, frame: dict[str, Any]) -> str:
        turn_id = self._safe_id(frame.get("turn_id"), "turn_id")
        if turn_id != self._active_turn or turn_id in self._terminal_turns:
            raise ConversationProtocolError("sidecar frame references an inactive turn")
        self._emit_turn_started(turn_id)
        return turn_id

    def _emit_turn_started(self, turn_id: str) -> None:
        if turn_id not in self._started_turns:
            self._started_turns.add(turn_id)
            self._emit(TurnStartedEvent, turn_id=turn_id)

    def _emit(self, event_type: type[Any], *, turn_id: str, **values: Any) -> None:
        event = event_type(sequence=self._next_sequence, turn_id=turn_id, **values)
        self._next_sequence += 1
        self._event_queue.put_nowait(event)

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while await self._process.stderr.read(64 * 1024):
            pass

    async def _fail(self, exc: BaseException) -> None:
        if self._fatal is not None or self._closed:
            return
        self._fatal = exc
        if self._ready is not None and not self._ready.done():
            self._ready.set_exception(exc)
        for future in self._turn_acks.values():
            if not future.done():
                future.set_exception(exc)
        self._turn_acks.clear()
        for future, _ in self._approval_acks.values():
            if not future.done():
                future.set_exception(exc)
        self._approval_acks.clear()
        await self._event_queue.put(exc)
        process = self._process
        if process is not None and process.returncode is None:
            self._terminate_process(process)
            try:
                await asyncio.wait_for(process.wait(), 0.5)
            except TimeoutError:
                self._kill_process(process)
                with suppress(Exception):
                    await process.wait()
            if os.name == "posix":
                self._kill_process(process)

    @staticmethod
    def _exact_keys(frame: dict[str, Any], expected: set[str]) -> None:
        if set(frame) != expected:
            raise ConversationProtocolError("sidecar frame has unexpected fields")

    @staticmethod
    def _safe_id(value: Any, label: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 128
            or not value.isascii()
            or not all(character.isalnum() or character in "-_" for character in value)
        ):
            raise ConversationProtocolError(f"invalid {label}")
        return value

    @staticmethod
    def _safe_text(value: Any, label: str, max_length: int, *, allow_blank: bool = False) -> str:
        if (
            not isinstance(value, str)
            or "\x00" in value
            or len(value) > max_length
            or (not allow_blank and not value)
        ):
            raise ConversationProtocolError(f"invalid {label}")
        return value

    @classmethod
    def _optional_text(cls, value: Any, label: str, max_length: int) -> str | None:
        return None if value is None else cls._safe_text(value, label, max_length, allow_blank=True)

    @staticmethod
    def _terminate_process(process: asyncio.subprocess.Process) -> None:
        if os.name == "posix":
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGTERM)
        elif process.returncode is None:
            process.terminate()

    @staticmethod
    def _kill_process(process: asyncio.subprocess.Process) -> None:
        if os.name == "posix":
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
