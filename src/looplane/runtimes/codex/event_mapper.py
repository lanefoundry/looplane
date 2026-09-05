"""Map Codex notifications into canonical events, owning item/preview state."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from looplane.conversation_runtime import (
    ActionPreviewUpdatedEvent,
    CompactionCompletedEvent,
    CompactionStartedEvent,
    ContextUsageUpdatedEvent,
    ConversationProtocolError,
    NoticeEvent,
    RuntimeSkillsChangedEvent,
    RuntimeToolKind,
    RuntimeTurnStatus,
    TextDeltaEvent,
    ToolCompletedEvent,
    ToolOutputDeltaEvent,
    ToolStartedEvent,
    TurnCompletedEvent,
    TurnStartedEvent,
)
from looplane.conversation_runtime import RuntimeToolStatus as RuntimeToolStatus
from looplane.runtime_semantics import (
    ContextTelemetry,
    ContextTelemetryAccuracy,
    ProposedChange,
    ProposedChangeKind,
)
from looplane.runtimes.codex import parsing as _codex_parsing
from looplane.runtimes.codex import tool_mapper as _codex_tools
from looplane.runtimes.codex.correlation import CodexCorrelation

_LOG = logging.getLogger("looplane.codex_app_server")


class EventEmitter(Protocol):
    def __call__(self, cls: type[Any], **kwargs: Any) -> None: ...


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


class CodexEventMapper:
    def __init__(
        self,
        *,
        correlation: CodexCorrelation,
        emit: EventEmitter,
        bounded: Callable[[str], str],
        new_id: Callable[[], str],
        stderr_tail: Callable[[], str],
        working_directory: Path,
    ) -> None:
        self.correlation = correlation
        self.emit = emit
        self.bounded = bounded
        self.new_id = new_id
        self.stderr_tail = stderr_tail
        self.working_directory = working_directory
        self.started_actions: set[str] = set()

        self.action_approval_context: dict[str, str] = {}

        self.action_previews: dict[str, tuple[ProposedChange, ...]] = {}

        self.preview_change_ids: dict[tuple[str, tuple[str, ...]], str] = {}

        self.turn_diffs: dict[str, str] = {}

    def handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method in _IGNORED_NOTIFICATIONS:
            return
        if method == "turn/started":
            turn = params.get("turn")
            if not isinstance(turn, dict) or not self.safe_id(turn.get("id")):
                raise ConversationProtocolError("turn/started is malformed")
            native_turn = turn["id"]
            if (
                native_turn not in self.correlation.native_turns
                and self.correlation.starting_turn is None
                and self.correlation.active_turn is not None
            ):
                # Codex 0.149+ can replace its internal turn (for example after a
                # failed collab spawn) and announce a brand-new native turn id on
                # the same connection.  The user's logical turn is still the one
                # looplane started, so adopt the replacement instead of failing the
                # whole conversation.
                _LOG.warning(
                    "codex app-server: adopting server-initiated turn %r into the "
                    "active local turn (previous native binding retained)",
                    native_turn,
                )
                self.correlation.adopt_turn(native_turn, self.correlation.active_turn)
            self.emit_turn_started(self.correlation.local_turn(native_turn, context="turn/started"))
            return
        if method == "turn/completed":
            self.complete_turn(params)
            return
        if method == "thread/tokenUsage/updated":
            self.observe_token_usage(params)
            return
        if method == "thread/compacted":
            self.emit_compaction_completed(
                self.correlation.correlated_turn(params, context="thread/compacted")
            )
            return
        if method == "turn/diff/updated":
            self.observe_turn_diff(params)
            return
        if method == "skills/changed":
            self.observe_skills_changed(params)
            return
        if method == "item/agentMessage/delta":
            turn = self.correlation.correlated_turn(params, context=method)
            delta = params.get("delta")
            if not isinstance(delta, str) or not delta:
                raise ConversationProtocolError("agent message delta is malformed")
            self.emit(TextDeltaEvent, turn_id=turn, text=self.bounded(delta))
            return
        if method in {"item/started", "item/completed"}:
            self.handle_item(method, params)
            return
        if method == "item/fileChange/patchUpdated":
            self.handle_file_change_preview(params)
            return
        if method in {
            "item/commandExecution/outputDelta",
            "item/mcpToolCall/progress",
        }:
            self.handle_tool_delta(method, params)
            return
        if method == "error":
            if params.get("willRetry") is True:
                return
            turn = self.correlation.correlated_turn(params, context="error")
            error = self.bounded(str(params.get("error", "Codex turn failed")))
            self.terminal(turn, RuntimeTurnStatus.FAILED, error)
            return
        if method == "warning":
            self.observe_warning(params)
            return
        raise ConversationProtocolError(f"unsupported server notification: {method}")

    def observe_token_usage(self, params: dict[str, Any]) -> None:
        # Telemetry only: a drifted payload must not end the conversation.
        try:
            turn = self.correlation.correlated_turn(params, context="thread/tokenUsage/updated")
            self.emit(
                ContextUsageUpdatedEvent,
                turn_id=turn,
                telemetry=self.context_telemetry(params.get("tokenUsage")),
            )
        except ConversationProtocolError:
            _LOG.warning(
                "codex app-server: dropping malformed token usage notification; recent stderr: %s",
                self.stderr_tail(),
            )

    def observe_turn_diff(self, params: dict[str, Any]) -> None:
        # Display-only delta text: a drifted payload must not end the conversation.
        try:
            turn = self.correlation.correlated_turn(params, context="turn/diff/updated")
            diff = params.get("diff")
            if not isinstance(diff, str):
                raise ConversationProtocolError("turn diff update is malformed")
            self.turn_diffs[turn] = self.bounded(diff)[:64000]
        except ConversationProtocolError:
            _LOG.warning(
                "codex app-server: dropping malformed turn diff notification; recent stderr: %s",
                self.stderr_tail(),
            )

    def observe_skills_changed(self, params: dict[str, Any]) -> None:
        # Observational runtime metadata: surfacing should not end the turn if
        # Codex changes this notification shape.
        try:
            thread = params.get("threadId")
            if thread is not None and thread != self.correlation.native_thread_id:
                raise ConversationProtocolError("skills change references the wrong thread")
            native_turn = params.get("turnId")
            if native_turn is not None:
                if not self.safe_id(native_turn):
                    raise ConversationProtocolError("skills change has invalid turn id")
                turn = self.correlation.local_turn(native_turn, context="skills/changed")
            else:
                turn = self.correlation.active_turn or self.correlation.starting_turn
                if turn is None:
                    return
            self.emit(
                RuntimeSkillsChangedEvent,
                turn_id=turn,
                source=self.skills_changed_source(params),
                skill_names=self.skills_changed_names(params.get("skills")),
                summary=self.skills_changed_summary(params),
            )
        except ConversationProtocolError:
            _LOG.warning(
                "codex app-server: dropping malformed skills/changed notification; "
                "recent stderr: %s",
                self.stderr_tail(),
            )

    def skills_changed_source(self, params: dict[str, Any]) -> str | None:
        value = params.get("source") or params.get("reason")
        if value is None:
            return None
        if not isinstance(value, str) or "\x00" in value:
            raise ConversationProtocolError("skills change source is malformed")
        value = self.bounded(value).strip()
        return value[:256] or None

    def skills_changed_summary(self, params: dict[str, Any]) -> str:
        value = params.get("message") or params.get("summary")
        if value is None:
            return "Runtime skill set changed."
        if not isinstance(value, str) or "\x00" in value:
            raise ConversationProtocolError("skills change summary is malformed")
        return self.bounded(value).strip()[:4000] or "Runtime skill set changed."

    def skills_changed_names(self, raw: object) -> tuple[str, ...]:
        if raw is None:
            return ()
        if not isinstance(raw, list):
            raise ConversationProtocolError("skills change list is malformed")
        names: list[str] = []
        seen: set[str] = set()
        for item in raw[:256]:
            if isinstance(item, str):
                name = item
            elif isinstance(item, dict):
                name = item.get("name")
            else:
                raise ConversationProtocolError("skills change entry is malformed")
            if not isinstance(name, str) or "\x00" in name:
                raise ConversationProtocolError("skills change name is malformed")
            name = self.bounded(name).strip()[:256]
            if name and name not in seen:
                names.append(name)
                seen.add(name)
        return tuple(names)

    def observe_warning(self, params: dict[str, Any]) -> None:
        # Secondary notice: a drifted payload must not end the conversation.
        message = params.get("message")
        try:
            if not isinstance(message, str) or not message:
                raise ConversationProtocolError("warning notification is malformed")
            thread_id = params.get("threadId")
            if thread_id is not None:
                if not self.safe_id(thread_id):
                    raise ConversationProtocolError("warning notification has invalid thread")
                if thread_id != self.correlation.native_thread_id:
                    raise ConversationProtocolError("warning notification has foreign thread")
        except ConversationProtocolError:
            _LOG.warning(
                "codex app-server: dropping malformed warning notification; recent stderr: %s",
                self.stderr_tail(),
            )
            return
        turn = self.correlation.active_turn or self.correlation.starting_turn
        if turn is not None and isinstance(message, str):
            self.emit(
                NoticeEvent,
                turn_id=turn,
                level="warning",
                text=self.bounded(message)[:16000],
            )

    def handle_item(self, method: str, params: dict[str, Any]) -> None:
        turn = self.correlation.correlated_turn(params, context=method)
        item = params.get("item")
        if not isinstance(item, dict) or not self.safe_id(item.get("id")):
            raise ConversationProtocolError("item lifecycle notification is malformed")
        item_type = item.get("type")
        if item_type == "contextCompaction":
            if method == "item/started":
                self.emit_compaction_started(turn)
            else:
                self.emit_compaction_completed(turn)
            return
        if item_type in _NON_TOOL_ITEM_TYPES:
            return
        if item_type not in _TOOL_ITEM_TYPES:
            raise ConversationProtocolError(f"unsupported Codex item type: {item_type!r}")
        native_turn = params["turnId"]
        action = self.correlation.local_action(native_turn, item["id"])
        if method == "item/started":
            if action in self.started_actions:
                raise ConversationProtocolError("duplicate tool start")
            self.started_actions.add(action)
            kind, name, effect, summary, path, paths = self.tool_description(item_type, item)
            self.action_approval_context[action] = self.tool_approval_context(
                kind=kind,
                summary=summary,
                path=path,
                paths=paths,
            )
            if item_type == "fileChange":
                changes = self.proposed_changes(action, item.get("changes"))
                if changes:
                    self.action_previews[action] = changes
            self.emit(
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
        if action not in self.started_actions:
            raise ConversationProtocolError("tool completed before it started")
        self.started_actions.remove(action)
        self.action_approval_context.pop(action, None)
        status = self.tool_status(item.get("status"))
        self.emit(
            ToolCompletedEvent,
            turn_id=turn,
            action_id=action,
            status=status,
            summary=self.tool_completion_summary(item_type, item),
            output=self.tool_completion_output(item_type, item),
            diff=self.tool_completion_diff(item_type, item),
        )

    def handle_tool_delta(self, method: str, params: dict[str, Any]) -> None:
        turn = self.correlation.correlated_turn(params, context=method)
        native_item = params.get("itemId")
        if not self.safe_id(native_item):
            raise ConversationProtocolError("tool delta has invalid item id")
        action = self.correlation.native_actions.get((params["turnId"], native_item))
        if action is None or action not in self.started_actions:
            raise ConversationProtocolError("tool delta preceded tool start")
        value = params.get("delta" if method.endswith("outputDelta") else "message")
        if not isinstance(value, str) or not value:
            raise ConversationProtocolError("tool output delta is malformed")
        self.emit(
            ToolOutputDeltaEvent,
            turn_id=turn,
            action_id=action,
            text=self.bounded(value),
        )

    def handle_file_change_preview(self, params: dict[str, Any]) -> None:
        turn = self.correlation.correlated_turn(params, context="item/fileChange/patchUpdated")
        native_item = params.get("itemId")
        if not self.safe_id(native_item):
            raise ConversationProtocolError("file change preview has invalid item id")
        action = self.correlation.native_actions.get((params["turnId"], native_item))
        if action is None or action not in self.started_actions:
            raise ConversationProtocolError("file change preview preceded tool start")
        changes = self.proposed_changes(action, params.get("changes"))
        if not changes:
            raise ConversationProtocolError("file change preview contains no changes")
        self.action_previews[action] = changes
        self.emit(
            ActionPreviewUpdatedEvent,
            turn_id=turn,
            action_id=action,
            proposed_changes=changes,
        )

    def proposed_changes(self, action_id: str, raw_changes: object) -> tuple[ProposedChange, ...]:
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
            shown_diff, original_bytes, truncated = self.preview_diff(diff)
            key = (action_id, paths)
            change_id = self.preview_change_ids.setdefault(key, self.new_id())
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

    preview_diff = staticmethod(_codex_parsing.preview_diff)

    def file_change_grant_scope(self, action_id: str) -> str | None:
        changes = self.action_previews.get(action_id, ())
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
    def context_telemetry(raw: object) -> ContextTelemetry:
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

    def emit_compaction_started(self, turn: str) -> None:
        if turn in self.correlation.started_compactions:
            return
        self.correlation.compaction_turns.add(turn)
        self.correlation.started_compactions.add(turn)
        self.correlation.active_turn = turn
        self.emit(CompactionStartedEvent, turn_id=turn, guidance=None)

    def emit_compaction_completed(self, turn: str) -> None:
        self.emit_compaction_started(turn)
        if turn in self.correlation.completed_compactions:
            return
        self.correlation.completed_compactions.add(turn)
        self.emit(CompactionCompletedEvent, turn_id=turn, checkpoint=None)

    def complete_turn(self, params: dict[str, Any]) -> None:
        turn = params.get("turn")
        if not isinstance(turn, dict) or not self.safe_id(turn.get("id")):
            raise ConversationProtocolError("turn/completed is malformed")
        local = self.correlation.local_turn(turn["id"], context="turn/completed")
        native_turn = turn["id"]
        if any(
            action in self.started_actions
            for (action_turn, _), action in self.correlation.native_actions.items()
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
        if local in self.correlation.compaction_turns:
            if local in self.correlation.completed_turns:
                raise ConversationProtocolError("duplicate terminal turn event")
            if raw_status == "completed":
                self.emit_compaction_completed(local)
            self.correlation.completed_turns.add(local)
            if self.correlation.active_turn == local:
                self.correlation.active_turn = None
            self.correlation.compaction_turns.discard(local)
            return
        error: str | None = None
        if raw_status == "failed":
            error = self.bounded(str(turn.get("error") or "Codex turn failed"))
        self.terminal(local, statuses[raw_status], error)

    def terminal(self, turn: str, status: RuntimeTurnStatus, error: str | None) -> None:
        if turn in self.correlation.completed_turns:
            raise ConversationProtocolError("duplicate terminal turn event")
        self.correlation.completed_turns.add(turn)
        if self.correlation.active_turn == turn:
            self.correlation.active_turn = None
        self.emit(TurnCompletedEvent, turn_id=turn, status=status, error=error)

    def emit_turn_started(self, turn: str) -> None:
        if turn in self.correlation.compaction_turns:
            self.emit_compaction_started(turn)
            return
        if turn in self.correlation.started_turns:
            return
        self.correlation.started_turns.add(turn)
        self.emit(TurnStartedEvent, turn_id=turn)

    def tool_approval_context(
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
        return self.bounded("\n".join(lines))[:16000]

    tool_description = staticmethod(_codex_tools.tool_description)

    def tool_completion_summary(self, item_type: str, item: dict[str, Any]) -> str:
        return _codex_tools.tool_completion_summary(item_type, item, bounded=self.bounded)

    def tool_completion_output(self, item_type: str, item: dict[str, Any]) -> str | None:
        return _codex_tools.tool_completion_output(item_type, item, bounded=self.bounded)

    def tool_completion_diff(self, item_type: str, item: dict[str, Any]) -> str | None:
        return _codex_tools.tool_completion_diff(item_type, item, bounded=self.bounded)

    tool_status = staticmethod(_codex_tools.tool_status)

    safe_id = staticmethod(_codex_parsing.safe_id)

    def approval_context(
        self,
        action_id: str,
    ) -> tuple[str, tuple[ProposedChange, ...], str | None]:
        return (
            self.action_approval_context.get(action_id, ""),
            self.action_previews.get(action_id, ()),
            self.file_change_grant_scope(action_id),
        )
