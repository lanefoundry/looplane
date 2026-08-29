"""Deterministic reducers for Rivumi event-log replay foundations."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_REPLAY_EVENTS = 10_000
MAX_REPLAY_EVENT_BYTES = 128 * 1024
MAX_REPLAY_TEXT_CHARS = 16_000


class ReplayValidationError(ValueError):
    """Raised when an event log cannot be deterministically reduced."""


@dataclass(frozen=True)
class ReplayTimelineItem:
    sequence: int
    event_type: str
    run_id: str | None = None
    conversation_id: str | None = None
    turn_id: str | None = None
    text: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        return _drop_none(
            {
                "sequence": self.sequence,
                "event_type": self.event_type,
                "run_id": self.run_id,
                "conversation_id": self.conversation_id,
                "turn_id": self.turn_id,
                "text": self.text,
                "detail": self.detail,
            }
        )


@dataclass(frozen=True)
class ReplayState:
    schema_version: int
    first_sequence: int | None
    last_sequence: int | None
    event_count: int
    run_id: str | None
    conversation_id: str | None
    active_turn_id: str | None
    completed_turn_ids: tuple[str, ...]
    terminal_event_type: str | None
    timeline: tuple[ReplayTimelineItem, ...]

    def as_dict(self) -> dict[str, object]:
        return _drop_none(
            {
                "schema_version": self.schema_version,
                "first_sequence": self.first_sequence,
                "last_sequence": self.last_sequence,
                "event_count": self.event_count,
                "run_id": self.run_id,
                "conversation_id": self.conversation_id,
                "active_turn_id": self.active_turn_id,
                "completed_turn_ids": list(self.completed_turn_ids),
                "terminal_event_type": self.terminal_event_type,
                "timeline": [item.as_dict() for item in self.timeline],
            }
        )

    def canonical_json(self) -> str:
        """Return a stable JSON representation for hashing, fixtures, and future artifacts."""

        return json.dumps(
            self.as_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True)
class ReplayForkSeed:
    schema_version: int
    fork_point_sequence: int
    fork_point_event_type: str
    source_run_id: str | None
    source_conversation_id: str | None
    events_included: int
    side_effects_replayed: bool
    run_started: bool
    replay_state: ReplayState

    def as_dict(self) -> dict[str, object]:
        return _drop_none(
            {
                "schema_version": self.schema_version,
                "fork_point_sequence": self.fork_point_sequence,
                "fork_point_event_type": self.fork_point_event_type,
                "source_run_id": self.source_run_id,
                "source_conversation_id": self.source_conversation_id,
                "events_included": self.events_included,
                "side_effects_replayed": self.side_effects_replayed,
                "run_started": self.run_started,
                "replay_state": self.replay_state.as_dict(),
            }
        )

    def canonical_json(self) -> str:
        return json.dumps(
            self.as_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


def reduce_events(
    events: Iterable[Mapping[str, Any]],
    *,
    max_events: int = MAX_REPLAY_EVENTS,
) -> ReplayState:
    """Reduce bounded event dictionaries into a compact deterministic replay state."""

    if max_events < 0:
        raise ReplayValidationError("max_events must be non-negative")
    normalized = [_normalize_event(event, index) for index, event in enumerate(events)]
    if len(normalized) > max_events:
        raise ReplayValidationError(f"event log exceeds {max_events} events")
    return _reduce_normalized(normalized)


def reduce_jsonl(
    path: str | Path,
    *,
    max_events: int = MAX_REPLAY_EVENTS,
    max_event_bytes: int = MAX_REPLAY_EVENT_BYTES,
) -> ReplayState:
    """Load event JSONL from disk and reduce it into a deterministic replay state."""

    return reduce_events(
        _load_jsonl_events(path, max_event_bytes=max_event_bytes),
        max_events=max_events,
    )


def fork_seed_at_sequence(
    events: Iterable[Mapping[str, Any]],
    sequence: int,
    *,
    max_events: int = MAX_REPLAY_EVENTS,
) -> ReplayForkSeed:
    """Build a reviewable fork seed from event-log state through ``sequence``.

    The seed is a deterministic artifact only: it reduces prior events into state and
    deliberately does not replay tools, checks, subprocesses, or model calls.
    """

    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise ReplayValidationError("fork sequence must be a non-negative integer")
    if max_events < 0:
        raise ReplayValidationError("max_events must be non-negative")
    normalized = [_normalize_event(event, index) for index, event in enumerate(events)]
    if len(normalized) > max_events:
        raise ReplayValidationError(f"event log exceeds {max_events} events")
    ordered = sorted(normalized, key=lambda event: event["sequence"])
    _reduce_normalized(ordered)
    target_event = next((event for event in ordered if event["sequence"] == sequence), None)
    if target_event is None:
        raise ReplayValidationError(f"fork sequence {sequence} was not found")
    prefix = tuple(event for event in ordered if event["sequence"] <= sequence)
    replay_state = _reduce_normalized(prefix)
    return ReplayForkSeed(
        schema_version=1,
        fork_point_sequence=sequence,
        fork_point_event_type=target_event["event_type"],
        source_run_id=replay_state.run_id,
        source_conversation_id=replay_state.conversation_id,
        events_included=len(prefix),
        side_effects_replayed=False,
        run_started=False,
        replay_state=replay_state,
    )


def fork_seed_jsonl(
    path: str | Path,
    sequence: int,
    *,
    max_events: int = MAX_REPLAY_EVENTS,
    max_event_bytes: int = MAX_REPLAY_EVENT_BYTES,
) -> ReplayForkSeed:
    """Load event JSONL and build a deterministic fork seed artifact."""

    return fork_seed_at_sequence(
        _load_jsonl_events(path, max_event_bytes=max_event_bytes),
        sequence,
        max_events=max_events,
    )


def canonical_fork_seed_json(events: Iterable[Mapping[str, Any]], sequence: int) -> str:
    """Convenience wrapper for deterministic fork-seed output."""

    return fork_seed_at_sequence(events, sequence).canonical_json()


def canonical_replay_json(events: Iterable[Mapping[str, Any]]) -> str:
    """Convenience wrapper for deterministic reducer output."""

    return reduce_events(events).canonical_json()


def _load_jsonl_events(
    path: str | Path,
    *,
    max_event_bytes: int = MAX_REPLAY_EVENT_BYTES,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        with Path(path).open("rb") as file:
            for line_number, raw_line in enumerate(file, 1):
                line = raw_line.rstrip(b"\n")
                if not line.strip():
                    continue
                if not raw_line.endswith(b"\n"):
                    raise ReplayValidationError("event JSONL has a partial final line")
                if len(line) > max_event_bytes:
                    raise ReplayValidationError(
                        f"event JSONL line {line_number} exceeds {max_event_bytes} bytes"
                    )
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ReplayValidationError(
                        f"event JSONL line {line_number} is not valid JSON"
                    ) from exc
                if not isinstance(value, dict):
                    raise ReplayValidationError(
                        f"event JSONL line {line_number} must contain an object"
                    )
                events.append(value)
    except OSError as exc:
        raise ReplayValidationError(f"could not read event JSONL: {exc}") from exc
    return events


def _reduce_normalized(events: Sequence[dict[str, Any]]) -> ReplayState:
    ordered = sorted(events, key=lambda event: event["sequence"])
    seen_sequences: set[int] = set()
    timeline: list[ReplayTimelineItem] = []
    completed_turn_ids: list[str] = []
    run_id: str | None = None
    conversation_id: str | None = None
    active_turn_id: str | None = None
    terminal_event_type: str | None = None

    for event in ordered:
        sequence = event["sequence"]
        if sequence in seen_sequences:
            raise ReplayValidationError(f"duplicate event sequence: {sequence}")
        seen_sequences.add(sequence)

        event_type = event["event_type"]
        event_run_id = event.get("run_id")
        event_conversation_id = event.get("conversation_id")
        turn_id = event.get("turn_id")
        if event_run_id is not None:
            run_id = _stable_optional_id(run_id, event_run_id, label="run_id")
        if event_conversation_id is not None:
            conversation_id = _stable_optional_id(
                conversation_id,
                event_conversation_id,
                label="conversation_id",
            )

        if isinstance(turn_id, str):
            if event_type in {"user.message", "turn_started"}:
                active_turn_id = turn_id
            elif event_type in {
                "turn.completed",
                "turn.failed",
                "turn.cancelled",
                "turn.interrupted",
                "turn_completed",
            }:
                if turn_id not in completed_turn_ids:
                    completed_turn_ids.append(turn_id)
                if active_turn_id == turn_id:
                    active_turn_id = None

        if _is_terminal_event(event_type):
            terminal_event_type = event_type

        timeline.append(_timeline_item(event))

    first_sequence = ordered[0]["sequence"] if ordered else None
    last_sequence = ordered[-1]["sequence"] if ordered else None
    return ReplayState(
        schema_version=1,
        first_sequence=first_sequence,
        last_sequence=last_sequence,
        event_count=len(ordered),
        run_id=run_id,
        conversation_id=conversation_id,
        active_turn_id=active_turn_id,
        completed_turn_ids=tuple(completed_turn_ids),
        terminal_event_type=terminal_event_type,
        timeline=tuple(timeline),
    )


def _normalize_event(event: Mapping[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise ReplayValidationError(f"event {index} must be an object")
    sequence = event.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise ReplayValidationError(f"event {index} requires a non-negative integer sequence")
    event_type = event.get("event_type")
    if not isinstance(event_type, str) or not event_type.strip():
        raise ReplayValidationError(f"event {index} requires event_type")
    normalized: dict[str, Any] = {"sequence": sequence, "event_type": event_type.strip()}
    for key in ("run_id", "conversation_id", "turn_id"):
        value = event.get(key)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ReplayValidationError(f"event {index} has invalid {key}")
            normalized[key] = value.strip()
    data = event.get("data", {})
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise ReplayValidationError(f"event {index} data must be an object")
    normalized["data"] = dict(data)
    for key in (
        "text",
        "summary",
        "terminal_reason",
        "reason",
        "error",
        "tool",
        "tool_name",
        "name",
        "model",
        "runtime",
        "provider",
        "base_sha",
        "model_override",
    ):
        value = event.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise ReplayValidationError(f"event {index} has invalid {key}")
            normalized[key] = _bounded_text(value, label=key)
    return normalized


def _timeline_item(event: Mapping[str, Any]) -> ReplayTimelineItem:
    return ReplayTimelineItem(
        sequence=event["sequence"],
        event_type=event["event_type"],
        run_id=event.get("run_id"),
        conversation_id=event.get("conversation_id"),
        turn_id=event.get("turn_id"),
        text=_extract_text(event),
        detail=_extract_detail(event),
    )


def _extract_text(event: Mapping[str, Any]) -> str | None:
    text = event.get("text")
    if isinstance(text, str) and text:
        return _bounded_text(text, label="text")
    data = event.get("data")
    if isinstance(data, Mapping):
        value = data.get("text")
        if isinstance(value, str) and value:
            return _bounded_text(value, label="data.text")
    return None


def _extract_detail(event: Mapping[str, Any]) -> str | None:
    data = event.get("data")
    data = data if isinstance(data, Mapping) else {}
    for source in (event, data):
        for key in (
            "summary",
            "terminal_reason",
            "reason",
            "error",
            "tool",
            "tool_name",
            "name",
            "model",
            "runtime",
            "provider",
            "base_sha",
        ):
            value = source.get(key)
            if isinstance(value, str) and value:
                return _bounded_text(value, label=key)
    return None


def _stable_optional_id(current: str | None, value: str, *, label: str) -> str:
    if current is None or current == value:
        return value
    raise ReplayValidationError(f"event log contains multiple {label} values")


def _bounded_text(value: str, *, label: str) -> str:
    if "\x00" in value:
        raise ReplayValidationError(f"{label} cannot contain NUL")
    return value[:MAX_REPLAY_TEXT_CHARS]


def _is_terminal_event(event_type: str) -> bool:
    return event_type in {
        "run.completed",
        "run.failed",
        "run.cancelled",
        "turn.completed",
        "turn.failed",
        "turn.cancelled",
        "turn.interrupted",
        "turn_completed",
    }


def _drop_none(value: dict[str, object]) -> dict[str, object]:
    return {key: item for key, item in value.items() if item is not None}
