from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from looplane.conversation import (
    MAX_MESSAGE_CHARS,
    ConversationBusyError,
    ConversationEvent,
    ConversationEventKind,
    ConversationStore,
    ConversationValidationError,
    default_conversation_root,
)


async def _completed_turn(
    store: ConversationStore,
    conversation_id: str,
    user: str,
    assistant_parts: tuple[str, ...],
) -> str:
    snapshot, lease = await store.resume(conversation_id)
    assert snapshot.manifest.conversation_id == conversation_id
    turn_id = uuid4().hex
    try:
        await store.append(
            lease,
            ConversationEventKind.USER_MESSAGE,
            turn_id=turn_id,
            text=user,
        )
        for part in assistant_parts:
            await store.append(
                lease,
                ConversationEventKind.ASSISTANT_CHUNK,
                turn_id=turn_id,
                text=part,
            )
        await store.append(
            lease,
            ConversationEventKind.TURN_COMPLETED,
            turn_id=turn_id,
        )
    finally:
        lease.release()
    return turn_id


def test_default_root_has_an_independent_state_contract(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("LOOPLANE_CONVERSATION_ROOT", raising=False)
    monkeypatch.delenv("PCA_CONVERSATION_ROOT", raising=False)

    assert default_conversation_root() == (tmp_path / "state" / "looplane" / "conversations")

    override = tmp_path / "private-conversations"
    monkeypatch.setenv("LOOPLANE_CONVERSATION_ROOT", str(override))
    assert default_conversation_root() == override


def test_default_root_discovers_legacy_conversations(tmp_path: Path, monkeypatch) -> None:
    state_root = tmp_path / "state"
    legacy = state_root / "python-coding-agent" / "conversations"
    legacy.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    monkeypatch.delenv("LOOPLANE_CONVERSATION_ROOT", raising=False)
    monkeypatch.delenv("PCA_CONVERSATION_ROOT", raising=False)

    assert default_conversation_root() == legacy


@pytest.mark.asyncio
async def test_create_round_trip_is_strict_private_and_vendor_neutral(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    snapshot = await store.create(
        runtime="codex-cli",
        model_override="gpt-5.6-terra",
        title="Deployment notes",
    )

    conversation_id = snapshot.manifest.conversation_id
    directory = store.root / conversation_id
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name in ("conversation.json", "events.jsonl", ".writer.lock"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    assert snapshot.manifest.last_event_sequence == 0
    assert snapshot.events[0].event_type is ConversationEventKind.CREATED

    persisted = (directory / "conversation.json").read_text() + (
        directory / "events.jsonl"
    ).read_text()
    for forbidden in ("vendor_session", "thread_id", "conversation_id_from_vendor", "data"):
        assert forbidden not in persisted

    with pytest.raises(ValidationError, match="Extra inputs"):
        ConversationEvent.model_validate(
            {
                **snapshot.events[0].model_dump(mode="json"),
                "data": {"thread_id": "vendor-owned"},
            }
        )


@pytest.mark.asyncio
async def test_store_assigns_contiguous_sequence_and_validates_turn_lifecycle(
    tmp_path: Path,
) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    created = await store.new(runtime="claude-code")
    conversation_id = created.manifest.conversation_id
    snapshot, lease = await store.resume(conversation_id)
    turn_id = uuid4().hex
    try:
        user = await store.append(
            lease,
            ConversationEventKind.USER_MESSAGE,
            turn_id=turn_id,
            text="What changed?",
        )
        first = await store.append(
            lease,
            ConversationEventKind.ASSISTANT_CHUNK,
            turn_id=turn_id,
            text="Two ",
        )
        second = await store.append(
            lease,
            ConversationEventKind.ASSISTANT_CHUNK,
            turn_id=turn_id,
            text="files.",
        )
        terminal = await store.append(
            lease,
            ConversationEventKind.TURN_COMPLETED,
            turn_id=turn_id,
        )
        assert [user.sequence, first.sequence, second.sequence, terminal.sequence] == [1, 2, 3, 4]

        with pytest.raises(ConversationValidationError, match="lifecycle"):
            await store.append(
                lease,
                ConversationEventKind.ASSISTANT_CHUNK,
                turn_id=turn_id,
                text="late output",
            )
    finally:
        lease.release()

    loaded = await store.load(conversation_id)
    assert loaded.manifest.turn_count == 1
    assert loaded.manifest.active_turn_id is None
    assert loaded.manifest.last_event_sequence == 4


@pytest.mark.asyncio
async def test_change_context_persists_without_changing_completed_turns(
    tmp_path: Path,
) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    created = await store.create(runtime="claude-code", model_override="sonnet")
    conversation_id = created.manifest.conversation_id
    turn_id = await _completed_turn(store, conversation_id, "question", ("ans", "wer"))

    _, lease = await store.resume(conversation_id)
    try:
        changed = await store.change_context(
            lease,
            runtime="codex-cli",
            model_override="gpt-5.6-terra",
        )
    finally:
        lease.release()

    loaded = await store.load(conversation_id)
    assert changed.event_type is ConversationEventKind.CONTEXT_CHANGED
    assert changed.turn_id is None
    assert changed.runtime == "codex-cli"
    assert changed.model_override == "gpt-5.6-terra"
    assert loaded.manifest.runtime == "codex-cli"
    assert loaded.manifest.model_override == "gpt-5.6-terra"
    assert loaded.manifest.last_event_sequence == changed.sequence
    assert loaded.events[-1] == changed
    assert [
        (message.role, message.content, message.turn_id)
        for message in await store.completed_turns(conversation_id)
    ] == [
        ("user", "question", turn_id),
        ("assistant", "answer", turn_id),
    ]


@pytest.mark.asyncio
async def test_change_context_rejects_active_turn_without_persisting_event(
    tmp_path: Path,
) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    created = await store.create(runtime="claude-code")
    conversation_id = created.manifest.conversation_id
    _, lease = await store.resume(conversation_id)
    try:
        await store.append(
            lease,
            ConversationEventKind.USER_MESSAGE,
            turn_id=uuid4().hex,
            text="still running",
        )
        before = await store.load(conversation_id)
        with pytest.raises(ConversationValidationError, match="active turn"):
            await store.change_context(
                lease,
                runtime="codex-cli",
                model_override=None,
            )
        after = await store.load(conversation_id)
    finally:
        lease.release()

    assert after == before


@pytest.mark.asyncio
async def test_append_context_checkpoint_persists_between_completed_turns(
    tmp_path: Path,
) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    created = await store.create(runtime="claude-code")
    conversation_id = created.manifest.conversation_id
    turn_id = await _completed_turn(store, conversation_id, "question", ("answer",))

    _, lease = await store.resume(conversation_id)
    try:
        compacted = await store.append_context_checkpoint(
            lease,
            {"checkpoint_id": "c1", "summary": {"text": "kept"}},
        )
    finally:
        lease.release()

    loaded = await store.load(conversation_id)
    assert compacted.event_type is ConversationEventKind.CONTEXT_COMPACTED
    assert compacted.turn_id is None
    assert loaded.events[-1] == compacted
    assert json.loads(compacted.text or "{}") == {
        "checkpoint_id": "c1",
        "summary": {"text": "kept"},
    }
    assert [
        (message.role, message.content, message.turn_id)
        for message in await store.completed_turns(conversation_id)
    ] == [
        ("user", "question", turn_id),
        ("assistant", "answer", turn_id),
    ]


@pytest.mark.asyncio
async def test_append_context_checkpoint_rejects_active_turn_without_persisting_event(
    tmp_path: Path,
) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    created = await store.create(runtime="codex-cli")
    conversation_id = created.manifest.conversation_id
    _, lease = await store.resume(conversation_id)
    try:
        await store.append(
            lease,
            ConversationEventKind.USER_MESSAGE,
            turn_id=uuid4().hex,
            text="still running",
        )
        before = await store.load(conversation_id)
        with pytest.raises(ConversationValidationError, match="compact during a turn"):
            await store.append_context_checkpoint(lease, {"checkpoint_id": "blocked"})
        after = await store.load(conversation_id)
    finally:
        lease.release()

    assert after == before


@pytest.mark.asyncio
async def test_resume_repairs_context_event_first_manifest_crash_window(
    tmp_path: Path,
) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    created = await store.create(runtime="claude-code", model_override="sonnet")
    conversation_id = created.manifest.conversation_id
    crash_event = ConversationEvent(
        conversation_id=conversation_id,
        sequence=created.manifest.last_event_sequence + 1,
        event_type=ConversationEventKind.CONTEXT_CHANGED,
        runtime="codex-cli",
        model_override=None,
    )
    events_path = store.root / conversation_id / "events.jsonl"
    with events_path.open("ab") as file:
        file.write(crash_event.model_dump_json(exclude_none=True).encode() + b"\n")

    with pytest.raises(ConversationValidationError, match="manifest event sequence"):
        await store.load(conversation_id)

    repaired, lease = await store.resume(conversation_id)
    lease.release()
    assert repaired.manifest.runtime == "codex-cli"
    assert repaired.manifest.model_override is None
    assert repaired.manifest.last_event_sequence == crash_event.sequence
    assert repaired.events[-1] == crash_event


@pytest.mark.asyncio
async def test_failed_turn_persists_exact_error_separately_from_reason(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    created = await store.create(runtime="codex-cli")
    snapshot, lease = await store.resume(created.manifest.conversation_id)
    turn_id = uuid4().hex
    try:
        await store.append(
            lease,
            ConversationEventKind.USER_MESSAGE,
            turn_id=turn_id,
            text="Do the work",
        )
        await store.append(
            lease,
            ConversationEventKind.ASSISTANT_CHUNK,
            turn_id=turn_id,
            text="Partial answer",
        )
        failed = await store.append(
            lease,
            ConversationEventKind.TURN_FAILED,
            turn_id=turn_id,
            reason="conversation_turn_failed",
            error="Workspace audit failed: reported paths did not match",
        )
    finally:
        lease.release()

    loaded = await store.load(snapshot.manifest.conversation_id)

    assert failed.error == "Workspace audit failed: reported paths did not match"
    assert loaded.events[-1].reason == "conversation_turn_failed"
    assert loaded.events[-1].error == "Workspace audit failed: reported paths did not match"
    assert await store.completed_turns(snapshot.manifest.conversation_id) == ()
    persisted = (store.root / snapshot.manifest.conversation_id / "events.jsonl").read_text()
    assert '"error":"Workspace audit failed: reported paths did not match"' in persisted

    with pytest.raises(ValidationError, match="only turn.failed"):
        ConversationEvent(
            conversation_id=snapshot.manifest.conversation_id,
            sequence=4,
            event_type=ConversationEventKind.TURN_CANCELLED,
            turn_id=uuid4().hex,
            reason="user_cancelled",
            error="not allowed",
        )


@pytest.mark.asyncio
async def test_writer_lease_rejects_contention_and_stale_fence(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    created = await store.create(runtime="codex-cli")
    conversation_id = created.manifest.conversation_id
    _, first = await store.resume(conversation_id)
    try:
        with pytest.raises(ConversationBusyError):
            store.acquire_writer(conversation_id)
    finally:
        first.release()

    _, stale = await store.resume(conversation_id)
    stale.release()
    _, current = await store.resume(conversation_id)
    try:
        with pytest.raises(ConversationValidationError, match="active.*lease"):
            await store.append(stale, ConversationEventKind.CREATED)
    finally:
        current.release()


@pytest.mark.asyncio
async def test_load_rejects_unsafe_ids_symlinks_and_noncontiguous_logs(
    tmp_path: Path,
) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    created = await store.create(runtime="claude-code")
    conversation_id = created.manifest.conversation_id

    for unsafe in ("../escape", "/absolute", "A" * 32, "short"):
        with pytest.raises(ConversationValidationError):
            await store.load(unsafe)

    events_path = store.root / conversation_id / "events.jsonl"
    event = created.events[0].model_copy(update={"sequence": 2})
    events_path.write_text(event.model_dump_json() + "\n", encoding="utf-8")
    with pytest.raises(ConversationValidationError, match="contiguous"):
        await store.load(conversation_id)

    symlink_root = tmp_path / "linked-root"
    symlink_root.symlink_to(store.root, target_is_directory=True)
    with pytest.raises(ConversationValidationError, match="unsafe"):
        await ConversationStore(symlink_root).list()


@pytest.mark.asyncio
async def test_load_rejects_partial_or_manifest_mismatched_event_log(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    created = await store.create(runtime="codex-cli")
    directory = store.root / created.manifest.conversation_id
    events_path = directory / "events.jsonl"
    events_path.write_bytes(events_path.read_bytes().rstrip(b"\n"))
    with pytest.raises(ConversationValidationError, match="partial final"):
        await store.load(created.manifest.conversation_id)

    events_path.write_text(created.events[0].model_dump_json() + "\n", encoding="utf-8")
    manifest = json.loads((directory / "conversation.json").read_text())
    manifest["last_event_sequence"] = 7
    (directory / "conversation.json").write_text(json.dumps(manifest))
    with pytest.raises(ConversationValidationError, match="manifest event sequence"):
        await store.load(created.manifest.conversation_id)


@pytest.mark.asyncio
async def test_resume_repairs_exactly_one_event_first_manifest_crash_window(
    tmp_path: Path,
) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    created = await store.create(runtime="codex-cli")
    conversation_id = created.manifest.conversation_id
    _, lease = await store.resume(conversation_id)
    turn_id = uuid4().hex
    try:
        await store.append(
            lease,
            ConversationEventKind.USER_MESSAGE,
            turn_id=turn_id,
            text="question persisted before the crash",
        )
        before_crash = await store.load(conversation_id)
        crash_event = ConversationEvent(
            conversation_id=conversation_id,
            sequence=before_crash.manifest.last_event_sequence + 1,
            event_type=ConversationEventKind.ASSISTANT_CHUNK,
            turn_id=turn_id,
            text="partial answer",
        )
        events_path = store.root / conversation_id / "events.jsonl"
        with events_path.open("ab") as file:
            file.write(crash_event.model_dump_json().encode() + b"\n")
    finally:
        lease.release()

    with pytest.raises(ConversationValidationError, match="manifest event sequence"):
        await store.load(conversation_id)

    repaired, resumed_lease = await store.resume("last")
    try:
        assert repaired.manifest.last_event_sequence == crash_event.sequence + 1
        assert repaired.manifest.active_turn_id is None
        assert repaired.events[-2] == crash_event
        assert repaired.events[-1].event_type is ConversationEventKind.TURN_INTERRUPTED
        assert repaired.events[-1].turn_id == turn_id
    finally:
        resumed_lease.release()


@pytest.mark.asyncio
async def test_resume_rejects_more_than_one_event_ahead(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    created = await store.create(runtime="codex-cli")
    conversation_id = created.manifest.conversation_id
    turn_id = uuid4().hex
    events_path = store.root / conversation_id / "events.jsonl"
    for sequence, event_type, text in (
        (1, ConversationEventKind.USER_MESSAGE, "question"),
        (2, ConversationEventKind.ASSISTANT_CHUNK, "answer"),
    ):
        event = ConversationEvent(
            conversation_id=conversation_id,
            sequence=sequence,
            event_type=event_type,
            turn_id=turn_id,
            text=text,
        )
        with events_path.open("ab") as file:
            file.write(event.model_dump_json().encode() + b"\n")

    with pytest.raises(ConversationValidationError, match="manifest event sequence"):
        await store.resume(conversation_id)


@pytest.mark.asyncio
async def test_resume_interrupts_active_turn_once_and_allows_next_user_message(
    tmp_path: Path,
) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    created = await store.create(runtime="claude-code")
    conversation_id = created.manifest.conversation_id
    _, lease = await store.resume(conversation_id)
    interrupted_turn = uuid4().hex
    try:
        await store.append(
            lease,
            ConversationEventKind.USER_MESSAGE,
            turn_id=interrupted_turn,
            text="unfinished question",
        )
        await store.append(
            lease,
            ConversationEventKind.ASSISTANT_CHUNK,
            turn_id=interrupted_turn,
            text="unfinished answer",
        )
    finally:
        lease.release()

    first_resume, first_lease = await store.resume(conversation_id)
    first_lease.release()
    assert first_resume.events[-1].event_type is ConversationEventKind.TURN_INTERRUPTED
    assert first_resume.events[-1].turn_id == interrupted_turn
    assert first_resume.manifest.active_turn_id is None

    second_resume, second_lease = await store.resume(conversation_id)
    try:
        interrupted_events = [
            event
            for event in second_resume.events
            if event.event_type == ConversationEventKind.TURN_INTERRUPTED
        ]
        assert len(interrupted_events) == 1
        next_turn = uuid4().hex
        next_user = await store.append(
            second_lease,
            ConversationEventKind.USER_MESSAGE,
            turn_id=next_turn,
            text="new question",
        )
        assert next_user.turn_id == next_turn
    finally:
        second_lease.release()


@pytest.mark.asyncio
async def test_completed_replay_is_bounded_and_excludes_incomplete_turns(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    created = await store.create(runtime="codex-cli")
    conversation_id = created.manifest.conversation_id
    first_id = await _completed_turn(store, conversation_id, "first", ("one",))
    second_id = await _completed_turn(store, conversation_id, "second", ("t", "wo"))

    _, lease = await store.resume(conversation_id)
    incomplete_id = uuid4().hex
    try:
        await store.append(
            lease,
            ConversationEventKind.USER_MESSAGE,
            turn_id=incomplete_id,
            text="not replayed",
        )
        await store.append(
            lease,
            ConversationEventKind.ASSISTANT_CHUNK,
            turn_id=incomplete_id,
            text="partial",
        )
    finally:
        lease.release()

    replay = await store.completed_turns(conversation_id)
    assert [(item.role, item.content, item.turn_id) for item in replay] == [
        ("user", "first", first_id),
        ("assistant", "one", first_id),
        ("user", "second", second_id),
        ("assistant", "two", second_id),
    ]
    latest_only = await store.completed_turns(
        conversation_id, max_messages=2, max_chars=MAX_MESSAGE_CHARS * 2
    )
    assert [item.content for item in latest_only] == ["second", "two"]


@pytest.mark.asyncio
async def test_list_and_resume_last_use_pca_manifest_recency(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    older = await store.create(runtime="claude-code", title="older")
    newer = await store.create(runtime="codex-cli", title="newer")
    older_path = store.root / older.manifest.conversation_id / "conversation.json"
    older_payload = json.loads(older_path.read_text())
    older_payload["updated_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    older_path.write_text(json.dumps(older_payload))

    listed = await store.list()
    assert [item.title for item in listed] == ["newer", "older"]

    snapshot, lease = await store.resume("last")
    try:
        assert snapshot.manifest.conversation_id == newer.manifest.conversation_id
    finally:
        lease.release()


@pytest.mark.asyncio
async def test_soft_delete_moves_only_exact_conversation_to_private_trash(
    tmp_path: Path,
) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    removed = await store.create(runtime="codex-cli", title="remove")
    retained = await store.create(runtime="claude-code", title="retain")

    destination = await store.delete(removed.manifest.conversation_id)

    assert destination.parent == store.root / ".trash"
    assert destination.is_dir()
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    assert not (store.root / removed.manifest.conversation_id).exists()
    assert (store.root / retained.manifest.conversation_id).is_dir()
    assert [item.conversation_id for item in await store.list()] == [
        retained.manifest.conversation_id
    ]


@pytest.mark.asyncio
async def test_soft_delete_refuses_active_conversation(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    created = await store.create(runtime="codex-cli")
    _, lease = await store.resume(created.manifest.conversation_id)
    try:
        with pytest.raises(ConversationBusyError):
            await store.clear(created.manifest.conversation_id)
        assert (store.root / created.manifest.conversation_id).is_dir()
    finally:
        lease.release()


def test_schema_rejects_unbounded_text_and_open_vendor_fields() -> None:
    base = {
        "conversation_id": uuid4().hex,
        "sequence": 1,
        "event_type": "user.message",
        "turn_id": uuid4().hex,
    }
    with pytest.raises(ValidationError, match="exceeds"):
        ConversationEvent.model_validate({**base, "text": "x" * (MAX_MESSAGE_CHARS + 1)})
    with pytest.raises(ValidationError, match="Extra inputs"):
        ConversationEvent.model_validate(
            {**base, "text": "hello", "vendor_thread_id": "do-not-store"}
        )


@pytest.mark.asyncio
async def test_event_and_manifest_symlinks_are_rejected(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    created = await store.create(runtime="claude-code")
    directory = store.root / created.manifest.conversation_id
    events = directory / "events.jsonl"
    real_events = tmp_path / "events.jsonl"
    os.replace(events, real_events)
    events.symlink_to(real_events)

    with pytest.raises(ConversationValidationError, match="unsafe"):
        await store.load(created.manifest.conversation_id)


@pytest.mark.asyncio
async def test_fork_before_turn_excludes_selected_turn_and_after(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    created = await store.create(runtime="claude-code", title="parent")
    conversation_id = created.manifest.conversation_id
    first = await _completed_turn(store, conversation_id, "first prompt", ("a1",))
    second = await _completed_turn(store, conversation_id, "second prompt", ("a2",))
    await _completed_turn(store, conversation_id, "third prompt", ("a3",))
    parent_events_path = store.root / conversation_id / "events.jsonl"
    parent_manifest_path = store.root / conversation_id / "conversation.json"
    parent_before = (
        parent_events_path.read_bytes(),
        parent_manifest_path.read_bytes(),
    )

    snapshot, lease = await store.fork_before_turn(conversation_id, second, title="branch")
    try:
        branch_id = snapshot.manifest.conversation_id
        assert branch_id != conversation_id
        turn_ids = {event.turn_id for event in snapshot.events if event.turn_id}
        assert turn_ids == {first}
        assert snapshot.events[-1].event_type == ConversationEventKind.TURN_COMPLETED
        assert snapshot.manifest.title == "branch"
        assert snapshot.manifest.runtime == "claude-code"
        assert snapshot.manifest.turn_count == 1

        messages = await store.completed_turns(branch_id)
        assert [(message.role, message.content) for message in messages] == [
            ("user", "first prompt"),
            ("assistant", "a1"),
        ]
    finally:
        lease.release()

    assert (
        parent_events_path.read_bytes(),
        parent_manifest_path.read_bytes(),
    ) == parent_before


@pytest.mark.asyncio
async def test_fork_before_first_prompt_yields_created_only_branch(
    tmp_path: Path,
) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    created = await store.create(runtime="codex-cli")
    conversation_id = created.manifest.conversation_id
    turn = await _completed_turn(store, conversation_id, "only prompt", ("b",))

    snapshot, lease = await store.fork_before_turn(conversation_id, turn)
    try:
        assert len(snapshot.events) == 1
        assert snapshot.events[0].event_type == ConversationEventKind.CREATED
        assert snapshot.manifest.turn_count == 0
        assert snapshot.manifest.active_turn_id is None
        assert snapshot.manifest.runtime == "codex-cli"
    finally:
        lease.release()


@pytest.mark.asyncio
async def test_fork_selects_by_turn_id_for_duplicate_prompts(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    created = await store.create(runtime="claude-code")
    conversation_id = created.manifest.conversation_id
    first = await _completed_turn(store, conversation_id, "same text", ("one",))
    second = await _completed_turn(store, conversation_id, "same text", ("two",))

    snapshot_first, lease_first = await store.fork_before_turn(conversation_id, first)
    try:
        assert [event.sequence for event in snapshot_first.events] == list(
            range(len(snapshot_first.events))
        )
        assert all(
            event.conversation_id == snapshot_first.manifest.conversation_id
            for event in snapshot_first.events
        )
    finally:
        lease_first.release()

    snapshot_second, lease_second = await store.fork_before_turn(conversation_id, second)
    try:
        branch_messages = await store.completed_turns(snapshot_second.manifest.conversation_id)
        # Forking before the *second* duplicate keeps only the first turn.
        assert [message.content for message in branch_messages][-1] == "one"
        assert first != second
    finally:
        lease_second.release()


@pytest.mark.asyncio
async def test_fork_rejects_unknown_turn_and_active_turn(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations", durable=False)
    created = await store.create(runtime="claude-code")
    conversation_id = created.manifest.conversation_id

    with pytest.raises(ConversationValidationError, match="not found"):
        await store.fork_before_turn(conversation_id, "f" * 32)

    snapshot, lease = await store.resume(conversation_id)
    try:
        await store.append(
            lease,
            ConversationEventKind.USER_MESSAGE,
            turn_id=uuid4().hex,
            text="in flight",
        )
        with pytest.raises(ConversationValidationError, match="active turn"):
            await store.fork_before_turn(conversation_id, uuid4().hex)
    finally:
        lease.release()
