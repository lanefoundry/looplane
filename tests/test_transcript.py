from looplane.transcript import (
    AssistantItem,
    NoticeItem,
    ToolGroupItem,
    ToolItem,
    TranscriptReducer,
    UserItem,
    safe_text,
)


def test_transcript_models_all_item_types_with_stable_ids() -> None:
    transcript = TranscriptReducer()

    user = transcript.add_user("hello")
    assistant = transcript.add_assistant("hi")
    notice = transcript.add_notice("working")
    transcript.add_context("repo", "/tmp/example")
    transcript.add_check("pytest", status="running")
    tool = transcript.tool_requested("call-1", "read_file")

    assert isinstance(user, UserItem)
    assert isinstance(assistant, AssistantItem)
    assert isinstance(notice, NoticeItem)
    assert [item.kind for item in transcript.items] == [
        "user",
        "assistant",
        "notice",
        "context",
        "check",
        "tool",
    ]
    assert len({item.id for item in transcript.items}) == 6
    assert tool.id == "tool:call-1"


def test_interleaved_tool_lifecycles_update_the_correlated_items_in_place() -> None:
    transcript = TranscriptReducer()
    first = transcript.tool_requested("a", "read_file", detail="queued A")
    second = transcript.tool_requested("b", "shell", detail="queued B")

    transcript.tool_started("b", "shell", detail="running B")
    transcript.tool_completed("a", "read_file", detail="10 lines")
    transcript.tool_completed("b", "shell", ok=False, detail="exit 1")

    first_after, second_after = transcript.items
    assert isinstance(first_after, ToolItem)
    assert isinstance(second_after, ToolItem)
    assert first_after.id == first.id
    assert first_after.status == "completed"
    assert first_after.detail == "10 lines"
    assert second_after.id == second.id
    assert second_after.status == "failed"
    assert second_after.detail == "exit 1"


def test_apply_tool_event_accepts_runtime_event_shape() -> None:
    transcript = TranscriptReducer()

    transcript.apply_tool_event("tool.requested", {"tool_call_id": "call-1", "name": "grep"})
    transcript.apply_tool_event(
        "tool.completed",
        {"tool_call_id": "call-1", "name": "grep", "ok": True, "preview": "3 matches"},
    )

    item = transcript.items[0]
    assert isinstance(item, ToolItem)
    assert item.status == "completed"
    assert item.detail_kind == "search"
    assert item.detail == "3 matches"


def test_consecutive_reads_and_searches_form_one_stable_collapsed_group() -> None:
    transcript = TranscriptReducer()
    transcript.tool_requested("read-1", "read_file")
    transcript.tool_requested("search-1", "grep")

    projected = transcript.project()
    assert len(projected) == 1
    group = projected[0]
    assert isinstance(group, ToolGroupItem)
    assert group.id == "tool-group:tool:read-1"
    assert group.collapsed is True
    assert [tool.detail_kind for tool in group.tools] == ["read", "search"]

    assert transcript.toggle_expansion(group.id) is True
    expanded = transcript.project()[0]
    assert isinstance(expanded, ToolGroupItem)
    assert expanded.id == group.id
    assert expanded.collapsed is False

    transcript.tool_requested("read-2", "read_file")
    enlarged = transcript.project()[0]
    assert isinstance(enlarged, ToolGroupItem)
    assert enlarged.id == group.id
    assert enlarged.collapsed is False
    assert len(enlarged.tools) == 3


def test_non_read_item_breaks_group_and_tool_expansion_is_independent() -> None:
    transcript = TranscriptReducer()
    first = transcript.tool_requested("read-1", "read_file", detail="a.py")
    transcript.add_assistant("Found it")
    transcript.tool_requested("search-1", "grep", detail="needle")

    assert len(transcript.project()) == 3
    assert transcript.toggle_expansion(first.id) is True
    changed = transcript.items[0]
    assert isinstance(changed, ToolItem)
    assert changed.expanded is True


def test_text_is_bounded_and_terminal_control_sequences_are_removed() -> None:
    transcript = TranscriptReducer(text_limit=8)
    item = transcript.add_notice("\x1b[31mred\x1b[0m\x00abcdef")

    assert item.text == "redabcd…"
    assert len(item.text) == 8
    assert "\x1b" not in item.text
    assert "\x00" not in item.text
    assert safe_text("a\r\nb\rc") == "a\nb\nc"


def test_markup_like_text_remains_literal_plain_text() -> None:
    transcript = TranscriptReducer()
    payload = "[bold]not markup[/bold] <script>alert(1)</script>"

    user = transcript.add_user(payload)
    tool = transcript.tool_requested("plain", "custom", detail=payload)

    assert user.text == payload
    assert tool.detail == payload
    assert tool.detail_kind == "plain"
