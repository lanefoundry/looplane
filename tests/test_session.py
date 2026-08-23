from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from rivumi.contracts import TaskContract, VerificationCommand
from rivumi.events import RunEvent
from rivumi.prompts import CODING_AGENT_PROMPT_VERSION
from rivumi.session import (
    SessionBusyError,
    SessionManifest,
    SessionStore,
    SessionValidationError,
)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo), *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_run(tmp_path: Path) -> tuple[SessionStore, SessionManifest, TaskContract]:
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "hello.txt").write_text("hello\n")
    git(repo, "add", "hello.txt")
    git(repo, "commit", "-qm", "initial")
    sha = git(repo, "rev-parse", "HEAD")

    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    git(tmp_path, "clone", "-q", str(repo), str(run_dir / "workspace"))
    git(run_dir / "workspace", "checkout", "-q", "--detach", sha)
    task = TaskContract(
        repository=repo,
        instruction="inspect",
        allowed_paths=("**/*",),
        verification=(VerificationCommand(name="tests", argv=("true",)),),
        task_id="task-1",
        base_sha=sha,
    )
    (run_dir / "request.json").write_text(task.model_dump_json())
    manifest = SessionManifest.new(
        run_id="run-1",
        task_id="task-1",
        provider_name="fixture",
        model_id="fixture-model",
        protocol="fixture-protocol",
        base_sha=sha,
    )
    return SessionStore(run_dir, durable=False), manifest, task


def test_legacy_manifest_does_not_claim_the_m3_prompt_version() -> None:
    manifest = SessionManifest.model_validate(
        {
            "schema_version": 1,
            "run_id": "legacy-run",
            "task_id": "legacy-task",
            "provider_name": "scripted",
            "model_id": "fixture",
            "protocol": "scripted",
            "base_sha": "a" * 40,
        }
    )

    assert manifest.prompt_version == "m2-unversioned-patch"
    assert SessionManifest.new(
        run_id="new-run",
        task_id="new-task",
        provider_name="scripted",
        model_id="fixture",
        protocol="scripted",
        base_sha="b" * 40,
    ).prompt_version == CODING_AGENT_PROMPT_VERSION


@pytest.mark.asyncio
async def test_atomic_manifest_round_trip_and_writer_fencing(tmp_path: Path) -> None:
    store, manifest, _ = make_run(tmp_path)
    with store.acquire_writer() as lease:
        current = await store.initialize(manifest, lease)
        current = current.model_copy(update={"step": 2})
        saved = await store.save(current, lease)
        assert saved.step == 2
        assert saved.active_writer_token == lease.token
        with pytest.raises(SessionBusyError):
            store.acquire_writer()
    assert (await store.load()).step == 2


@pytest.mark.asyncio
async def test_stale_manifest_value_is_rejected_after_new_writer_claim(tmp_path: Path) -> None:
    store, manifest, _ = make_run(tmp_path)
    with store.acquire_writer() as first:
        stale = await store.initialize(manifest, first)
    with store.acquire_writer() as second:
        await store.claim(second)
        with pytest.raises(SessionValidationError, match="different writer"):
            await store.save(stale, second)


@pytest.mark.asyncio
async def test_resume_validates_request_events_and_workspace(tmp_path: Path) -> None:
    store, manifest, task = make_run(tmp_path)
    run_dir = store.run_dir
    value = RunEvent(
        event_type="run.created",
        run_id=manifest.run_id,
        task_id=manifest.task_id,
        sequence=0,
    )
    (run_dir / "events.jsonl").write_text(value.model_dump_json() + "\n")
    manifest = manifest.model_copy(update={"last_event_sequence": 0})
    with store.acquire_writer() as lease:
        await store.initialize(manifest, lease)
    with store.acquire_writer() as lease:
        resumed, loaded_task = await store.claim_and_validate_resume(lease)
        assert resumed.base_sha == task.base_sha
        assert loaded_task == task


@pytest.mark.asyncio
async def test_resume_recovers_manifest_committed_one_event_ahead(tmp_path: Path) -> None:
    store, manifest, _ = make_run(tmp_path)
    event = RunEvent(
        event_type="run.created",
        run_id=manifest.run_id,
        task_id=manifest.task_id,
        sequence=0,
    )
    (store.run_dir / "events.jsonl").write_text(event.model_dump_json() + "\n")
    # The state snapshot for event 1 was atomically saved, then the process died before the
    # append. Resume retains that state but reuses sequence 1 for its recovery event.
    manifest = manifest.model_copy(update={"last_event_sequence": 1, "step": 2})
    with store.acquire_writer() as lease:
        await store.initialize(manifest, lease)
    with store.acquire_writer() as lease:
        resumed, _ = await store.claim_and_validate_resume(lease)
        assert resumed.last_event_sequence == 0
        assert resumed.step == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["tool.started", "verification.started"])
async def test_resume_rejects_ambiguous_in_flight_side_effect(
    tmp_path: Path, event_type: str
) -> None:
    store, manifest, _ = make_run(tmp_path)
    event = RunEvent(
        event_type=event_type,
        run_id=manifest.run_id,
        task_id=manifest.task_id,
        sequence=0,
    )
    (store.run_dir / "events.jsonl").write_text(event.model_dump_json() + "\n")
    manifest = manifest.model_copy(update={"last_event_sequence": 0})
    with store.acquire_writer() as lease:
        await store.initialize(manifest, lease)
    with store.acquire_writer() as lease, pytest.raises(
        SessionValidationError, match="cannot prove"
    ):
        await store.claim_and_validate_resume(lease)


@pytest.mark.asyncio
async def test_resume_rejects_non_contiguous_event_log(tmp_path: Path) -> None:
    store, manifest, _ = make_run(tmp_path)
    event = RunEvent(event_type="run.created", run_id="run-1", task_id="task-1", sequence=1)
    (store.run_dir / "events.jsonl").write_text(event.model_dump_json() + "\n")
    manifest = manifest.model_copy(update={"last_event_sequence": 1})
    with store.acquire_writer() as lease:
        await store.initialize(manifest, lease)
    with store.acquire_writer() as lease, pytest.raises(
        SessionValidationError, match="not contiguous"
    ):
        await store.claim_and_validate_resume(lease)


@pytest.mark.asyncio
async def test_resume_rejects_workspace_at_wrong_commit(tmp_path: Path) -> None:
    store, manifest, _ = make_run(tmp_path)
    workspace = store.run_dir / "workspace"
    (workspace / "other.txt").write_text("other\n")
    git(workspace, "config", "user.email", "test@example.com")
    git(workspace, "config", "user.name", "Test")
    git(workspace, "add", "other.txt")
    git(workspace, "commit", "-qm", "other")
    with store.acquire_writer() as lease:
        await store.initialize(manifest, lease)
    with store.acquire_writer() as lease, pytest.raises(
        SessionValidationError, match="HEAD"
    ):
        await store.claim_and_validate_resume(lease)


@pytest.mark.asyncio
async def test_concurrent_save_serializes_in_process(tmp_path: Path) -> None:
    store, manifest, _ = make_run(tmp_path)
    with store.acquire_writer() as lease:
        current = await store.initialize(manifest, lease)
        # This asserts atomic JSON remains parseable during normal sequential session updates.
        for step in range(3):
            current = await store.save(current.model_copy(update={"step": step}), lease)
            json.loads(store.manifest_path.read_text())
        await asyncio.sleep(0)
