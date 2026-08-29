from __future__ import annotations

import json
from pathlib import Path

import pytest

from rivumi.session_replay import (
    MAX_REPLAY_TEXT_CHARS,
    ReplayValidationError,
    canonical_fork_seed_json,
    canonical_replay_json,
    fork_seed_at_sequence,
    reduce_events,
    reduce_jsonl,
)


def test_reduce_events_sorts_and_tracks_turn_state_deterministically() -> None:
    state = reduce_events(
        (
            {
                "sequence": 2,
                "event_type": "turn.completed",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "data": {"summary": "done"},
            },
            {
                "sequence": 0,
                "event_type": "run.created",
                "run_id": "run-1",
                "data": {"model": "gpt-5"},
            },
            {
                "sequence": 1,
                "event_type": "user.message",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "text": "fix it",
            },
        )
    )

    assert state.first_sequence == 0
    assert state.last_sequence == 2
    assert state.run_id == "run-1"
    assert state.active_turn_id is None
    assert state.completed_turn_ids == ("turn-1",)
    assert state.terminal_event_type == "turn.completed"
    assert [item.sequence for item in state.timeline] == [0, 1, 2]
    assert canonical_replay_json(tuple(reversed([item.as_dict() for item in state.timeline])))


def test_reduce_events_reduces_conversation_event_shape() -> None:
    state = reduce_events(
        (
            {
                "schema_version": 1,
                "conversation_id": "1" * 32,
                "sequence": 0,
                "event_id": "2" * 32,
                "event_type": "conversation.created",
            },
            {
                "schema_version": 1,
                "conversation_id": "1" * 32,
                "sequence": 1,
                "event_id": "3" * 32,
                "event_type": "user.message",
                "turn_id": "4" * 32,
                "text": "What changed?",
            },
            {
                "schema_version": 1,
                "conversation_id": "1" * 32,
                "sequence": 2,
                "event_id": "5" * 32,
                "event_type": "assistant.chunk",
                "turn_id": "4" * 32,
                "text": "Two files.",
            },
            {
                "schema_version": 1,
                "conversation_id": "1" * 32,
                "sequence": 3,
                "event_id": "6" * 32,
                "event_type": "turn.completed",
                "turn_id": "4" * 32,
            },
        )
    )

    assert state.conversation_id == "1" * 32
    assert state.active_turn_id is None
    assert state.completed_turn_ids == ("4" * 32,)
    assert [(item.event_type, item.text) for item in state.timeline] == [
        ("conversation.created", None),
        ("user.message", "What changed?"),
        ("assistant.chunk", "Two files."),
        ("turn.completed", None),
    ]


def test_reduce_events_accepts_runtime_event_shape() -> None:
    state = reduce_events(
        (
            {"event_type": "turn_started", "sequence": 0, "turn_id": "turn"},
            {"event_type": "text_delta", "sequence": 1, "turn_id": "turn", "text": "hi"},
            {
                "event_type": "tool_started",
                "sequence": 2,
                "turn_id": "turn",
                "tool_name": "shell",
                "summary": "pytest -q",
            },
            {
                "event_type": "turn_completed",
                "sequence": 3,
                "turn_id": "turn",
                "status": "completed",
            },
        )
    )

    assert state.completed_turn_ids == ("turn",)
    assert state.timeline[1].text == "hi"
    assert state.timeline[2].detail == "pytest -q"


def test_reduce_events_rejects_duplicate_sequences_and_id_drift() -> None:
    with pytest.raises(ReplayValidationError, match="duplicate event sequence"):
        reduce_events(
            (
                {"sequence": 1, "event_type": "run.created", "run_id": "run-1"},
                {"sequence": 1, "event_type": "run.completed", "run_id": "run-1"},
            )
        )

    with pytest.raises(ReplayValidationError, match="multiple run_id"):
        reduce_events(
            (
                {"sequence": 0, "event_type": "run.created", "run_id": "run-1"},
                {"sequence": 1, "event_type": "run.completed", "run_id": "run-2"},
            )
        )


@pytest.mark.parametrize(
    "event",
    [
        {"event_type": "message"},
        {"sequence": 0},
        {"event_type": "", "sequence": 0},
        {"event_type": "message", "sequence": -1},
        {"event_type": "message", "sequence": True},
        {"event_type": "message", "sequence": 0, "data": []},
        {"event_type": "message", "sequence": 0, "text": 42},
    ],
)
def test_reduce_events_rejects_invalid_or_missing_fields(event: dict[str, object]) -> None:
    with pytest.raises(ReplayValidationError):
        reduce_events([event])


def test_reduce_jsonl_loads_bounded_objects(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(event)
            for event in (
                {"event_type": "message", "sequence": 1, "text": "answer"},
                {"event_type": "run.created", "sequence": 0},
            )
        )
        + "\n",
        encoding="utf-8",
    )

    state = reduce_jsonl(path)

    assert [item.sequence for item in state.timeline] == [0, 1]
    assert state.event_count == 2


def test_reduce_jsonl_rejects_invalid_and_partial_lines(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.jsonl"
    invalid.write_text('{"sequence":0,"event_type":"run.created"}\n{bad}\n', encoding="utf-8")
    with pytest.raises(ReplayValidationError, match="not valid JSON"):
        reduce_jsonl(invalid)

    partial = tmp_path / "partial.jsonl"
    partial.write_text('{"sequence":0,"event_type":"run.created"}', encoding="utf-8")
    with pytest.raises(ReplayValidationError, match="partial final line"):
        reduce_jsonl(partial)


def test_replay_timeline_text_is_bounded(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps(
            {
                "sequence": 0,
                "event_type": "message",
                "text": "x" * (MAX_REPLAY_TEXT_CHARS + 10),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    state = reduce_jsonl(path)

    assert state.timeline[0].text == "x" * MAX_REPLAY_TEXT_CHARS


def test_replay_output_is_deterministic() -> None:
    first = (
        {"event_type": "run.completed", "run_id": "run-1", "sequence": 1},
        {"event_type": "run.created", "run_id": "run-1", "sequence": 0},
    )
    second = (
        {"run_id": "run-1", "sequence": 0, "event_type": "run.created"},
        {"sequence": 1, "event_type": "run.completed", "run_id": "run-1"},
    )

    assert canonical_replay_json(first) == canonical_replay_json(second)
    assert reduce_events(first).as_dict() == reduce_events(second).as_dict()


def test_fork_seed_at_sequence_builds_side_effect_free_prefix() -> None:
    events = (
        {"event_type": "run.completed", "run_id": "run-1", "sequence": 3},
        {
            "event_type": "tool.completed",
            "run_id": "run-1",
            "sequence": 2,
            "data": {"name": "apply_patch", "summary": "patched"},
        },
        {
            "event_type": "run.created",
            "run_id": "run-1",
            "sequence": 0,
            "data": {"provider": "scripted", "model": "scripted"},
        },
        {
            "event_type": "user.message",
            "run_id": "run-1",
            "sequence": 1,
            "turn_id": "turn-1",
            "text": "Fix it",
        },
    )

    seed = fork_seed_at_sequence(events, 2)

    assert seed.fork_point_sequence == 2
    assert seed.fork_point_event_type == "tool.completed"
    assert seed.source_run_id == "run-1"
    assert seed.events_included == 3
    assert seed.side_effects_replayed is False
    assert seed.run_started is False
    assert seed.replay_state.last_sequence == 2
    assert seed.replay_state.terminal_event_type is None
    assert [item.sequence for item in seed.replay_state.timeline] == [0, 1, 2]
    assert canonical_fork_seed_json(events, 2) == canonical_fork_seed_json(
        tuple(reversed(events)),
        2,
    )


def test_fork_seed_at_sequence_rejects_invalid_sequence() -> None:
    events = ({"event_type": "run.created", "run_id": "run-1", "sequence": 0},)

    with pytest.raises(ReplayValidationError, match="was not found"):
        fork_seed_at_sequence(events, 1)

    with pytest.raises(ReplayValidationError, match="non-negative integer"):
        fork_seed_at_sequence(events, -1)


def test_reduce_events_respects_event_count_bound() -> None:
    with pytest.raises(ReplayValidationError, match="exceeds 1 events"):
        reduce_events(
            (
                {"event_type": "run.created", "sequence": 0},
                {"event_type": "run.completed", "sequence": 1},
            ),
            max_events=1,
        )
