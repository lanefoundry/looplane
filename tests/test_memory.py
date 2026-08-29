from __future__ import annotations

from pathlib import Path

import pytest

from rivumi.memory import (
    load_memory_entries,
    parse_remember_argument,
    relevant_memory_entries,
    remember,
    render_known_context,
)


def test_parse_remember_argument_supports_scopes() -> None:
    assert parse_remember_argument("user: keep replies concise") == (
        "user_preference",
        "user preference",
        "keep replies concise",
    )
    assert parse_remember_argument("preference: run pytest first") == (
        "project_preference",
        "project preference",
        "run pytest first",
    )
    assert parse_remember_argument("this repo uses pnpm") == (
        "project_fact",
        "project fact",
        "this repo uses pnpm",
    )


def test_remember_persists_jsonl_and_filters_by_project(tmp_path: Path) -> None:
    memory_path = tmp_path / "memory.jsonl"
    project = tmp_path / "repo"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()

    remember("user: prefer focused answers", project=project, memory_path=memory_path)
    remember("project: uses pytest", project=project, memory_path=memory_path)
    remember("project: uses cargo", project=other, memory_path=memory_path)

    assert len(load_memory_entries(memory_path)) == 3
    selected = relevant_memory_entries(project=project, memory_path=memory_path)
    assert [entry.description for entry in selected] == [
        "prefer focused answers",
        "uses pytest",
    ]
    rendered = render_known_context(selected)
    assert "[user] prefer focused answers" in rendered
    assert "[project] uses pytest" in rendered
    assert "uses cargo" not in rendered


def test_remember_rejects_blank_description(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        remember("user:   ", memory_path=tmp_path / "memory.jsonl")
