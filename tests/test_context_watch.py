from __future__ import annotations

import asyncio

from rivumi.context_watch import (
    ProjectContextWatchBackend,
    project_context_watch_capabilities,
    project_context_watch_snapshot,
    render_project_context_reload,
    watch_project_context_changes,
)


def test_project_context_watch_snapshot_detects_skill_changes(tmp_path) -> None:
    first = project_context_watch_snapshot(tmp_path)
    skills = tmp_path / ".rivumi" / "skills"
    skills.mkdir(parents=True)
    (skills / "review.md").write_text("---\nname: review\n---\nCheck risks.", encoding="utf-8")

    second = project_context_watch_snapshot(tmp_path)

    assert second.changed_categories(first) == ("skills",)
    rendered = render_project_context_reload(first, second)
    assert "[project-context-reload-v1]" in rendered
    assert "- skills: .rivumi/skills/review.md" in rendered


def test_project_context_watch_snapshot_detects_raw_hook_changes(tmp_path) -> None:
    first = project_context_watch_snapshot(tmp_path)
    hooks = tmp_path / ".rivumi"
    hooks.mkdir()
    (hooks / "hooks.json").write_text('{"pre_tool_use":[]}', encoding="utf-8")

    second = project_context_watch_snapshot(tmp_path)

    assert second.changed_categories(first) == ("hooks",)


def test_project_context_watch_capabilities_are_explicit() -> None:
    capabilities = {
        capability.backend: capability for capability in project_context_watch_capabilities()
    }

    assert capabilities[ProjectContextWatchBackend.PORTABLE_POLLING].available is True
    assert capabilities[ProjectContextWatchBackend.OS_NATIVE].available is False
    assert "not enabled" in capabilities[ProjectContextWatchBackend.OS_NATIVE].reason


async def test_watch_project_context_changes_yields_changed_categories(tmp_path) -> None:
    watcher = watch_project_context_changes(tmp_path, interval_seconds=0.01)
    next_change = asyncio.create_task(anext(watcher))
    try:
        await asyncio.sleep(0.02)
        skills = tmp_path / ".rivumi" / "skills"
        skills.mkdir(parents=True)
        (skills / "review.md").write_text(
            "---\nname: review\n---\nCheck risks.",
            encoding="utf-8",
        )
        change = await asyncio.wait_for(next_change, timeout=1.0)
    finally:
        next_change.cancel()
        await watcher.aclose()

    assert change.changed_categories == ("skills",)
    assert change.current.sources["skills"] == (".rivumi/skills/review.md",)


async def test_watch_project_context_changes_rejects_unavailable_backend(tmp_path) -> None:
    try:
        watcher = watch_project_context_changes(
            tmp_path,
            interval_seconds=0.01,
            backend=ProjectContextWatchBackend.OS_NATIVE,
        )
        await anext(watcher)
    except ValueError as exc:
        assert "portable_polling" in str(exc)
    else:
        raise AssertionError("expected unavailable backend to fail")
