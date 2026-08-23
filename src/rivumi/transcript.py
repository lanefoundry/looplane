"""Typed, render-independent state for an interactive transcript.

The objects in this module contain plain text only.  A Textual (or other) view
can project them into widgets without having to own tool correlation, grouping,
or expansion state itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Literal, TypeAlias

ItemKind: TypeAlias = Literal["user", "assistant", "tool", "notice", "check", "context"]
ToolStatus: TypeAlias = Literal["queued", "running", "completed", "failed", "cancelled"]
ToolDetailKind: TypeAlias = Literal["read", "search", "command", "diff", "plain"]
CheckStatus: TypeAlias = Literal["queued", "running", "passed", "failed"]

DEFAULT_TEXT_LIMIT = 16_000

_ANSI_ESCAPE = re.compile(r"(?:\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~])")


def safe_text(value: object, *, limit: int = DEFAULT_TEXT_LIMIT) -> str:
    """Return bounded plain text with terminal escapes and unsafe controls removed."""

    if limit < 1:
        raise ValueError("text limit must be positive")
    text = _ANSI_ESCAPE.sub("", str(value)).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(character for character in text if character in "\n\t" or ord(character) >= 32)
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return f"{text[: limit - 1]}…"


@dataclass(frozen=True, slots=True)
class UserItem:
    id: str
    text: str
    kind: Literal["user"] = "user"


@dataclass(frozen=True, slots=True)
class AssistantItem:
    id: str
    text: str
    kind: Literal["assistant"] = "assistant"


@dataclass(frozen=True, slots=True)
class NoticeItem:
    id: str
    text: str
    kind: Literal["notice"] = "notice"


@dataclass(frozen=True, slots=True)
class ContextItem:
    id: str
    title: str
    detail: str = ""
    kind: Literal["context"] = "context"


@dataclass(frozen=True, slots=True)
class CheckItem:
    id: str
    name: str
    status: CheckStatus = "queued"
    detail: str = ""
    expanded: bool = False
    kind: Literal["check"] = "check"


@dataclass(frozen=True, slots=True)
class ToolItem:
    id: str
    tool_call_id: str
    name: str
    title: str
    status: ToolStatus = "queued"
    detail: str = ""
    detail_kind: ToolDetailKind = "plain"
    expanded: bool = False
    kind: Literal["tool"] = "tool"


@dataclass(frozen=True, slots=True)
class ToolGroupItem:
    """A view-only group of consecutive read/search tool entries."""

    id: str
    tools: tuple[ToolItem, ...]
    collapsed: bool = True
    kind: Literal["tool_group"] = "tool_group"


TranscriptItem: TypeAlias = (
    UserItem | AssistantItem | ToolItem | NoticeItem | CheckItem | ContextItem
)
ProjectedItem: TypeAlias = TranscriptItem | ToolGroupItem


def infer_tool_detail_kind(name: str) -> ToolDetailKind:
    """Classify common agent tool names without coupling to a backend."""

    normalized = name.casefold().replace("-", "_").replace(" ", "_")
    if normalized in {"list_files", "read", "read_file", "read_many_files", "view_file"}:
        return "read"
    if normalized in {
        "search",
        "search_text",
        "grep",
        "glob",
        "find",
        "find_files",
        "web_search",
    }:
        return "search"
    if normalized in {
        "bash",
        "shell",
        "command",
        "run",
        "run_check",
        "run_command",
        "exec_command",
    }:
        return "command"
    if normalized in {
        "apply_patch",
        "diff",
        "edit",
        "edit_file",
        "file_change",
        "replace_text",
        "write",
        "write_file",
    }:
        return "diff"
    return "plain"


class TranscriptReducer:
    """Own ordered transcript state and correlate lifecycle events by tool-call ID."""

    def __init__(self, *, text_limit: int = DEFAULT_TEXT_LIMIT) -> None:
        if text_limit < 1:
            raise ValueError("text_limit must be positive")
        self.text_limit = text_limit
        self._items: list[TranscriptItem] = []
        self._item_ids: set[str] = set()
        self._tool_indexes: dict[str, int] = {}
        self._group_expansion: dict[str, bool] = {}
        self._next_id = 1

    @property
    def items(self) -> tuple[TranscriptItem, ...]:
        return tuple(self._items)

    def clear(self) -> None:
        self._items.clear()
        self._item_ids.clear()
        self._tool_indexes.clear()
        self._group_expansion.clear()

    def add_user(self, text: object, *, item_id: str | None = None) -> UserItem:
        return self._append(UserItem(self._id("user", item_id), self._text(text)))

    def add_assistant(self, text: object, *, item_id: str | None = None) -> AssistantItem:
        return self._append(AssistantItem(self._id("assistant", item_id), self._text(text)))

    def add_notice(self, text: object, *, item_id: str | None = None) -> NoticeItem:
        return self._append(NoticeItem(self._id("notice", item_id), self._text(text)))

    def add_context(
        self, title: object, detail: object = "", *, item_id: str | None = None
    ) -> ContextItem:
        return self._append(
            ContextItem(self._id("context", item_id), self._text(title), self._text(detail))
        )

    def add_check(
        self,
        name: object,
        *,
        status: CheckStatus = "queued",
        detail: object = "",
        item_id: str | None = None,
    ) -> CheckItem:
        return self._append(
            CheckItem(
                self._id("check", item_id),
                self._text(name),
                status=status,
                detail=self._text(detail),
            )
        )

    def update_check(
        self, item_id: str, *, status: CheckStatus | None = None, detail: object | None = None
    ) -> CheckItem:
        index = self._index_for_item(item_id)
        current = self._items[index]
        if not isinstance(current, CheckItem):
            raise TypeError(f"transcript item {item_id!r} is not a check")
        updated = replace(
            current,
            status=current.status if status is None else status,
            detail=current.detail if detail is None else self._text(detail),
        )
        self._items[index] = updated
        return updated

    def tool_requested(
        self,
        tool_call_id: str,
        name: object,
        *,
        title: object | None = None,
        detail: object = "",
        detail_kind: ToolDetailKind | None = None,
    ) -> ToolItem:
        return self.upsert_tool(
            tool_call_id,
            name,
            title=title,
            status="queued",
            detail=detail,
            detail_kind=detail_kind,
        )

    def tool_started(
        self,
        tool_call_id: str,
        name: object = "tool",
        *,
        title: object | None = None,
        detail: object | None = None,
        detail_kind: ToolDetailKind | None = None,
    ) -> ToolItem:
        return self.upsert_tool(
            tool_call_id,
            name,
            title=title,
            status="running",
            detail=detail,
            detail_kind=detail_kind,
        )

    def tool_completed(
        self,
        tool_call_id: str,
        name: object = "tool",
        *,
        ok: bool = True,
        detail: object | None = None,
        detail_kind: ToolDetailKind | None = None,
    ) -> ToolItem:
        return self.upsert_tool(
            tool_call_id,
            name,
            status="completed" if ok else "failed",
            detail=detail,
            detail_kind=detail_kind,
        )

    def tool_cancelled(
        self, tool_call_id: str, name: object = "tool", *, detail: object | None = None
    ) -> ToolItem:
        return self.upsert_tool(tool_call_id, name, status="cancelled", detail=detail)

    def upsert_tool(
        self,
        tool_call_id: str,
        name: object,
        *,
        title: object | None = None,
        status: ToolStatus,
        detail: object | None = None,
        detail_kind: ToolDetailKind | None = None,
    ) -> ToolItem:
        if not tool_call_id:
            raise ValueError("tool_call_id must not be empty")
        tool_name = self._text(name)
        existing_index = self._tool_indexes.get(tool_call_id)
        if existing_index is None:
            item_id = self._id("tool", f"tool:{tool_call_id}")
            item = ToolItem(
                id=item_id,
                tool_call_id=tool_call_id,
                name=tool_name,
                title=self._text(tool_name if title is None else title),
                status=status,
                detail="" if detail is None else self._text(detail),
                detail_kind=detail_kind or infer_tool_detail_kind(tool_name),
            )
            self._tool_indexes[tool_call_id] = len(self._items)
            return self._append(item)

        current = self._items[existing_index]
        assert isinstance(current, ToolItem)
        resolved_name = tool_name if tool_name != "tool" else current.name
        resolved_title = current.title
        if title is not None:
            resolved_title = self._text(title)
        elif current.name == "tool" and current.title == "tool" and tool_name != "tool":
            resolved_title = tool_name
        resolved_detail_kind = detail_kind
        if resolved_detail_kind is None:
            inferred = infer_tool_detail_kind(resolved_name)
            resolved_detail_kind = inferred if inferred != "plain" else current.detail_kind
        status_rank = {"queued": 0, "running": 1, "completed": 2, "failed": 2, "cancelled": 2}
        resolved_status = (
            status if status_rank[status] >= status_rank[current.status] else current.status
        )
        updated = replace(
            current,
            name=resolved_name,
            title=resolved_title,
            status=resolved_status,
            detail=current.detail if detail is None else self._text(detail),
            detail_kind=resolved_detail_kind,
        )
        self._items[existing_index] = updated
        return updated

    def apply_tool_event(self, event_type: str, data: dict[str, object]) -> ToolItem | None:
        """Apply the ``tool.*`` event shape emitted by Rivumi runtimes."""

        call_id = data.get("tool_call_id") or data.get("action_id")
        if not isinstance(call_id, str):
            return None
        name = data.get("name", "tool")
        detail = data.get("preview")
        if detail is None:
            detail = data.get("detail")
        if event_type == "tool.requested":
            return self.tool_requested(call_id, name, detail="" if detail is None else detail)
        if event_type == "tool.started":
            return self.tool_started(call_id, name, detail=detail)
        if event_type == "tool.completed":
            return self.tool_completed(call_id, name, ok=data.get("ok") is not False, detail=detail)
        if event_type in {"tool.failed", "tool.cancelled"}:
            failure_detail = data.get("error", detail)
            if event_type == "tool.cancelled":
                return self.tool_cancelled(call_id, name, detail=failure_detail)
            return self.upsert_tool(call_id, name, status="failed", detail=failure_detail)
        return None

    def toggle_expansion(self, item_id: str) -> bool:
        """Toggle a tool/check detail or a projected group; return its new expanded state."""

        if item_id.startswith("tool-group:"):
            expanded = not self._group_expansion.get(item_id, False)
            self._group_expansion[item_id] = expanded
            return expanded
        index = self._index_for_item(item_id)
        current = self._items[index]
        if not isinstance(current, (ToolItem, CheckItem)):
            raise TypeError(f"transcript item {item_id!r} is not expandable")
        updated = replace(current, expanded=not current.expanded)
        self._items[index] = updated
        return updated.expanded

    def project(self) -> tuple[ProjectedItem, ...]:
        """Return items with consecutive read/search tools represented as collapsed groups."""

        projected: list[ProjectedItem] = []
        index = 0
        while index < len(self._items):
            item = self._items[index]
            if not isinstance(item, ToolItem) or item.detail_kind not in {"read", "search"}:
                projected.append(item)
                index += 1
                continue
            end = index + 1
            while end < len(self._items):
                candidate = self._items[end]
                if not isinstance(candidate, ToolItem) or candidate.detail_kind not in {
                    "read",
                    "search",
                }:
                    break
                end += 1
            tools = tuple(self._items[index:end])
            if len(tools) == 1:
                projected.append(item)
            else:
                assert all(isinstance(tool, ToolItem) for tool in tools)
                group_id = f"tool-group:{tools[0].id}"
                projected.append(
                    ToolGroupItem(
                        id=group_id,
                        tools=tools,  # type: ignore[arg-type]
                        collapsed=not self._group_expansion.get(group_id, False),
                    )
                )
            index = end
        return tuple(projected)

    def _text(self, value: object) -> str:
        return safe_text(value, limit=self.text_limit)

    def _id(self, prefix: str, item_id: str | None) -> str:
        if item_id is None:
            item_id = f"{prefix}:{self._next_id}"
            self._next_id += 1
        if not item_id:
            raise ValueError("item ID must not be empty")
        if item_id in self._item_ids:
            raise ValueError(f"duplicate transcript item ID: {item_id}")
        return item_id

    def _append(self, item: TranscriptItem):
        self._item_ids.add(item.id)
        self._items.append(item)
        return item

    def _index_for_item(self, item_id: str) -> int:
        for index, item in enumerate(self._items):
            if item.id == item_id:
                return index
        raise KeyError(item_id)
