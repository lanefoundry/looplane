"""Owned event-to-view projection with no Textual or application state access."""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Literal
from uuid import uuid4

from looplane.approvals import ApprovalDecision, ApprovalRequest
from looplane.console import LiveEventProjection
from looplane.contracts import RunResult, RunStatus
from looplane.conversation_runtime import (
    ActionPreviewUpdatedEvent,
    ApprovalRequestedEvent,
    ApprovalResolvedEvent,
    CompactionCompletedEvent,
    CompactionStartedEvent,
    ContextUsageUpdatedEvent,
    ConversationRuntimeEvent,
    NoticeEvent,
    RuntimeModelUpdatedEvent,
    RuntimeTurnStatus,
    TextDeltaEvent,
    ToolOutputDeltaEvent,
    TurnStartedEvent,
)
from looplane.conversation_runtime import ToolCompletedEvent as RuntimeToolCompletedEvent
from looplane.conversation_runtime import ToolStartedEvent as RuntimeToolStartedEvent
from looplane.conversation_runtime import TurnCompletedEvent as RuntimeTurnCompletedEvent
from looplane.events import RunEvent
from looplane.external_agents import ExternalAgentEvent
from looplane.runtime_semantics import ContextTelemetry
from looplane.terminal.status import format_token_count
from looplane.terminal.types import LoadingPhase
from looplane.transcript import infer_tool_detail_kind
from looplane.transcript_export import TranscriptReducer


@dataclass(frozen=True)
class MessageView:
    item_id: str
    role: str
    content: str
    stream_turn_id: str | None = None


@dataclass(frozen=True)
class StreamAppend:
    turn_id: str
    text: str


@dataclass(frozen=True)
class TimelineView:
    title: str
    detail: str | None = None
    severity: str | None = None


@dataclass(frozen=True)
class ToolView:
    action_id: str
    title: str
    status: str
    detail: str
    detail_kind: str
    collapsed_detail: str | None


@dataclass(frozen=True)
class AliasTool:
    alias: str
    action_id: str


@dataclass(frozen=True)
class LoadingView:
    label: str | None
    phase: LoadingPhase = LoadingPhase.RESPONDING
    show_indicator: bool = True


@dataclass(frozen=True)
class StatusView:
    text: str


@dataclass(frozen=True)
class ActivityLine:
    text: str
    dim: bool = False


@dataclass(frozen=True)
class RefreshChrome:
    region: Literal["metrics", "context", "statusline"]


@dataclass(frozen=True)
class TrackItem:
    item_id: str


@dataclass(frozen=True)
class ContextPolicyObservation:
    """UI observation only; App/controller retain compaction decisions."""

    rearm: bool = False
    reminder: bool = False


ViewCommand = (
    MessageView
    | StreamAppend
    | TimelineView
    | ToolView
    | AliasTool
    | LoadingView
    | StatusView
    | ActivityLine
    | RefreshChrome
    | TrackItem
    | ContextPolicyObservation
)


@dataclass(frozen=True)
class ProjectionContext:
    mode: str = "agent"
    force_stopped: bool = False
    result: RunResult | None = None
    received_message: bool = False


@dataclass
class ProjectionState:
    projection_errors: int = 0
    turn_rendered_git_diff: bool = False
    latest_context_telemetry: ContextTelemetry | None = None
    runtime_reported_model: str | None = None
    turn_started_at: float | None = None
    last_turn_seconds: float | None = None
    stream_char_count: int = 0


class ToolPresentation:
    """One logical action, emitting immutable snapshots on semantic updates."""

    def __init__(
        self,
        action_id: str,
        title: str,
        detail: str | None,
        detail_kind: str,
        emit: Callable[[ViewCommand], None],
    ) -> None:
        self.action_id = action_id
        self.title = title
        self.detail = detail or ""
        self.detail_kind = detail_kind
        self.collapsed_detail: str | None = None
        self.status = "queued"
        self._emit = emit
        self.publish()

    def publish(self) -> None:
        self._emit(
            ToolView(
                self.action_id,
                self.title,
                self.status,
                self.detail,
                self.detail_kind,
                self.collapsed_detail,
            )
        )

    def set_title(self, title: str) -> None:
        self.title = title
        self.publish()

    def set_state(
        self,
        status: str,
        *,
        detail: str | None = None,
        detail_kind: str | None = None,
        collapsed_detail: str | None = None,
    ) -> None:
        self.status = status
        if detail is not None:
            self.detail = detail
        if detail_kind is not None:
            self.detail_kind = detail_kind
        if collapsed_detail is not None:
            self.collapsed_detail = collapsed_detail
        self.publish()


class TerminalProjection:
    """Semantic display state; callers drain commands and apply them behind a fence."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = monotonic,
        format_tokens: Callable[[int], str] = format_token_count,
    ) -> None:
        self.clock = clock
        self.format_tokens = format_tokens
        self.context = ProjectionContext()
        self.state = ProjectionState()
        self.reducer = TranscriptReducer()
        self._reducer = self.reducer
        self._projection = LiveEventProjection()
        self._commands: list[ViewCommand] = []
        self._tool_actions: dict[str, ToolPresentation] = {}
        self._turn_completed_verification_actions: set[str] = set()
        self._pending_verification_reuse: dict[str, Mapping[str, object]] = {}
        self._approval_actions: dict[str, str] = {}
        self._runtime_stream_text: dict[str, str] = {}
        self._runtime_stream_visible_length: dict[str, int] = {}
        self._runtime_stream_last_flush: dict[str, float] = {}
        self._runtime_text_blocks: set[str] = set()

    def drain(self) -> tuple[ViewCommand, ...]:
        commands = tuple(self._commands)
        self._commands.clear()
        return commands

    def project(
        self,
        event: RunEvent | ExternalAgentEvent | ConversationRuntimeEvent,
        context: ProjectionContext,
    ) -> tuple[ViewCommand, ...]:
        self.context = context
        if isinstance(event, RunEvent):
            self.event_received(event)
        elif isinstance(event, ExternalAgentEvent):
            self.external_event_received(event)
        else:
            self.conversation_runtime_event_received(event)
        return self.drain()

    def begin_turn(self) -> None:
        self._projection = LiveEventProjection()
        self.state.projection_errors = 0
        self._turn_completed_verification_actions.clear()
        self._pending_verification_reuse.clear()
        self.state.turn_rendered_git_diff = False
        for action_id in tuple(self._tool_actions):
            if action_id.startswith("verification:"):
                del self._tool_actions[action_id]

    def reset_transcript(self) -> None:
        self.reducer.reset()
        self._commands.clear()
        self._tool_actions.clear()
        self._pending_verification_reuse.clear()
        self._approval_actions.clear()
        self._runtime_stream_text.clear()
        self._runtime_stream_visible_length.clear()
        self._runtime_stream_last_flush.clear()
        self._runtime_text_blocks.clear()

    def write_turn(self, role: str, content: str) -> str:
        item_id = f"message:{uuid4().hex}"
        if content.strip():
            if role in {"You", "Task"}:
                self.reducer.add_user(content)
            elif role in {"Assistant", "Agent"}:
                self.reducer.add_assistant(content)
        self._commands.append(MessageView(item_id, role, content))
        return item_id

    def write_timeline(
        self, title: str, detail: str | None = None, *, severity: str | None = None
    ) -> None:
        self.reducer.add_notice(title, detail or "")
        self._commands.append(TimelineView(title, detail, severity))

    def _write_notice(self, text: str) -> None:
        self._commands.append(ActivityLine(text, dim=True))

    def _set_status(self, text: str) -> None:
        self._commands.append(StatusView(text))

    def _set_loading(
        self,
        label: str | None,
        *,
        phase: LoadingPhase = LoadingPhase.RESPONDING,
        show_indicator: bool = True,
    ) -> None:
        self._commands.append(LoadingView(label, phase, show_indicator))

    def _track_transcript_item(self, item_id: str) -> None:
        self._commands.append(TrackItem(item_id))

    def _update_metrics(self) -> None:
        self._commands.append(RefreshChrome("metrics"))

    def _refresh_context(self) -> None:
        self._commands.append(RefreshChrome("context"))

    def _mark_turn_finished(self) -> None:
        if self.state.turn_started_at is not None:
            self.state.last_turn_seconds = self.clock() - self.state.turn_started_at
            self.state.turn_started_at = None
        self.state.stream_char_count = 0
        self._update_metrics()
        self._commands.append(RefreshChrome("statusline"))

    def ensure_tool_action(
        self, action_id: str, title: str, *, detail: str | None = None, detail_kind: str = "plain"
    ) -> ToolPresentation:
        action = self._tool_actions.get(action_id)
        if action is not None:
            return action
        action = ToolPresentation(action_id, title, detail, detail_kind, self._commands.append)
        self._tool_actions[action_id] = action
        pending = self._pending_verification_reuse.pop(action_id, None)
        if pending is not None:
            self._apply_verification_reuse(action, pending)
        return action

    def prepare_approval(self, request: ApprovalRequest) -> tuple[ViewCommand, ...]:
        action = self._tool_actions.get(request.action_id)
        if action is None and request.tool_call is not None:
            action = self.ensure_tool_action(
                request.action_id,
                self.tool_title(request.tool_call.name, request.tool_call.arguments),
                detail_kind=self.tool_detail_kind(request.tool_call.name),
            )
            action.set_state("waiting", detail="Waiting for permission")
        if (
            action is not None
            and request.tool_call is not None
            and request.tool_call.name == "run_check"
            and request.preview.startswith("$ ")
        ):
            action.set_title(f"Run {request.preview[2:]}")
        return self.drain()

    def flush_runtime_stream_preview(self, turn_id: str, *, final: bool = False) -> bool:
        streamed = self._runtime_stream_text.get(turn_id, "")
        end = len(streamed)
        previous = self._runtime_stream_visible_length.get(turn_id, 0)
        if end <= previous:
            return previous > 0
        now = self.clock()
        pending = streamed[previous:end]
        if (
            not final
            and "\n" not in pending
            and len(pending) < 96
            and now - self._runtime_stream_last_flush.get(turn_id, 0.0) < 0.08
        ):
            return previous > 0
        if turn_id not in self._runtime_text_blocks:
            self._runtime_text_blocks.add(turn_id)
            self._commands.append(MessageView(f"message:{turn_id}", "Assistant", "", turn_id))
        else:
            self._track_transcript_item(f"message:{turn_id}")
        self._commands.append(StreamAppend(turn_id, pending))
        self._runtime_stream_visible_length[turn_id] = end
        self._runtime_stream_last_flush[turn_id] = now
        return True

    def finish_result(
        self, result: RunResult, context: ProjectionContext
    ) -> tuple[ViewCommand, ...]:
        self.context = context
        self._mark_turn_finished()
        self._set_status(self.result_status(result))
        if result.summary and (context.mode != "ask" or not context.received_message):
            self.write_turn("Assistant" if context.mode == "ask" else "Agent", result.summary)
        if result.changed_files:
            changed = ", ".join(result.changed_files)
            self._commands.append(ActivityLine("Changed: " + changed))
            if result.status != RunStatus.FAILED:
                self.write_timeline("Edited", changed)
        if result.status == RunStatus.FAILED:
            self.write_timeline("Run failed", self.failure_detail(result), severity="failure")
        for outcome in result.verification:
            action_id = f"verification:{outcome.name}"
            action = self.ensure_tool_action(
                action_id, self.verification_title(outcome.name, outcome.argv)
            )
            if action.action_id == action_id:
                action.set_title(self.verification_title(outcome.name, outcome.argv))
            summary = self.verification_summary(
                outcome.ok, outcome.exit_code, outcome.duration_seconds
            )
            detail = self.verification_detail(summary, outcome.output)
            action.set_state(
                "completed" if outcome.ok else "failed",
                detail=detail,
                detail_kind="plain",
                collapsed_detail=summary,
            )
            if action_id not in self._turn_completed_verification_actions:
                self.reducer.add_tool(
                    action.title, "completed" if outcome.ok else "failed", summary
                )
            self._turn_completed_verification_actions.add(action_id)
        if context.mode == "agent":
            self._commands.append(ActivityLine(f"Session: {result.run_id}"))
        return self.drain()

    def present_patch(self, run_id: str, filename: str, preview: str) -> tuple[ViewCommand, ...]:
        if preview and not self.state.turn_rendered_git_diff:
            action = self.ensure_tool_action(
                f"artifact:patch:{run_id}", f"Diff · {filename}", detail_kind="diff"
            )
            action.set_state("completed", detail=preview, detail_kind="diff")
        return self.drain()

    @staticmethod
    def one_line_error(error: str, *, max_chars: int = 160) -> str:
        rendered = " ".join(error.split())
        return rendered if len(rendered) <= max_chars else rendered[: max_chars - 1] + "…"

    @staticmethod
    def failure_detail(result: RunResult) -> str:
        lines = [f"Error: {result.error or result.terminal_reason.replace('_', ' ')}"]
        if result.changed_files:
            lines.append("Files changed before failure:")
            lines.extend(f"- {path}" for path in result.changed_files)
        else:
            lines.append("No file changes were reported before failure.")
        return "\n".join(lines)

    def result_status(self, result: RunResult) -> str:
        if self.context.force_stopped and result.status == RunStatus.CANCELLED:
            return "Force-stopped unresponsive runtime · ready"
        usage_suffix = ""
        if result.usage.total_tokens:
            usage_suffix = f" · {self.format_tokens(result.usage.total_tokens)} tokens"
        if result.status == RunStatus.FAILED:
            status = "Failed"
            if result.error:
                status += f" · {self.one_line_error(result.error)}"
            changed_count = len(result.changed_files)
            if changed_count:
                noun = "file" if changed_count == 1 else "files"
                status += f" · {changed_count} {noun} changed before failure"
            return status + usage_suffix
        status = f"{result.status.value} · {result.terminal_reason}"
        if self.context.mode == "agent":
            status += f" · {len(result.changed_files)} changed file(s)"
        return status + usage_suffix

    @staticmethod
    def verification_summary(
        ok: bool,
        exit_code: object,
        duration_seconds: object | None = None,
    ) -> str:
        summary = f"{'Passed' if ok else 'Failed'} · exit {exit_code}"
        if isinstance(duration_seconds, (int, float)):
            summary += f" · {duration_seconds:.2f}s"
        return summary

    @staticmethod
    def verification_detail(summary: str, output: object) -> str:
        rendered = str(output).strip() if output is not None else ""
        return f"{summary}\n{rendered}" if rendered else summary

    @staticmethod
    def structured_verification(value: object) -> Mapping[str, object] | None:
        if isinstance(value, Mapping):
            payload = value
        elif isinstance(value, str):
            try:
                decoded = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return None
            if not isinstance(decoded, Mapping):
                return None
            payload = decoded
        else:
            return None
        required = {"argv", "ok", "exit_code", "duration_seconds", "output"}
        return payload if required.issubset(payload) else None

    def _apply_verification_reuse(
        self,
        action: ToolPresentation,
        data: Mapping[str, object],
    ) -> None:
        name = str(data.get("name", "verification"))
        verification_id = f"verification:{name}"
        argv = data.get("argv")
        if isinstance(argv, (list, tuple)) and argv and all(isinstance(part, str) for part in argv):
            action.set_title(f"Run {shlex.join(argv)}")
        if action.status not in {"completed", "failed", "denied", "cancelled"}:
            ok = bool(data.get("ok"))
            summary = self.verification_summary(ok, data.get("exit_code"))
            action.set_state(
                "completed" if ok else "failed",
                detail=summary,
                detail_kind="plain",
                collapsed_detail=summary,
            )
        self._tool_actions[verification_id] = action
        self._commands.append(AliasTool(verification_id, action.action_id))
        self._turn_completed_verification_actions.add(verification_id)

    def settle_orphan_verification_actions(self, status: RunStatus) -> None:
        terminal = "cancelled" if status == RunStatus.CANCELLED else "failed"
        detail = (
            "Cancelled before verification finished"
            if status == RunStatus.CANCELLED
            else "Run ended before verification finished"
        )
        seen: set[int] = set()
        for action_id, action in self._tool_actions.items():
            if not action_id.startswith("verification:") or id(action) in seen:
                continue
            seen.add(id(action))
            if action.status in {"queued", "running", "waiting"}:
                action.set_state(
                    terminal,
                    detail=detail,
                    detail_kind="plain",
                    collapsed_detail=detail,
                )

    @staticmethod
    def tool_title(name: str, arguments: object) -> str:
        values = arguments if isinstance(arguments, dict) else {}
        path = values.get("path")
        if not isinstance(path, str):
            path = values.get("file_path")
        if name == "read_file":
            return f"Read {path}" if isinstance(path, str) else "Read file"
        if name == "list_files":
            return f"List {path}" if isinstance(path, str) else "List files"
        if name == "search_text":
            query = values.get("query") or values.get("pattern")
            return f'Search "{query}"' if isinstance(query, str) else "Search files"
        if name in {"replace_text", "apply_patch"}:
            return f"Update {path}" if isinstance(path, str) else "Update files"
        if name == "create_file":
            return f"Create {path}" if isinstance(path, str) else "Create file"
        if name == "run_check":
            check = values.get("name")
            return f"Run {check}" if isinstance(check, str) else "Run check"
        if name == "git_diff":
            return "Review changes"
        return name.replace("_", " ").capitalize()

    @staticmethod
    def verification_title(name: str, argv: object) -> str:
        if isinstance(argv, (list, tuple)) and argv and all(isinstance(part, str) for part in argv):
            return f"Check {shlex.join(argv)}"
        return f"Check {name}"

    @staticmethod
    def tool_detail_kind(name: str) -> str:
        return infer_tool_detail_kind(name)

    def event_received(self, event: RunEvent) -> None:
        try:
            projected = self._projection.apply(event)
        except ValueError as exc:
            # Display layer is best-effort: never let a single malformed event
            # end the Textual app. Same contract as CompositeEventSink —
            # secondary failures must not corrupt durable state.
            self.state.projection_errors += 1
            self._commands.append(ActivityLine(f"[projection error: {type(exc).__name__}: {exc}]"))
            return
        for line in projected:
            self._commands.append(ActivityLine(line))
        event_type = event.event_type
        data = event.data
        action_id = data.get("tool_call_id") or data.get("action_id")
        if event_type == "tool.requested" and isinstance(action_id, str):
            name = str(data.get("name", "tool"))
            title = self.tool_title(name, data.get("arguments"))
            action = self.ensure_tool_action(
                action_id,
                title,
                detail_kind=self.tool_detail_kind(name),
            )
            if action.status not in {"completed", "failed", "denied", "cancelled"}:
                action.set_title(title)
                action.set_state("queued")
        elif event_type == "tool.started" and isinstance(action_id, str):
            name = str(data.get("name", "tool"))
            action = self.ensure_tool_action(
                action_id,
                self.tool_title(name, {}),
                detail_kind=self.tool_detail_kind(name),
            )
            if action.status not in {"completed", "failed", "denied", "cancelled"}:
                action.set_state("running")
        elif event_type == "approval.requested" and isinstance(action_id, str):
            action = self._tool_actions.get(action_id)
            if action is not None:
                action.set_state("waiting", detail="Waiting for permission")
        elif event_type == "approval.resolved" and isinstance(action_id, str):
            action = self._tool_actions.get(action_id)
            if action is not None:
                decision = str(data.get("decision", ""))
                action.set_state(
                    "denied" if decision == "deny" else "running",
                    detail=("Permission denied" if decision == "deny" else "Permission granted"),
                )
        elif event_type == "tool.completed" and isinstance(action_id, str):
            name = str(data.get("name", "tool"))
            action = self.ensure_tool_action(
                action_id,
                self.tool_title(name, {}),
                detail_kind=self.tool_detail_kind(name),
            )
            ok = bool(data.get("ok"))
            if ok and name in {
                "apply_patch",
                "create_file",
                "replace_text",
                "tool_program",
                "tool_transaction",
            }:
                self.state.turn_rendered_git_diff = False
            elif name == "git_diff" and ok:
                self.state.turn_rendered_git_diff = True
            detail = data.get("preview") if ok else data.get("error")
            collapsed_detail = None
            detail_kind = "plain" if not ok else None
            if name == "run_check":
                structured = self.structured_verification(data.get("verification"))
                if structured is None:
                    structured = self.structured_verification(data.get("preview"))
                if structured is not None:
                    argv = structured.get("argv")
                    if (
                        isinstance(argv, (list, tuple))
                        and argv
                        and all(isinstance(part, str) for part in argv)
                    ):
                        action.set_title(f"Run {shlex.join(argv)}")
                    ok = bool(structured.get("ok"))
                    collapsed_detail = self.verification_summary(
                        ok,
                        structured.get("exit_code"),
                        structured.get("duration_seconds"),
                    )
                    detail = self.verification_detail(
                        collapsed_detail,
                        structured.get("output"),
                    )
                    detail_kind = "plain"
            action.set_state(
                "completed" if ok else "failed",
                detail=str(detail) if detail else None,
                detail_kind=detail_kind,
                collapsed_detail=collapsed_detail,
            )
            self._reducer.add_tool(
                action.title,
                "completed" if ok else "failed",
                str(detail) if detail else "",
            )
        elif event_type == "verification.reused":
            name = str(data.get("name", "verification"))
            verification_id = f"verification:{name}"
            self._turn_completed_verification_actions.add(verification_id)
            original_id = data.get("tool_call_id")
            if isinstance(original_id, str):
                action = self._tool_actions.get(original_id)
                if action is None:
                    self._pending_verification_reuse[original_id] = dict(data)
                else:
                    self._apply_verification_reuse(action, data)
        elif event_type == "verification.started":
            name = str(data.get("name", "verification"))
            action_id = f"verification:{name}"
            title = self.verification_title(name, data.get("argv"))
            action = self.ensure_tool_action(
                action_id,
                title,
            )
            if title != f"Check {name}" or action.title == f"Check {name}":
                action.set_title(title)
            if action.status not in {"completed", "failed", "denied", "cancelled"}:
                action.set_state("running")
        elif event_type == "verification.completed":
            name = str(data.get("name", "verification"))
            action_id = f"verification:{name}"
            self._turn_completed_verification_actions.add(action_id)
            title = self.verification_title(name, data.get("argv"))
            action = self.ensure_tool_action(
                action_id,
                title,
            )
            if title != f"Check {name}" or action.title == f"Check {name}":
                action.set_title(title)
            ok = bool(data.get("ok"))
            exit_code = data.get("exit_code")
            summary = self.verification_summary(
                ok,
                exit_code,
                data.get("duration_seconds"),
            )
            detail = self.verification_detail(summary, data.get("output"))
            action.set_state(
                "completed" if ok else "failed",
                detail=detail,
                detail_kind="plain",
                collapsed_detail=summary,
            )
            self._reducer.add_tool(action.title, "completed" if ok else "failed", summary)
        if event_type == "model.requested":
            self._set_loading("Thinking…", phase=LoadingPhase.REQUESTING)
        elif event_type in {"tool.requested", "tool.started"}:
            self._set_loading(f"Using {data.get('name', 'tool')}…", phase=LoadingPhase.TOOL_USE)
        elif event_type == "tool.completed":
            self._set_loading("Thinking…", phase=LoadingPhase.THINKING)
        elif event_type == "approval.requested":
            self._set_loading(None)
            self._set_status("Waiting for permission…")
        elif event_type == "approval.resolved":
            decision = str(data.get("decision", ""))
            if decision == "deny":
                self._set_loading(None)
                self._set_status("Permission denied")
            else:
                self._set_loading("Working…", phase=LoadingPhase.TOOL_USE)
        elif event_type == "verification.started":
            self._set_loading("Verifying…", phase=LoadingPhase.VERIFYING)
        elif event_type in {"verification.completed", "verification.reused"}:
            self._set_loading("Thinking…", phase=LoadingPhase.THINKING)

    def external_event_received(self, event: ExternalAgentEvent) -> None:
        if self.context.mode == "ask" and event.event_type == "message" and event.text:
            self._set_loading(None)
            self.write_turn("Assistant", event.text)
        elif self.context.mode == "agent" and event.event_type == "message" and event.text:
            self._set_loading(None)
            self.write_turn("Agent", event.text)
        elif self.context.mode == "agent" and event.event_type == "activity":
            item_type = event.data.get("item_type")
            if isinstance(item_type, str):
                label = item_type.replace("_", " ")
                self._set_loading(
                    f"Working · {label}",
                    phase=LoadingPhase.TOOL_USE,
                )
                # Activity is ephemeral phase state. Keeping it out of both the
                # durable transcript and hidden activity log avoids triplicate
                # status for a single runtime event.
                return
        if event.event_type == "result":
            self._set_loading(None)
            status = "Answer received…" if self.context.mode == "ask" else "Auditing patch…"
            self._set_status(status)
        elif event.event_type == "message":
            self._set_loading(
                "Responding…",
                phase=LoadingPhase.RESPONDING,
                show_indicator=False,
            )
        else:
            self._set_loading(
                "Read-only runtime working…"
                if self.context.mode == "ask"
                else "Delegated runtime working…",
                phase=LoadingPhase.RESPONDING,
            )

    def conversation_runtime_event_received(self, event: ConversationRuntimeEvent) -> None:
        if isinstance(event, TurnStartedEvent):
            self._runtime_stream_text[event.turn_id] = ""
            self._runtime_stream_visible_length[event.turn_id] = 0
            self.state.turn_started_at = self.clock()
            self.state.stream_char_count = 0
            self._set_loading("Thinking…", phase=LoadingPhase.REQUESTING)
            return
        if isinstance(event, TextDeltaEvent):
            streamed = self._runtime_stream_text.get(event.turn_id, "") + event.text
            self._runtime_stream_text[event.turn_id] = streamed
            self.state.stream_char_count += len(event.text)
            if self.state.stream_char_count % 256 < len(event.text):
                self._update_metrics()
            if self.flush_runtime_stream_preview(event.turn_id):
                self._set_loading(
                    "Responding…",
                    phase=LoadingPhase.RESPONDING,
                    show_indicator=False,
                )
            else:
                self._set_loading("Responding…", phase=LoadingPhase.RESPONDING)
        elif isinstance(event, NoticeEvent):
            self._write_notice(event.text)
            self._set_status(f"Warning · {event.text}")
            return
        elif isinstance(event, ContextUsageUpdatedEvent):
            self.state.latest_context_telemetry = event.telemetry
            if event.telemetry.context_window is not None:
                pressure = event.telemetry.total_tokens / event.telemetry.context_window
                if pressure <= 0.70:
                    self._commands.append(ContextPolicyObservation(rearm=True))
            self._update_metrics()
            return
        elif isinstance(event, RuntimeModelUpdatedEvent):
            self.state.runtime_reported_model = event.model
            self._refresh_context()
            self._update_metrics()
            return
        elif isinstance(event, CompactionStartedEvent):
            self._set_status("Compacting native context…")
            return
        elif isinstance(event, CompactionCompletedEvent):
            if event.checkpoint is not None:
                self.state.latest_context_telemetry = event.checkpoint.telemetry_after
            else:
                self.state.latest_context_telemetry = None
            self._commands.append(ContextPolicyObservation(reminder=True))
            self._set_status("Context compacted · ready")
            return
        if isinstance(event, RuntimeToolStartedEvent):
            self.flush_runtime_stream_preview(event.turn_id, final=True)
            title = event.tool_name
            if event.path:
                title = f"{title} {event.path}"
            action = self.ensure_tool_action(
                event.action_id,
                title,
                detail=event.summary or None,
                detail_kind=self.tool_detail_kind(event.kind.value),
            )
            action.set_state("running")
            self._set_loading(f"Using {event.tool_name}…", phase=LoadingPhase.TOOL_USE)
            return
        if isinstance(event, ToolOutputDeltaEvent):
            action = self._tool_actions.get(event.action_id)
            if action is not None:
                self._track_transcript_item(f"tool:{event.action_id}")
                combined = action.detail + event.text
                if len(combined) > 48_000:
                    combined = combined[:24_000] + "\n… output truncated …\n" + combined[-24_000:]
                action.set_state("running", detail=combined)
            return
        if isinstance(event, ActionPreviewUpdatedEvent):
            action = self._tool_actions.get(event.action_id)
            if action is not None:
                rendered = "\n\n".join(
                    change.unified_diff or change.summary for change in event.proposed_changes
                )
                action.set_state(
                    "waiting",
                    detail=rendered or "Proposed file change",
                    detail_kind=(
                        "diff"
                        if any(change.unified_diff for change in event.proposed_changes)
                        else "plain"
                    ),
                )
            return
        if isinstance(event, ApprovalRequestedEvent):
            self._set_loading(None)
            self._approval_actions[event.approval.request_id] = event.approval.action_id
            action = self._tool_actions.get(event.approval.action_id)
            if action is not None:
                action.set_state("waiting", detail=event.approval.preview)
            self._set_status("Waiting for permission…")
            return
        if isinstance(event, ApprovalResolvedEvent):
            action_id = self._approval_actions.pop(event.request_id, None)
            action = self._tool_actions.get(action_id) if action_id is not None else None
            allowed = event.decision in {
                ApprovalDecision.ALLOW_ONCE,
                ApprovalDecision.ALLOW_SESSION,
            }
            if action is not None:
                action.set_state(
                    "running" if allowed else "denied",
                    detail="Permission granted" if allowed else "Permission denied",
                )
            if allowed:
                self._set_loading(
                    f"Using {action.title}…" if action is not None else "Working…",
                    phase=LoadingPhase.TOOL_USE,
                )
            else:
                self._set_loading(None)
                self._set_status("Permission denied")
            return
        if isinstance(event, RuntimeToolCompletedEvent):
            action = self._tool_actions.get(event.action_id)
            if action is not None:
                self._track_transcript_item(f"tool:{event.action_id}")
                detail = event.diff or event.output or event.summary or None
                succeeded = event.status.value == "completed"
                action.set_state(
                    "completed" if succeeded else "failed",
                    detail=detail,
                    detail_kind=(
                        "diff" if succeeded and event.diff else "plain" if not succeeded else None
                    ),
                )
                self._reducer.add_tool(
                    action.title,
                    "completed" if succeeded else "failed",
                    detail or "",
                )
            self._set_loading("Thinking…", phase=LoadingPhase.THINKING)
            return
        if isinstance(event, RuntimeTurnCompletedEvent):
            self.flush_runtime_stream_preview(event.turn_id, final=True)
            final_stream_text = self._runtime_stream_text.get(event.turn_id)
            if final_stream_text:
                self._reducer.add_assistant(final_stream_text)
            self._runtime_stream_text.pop(event.turn_id, None)
            self._runtime_stream_visible_length.pop(event.turn_id, None)
            self._runtime_stream_last_flush.pop(event.turn_id, None)
            self._set_loading(None)
            self._mark_turn_finished()
            if self.context.result is not None:
                self._set_status(self.result_status(self.context.result))
                return
            if event.status == RuntimeTurnStatus.FAILED:
                self._set_status(
                    f"Failed · {self.one_line_error(event.error or 'Unknown runtime error')}"
                )
            else:
                self._set_status(
                    "Completed" if event.status.value == "completed" else event.status.value.title()
                )
