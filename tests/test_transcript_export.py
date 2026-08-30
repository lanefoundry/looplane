from __future__ import annotations

from looplane.transcript_export import (
    MAX_EXPORT_CHARS,
    TranscriptReducer,
)


def test_reducer_skips_empty_rows_and_preserves_order() -> None:
    reducer = TranscriptReducer()
    reducer.add_user("  ")
    reducer.add_user("first question")
    reducer.add_assistant("")
    reducer.add_tool("Update src/app.py", "completed", "+1 line")
    reducer.add_notice("Turn cancelled", "Interrupted.")
    reducer.add_assistant("final answer")

    exported = reducer.render(conversation_id="abc", resume_command="/resume abc")
    lines = exported.splitlines()
    assert lines[0] == "looplane session · conversation abc"
    assert lines[1] == "Resume with: /resume abc"
    assert "You › first question" in lines
    assert "Assistant › final answer" in lines
    assert "[ok] Update src/app.py" in lines
    assert "    +1 line" in lines
    assert "· Turn cancelled" in lines
    # No empty assistant/user rows leaked through.
    assert exported.count("You ›") == 1
    assert exported.count("Assistant ›") == 1


def test_reducer_render_without_rows_is_empty() -> None:
    reducer = TranscriptReducer()
    assert reducer.render(conversation_id=None, resume_command=None) == ""


def test_reducer_export_is_bounded() -> None:
    reducer = TranscriptReducer()
    filler = "x" * 2_000
    for _ in range(100):
        reducer.add_user(filler)
        reducer.add_assistant(filler)
    exported = reducer.render(conversation_id=None, resume_command=None)
    assert len(exported) <= MAX_EXPORT_CHARS + 64
    # Most recent rows survive; earliest rows are dropped.
    assert exported.count("You ›") < 100


def test_reducer_reset_clears_history() -> None:
    reducer = TranscriptReducer()
    reducer.add_user("hello")
    assert len(reducer) == 1
    reducer.reset()
    assert len(reducer) == 0
    assert reducer.render(conversation_id=None, resume_command=None) == ""
