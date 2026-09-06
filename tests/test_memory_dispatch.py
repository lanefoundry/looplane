"""Tests for agent memory tools — save_memory and recall_memory."""

from __future__ import annotations

from pathlib import Path

from looplane.agent.memory_dispatch import (
    execute_recall_memory,
    execute_save_memory,
    memory_tool_definitions,
)
from looplane.contracts import ToolCall
from looplane.memory import load_memory_files, save_memory_file


class TestToolDefinitions:
    def test_definitions_have_correct_names(self) -> None:
        defs = memory_tool_definitions()
        names = {d.name for d in defs}
        assert names == {"save_memory", "recall_memory"}

    def test_save_memory_is_not_read_only(self) -> None:
        defs = memory_tool_definitions()
        save = next(d for d in defs if d.name == "save_memory")
        assert not save.read_only

    def test_recall_memory_is_read_only(self) -> None:
        defs = memory_tool_definitions()
        recall = next(d for d in defs if d.name == "recall_memory")
        assert recall.read_only


class TestSaveMemory:
    def test_saves_project_fact(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        call = ToolCall(
            name="save_memory",
            arguments={
                "description": "This repo uses pytest with -x flag",
                "body": "Always run pytest with -x for fail-fast.",
                "type": "project_fact",
            },
        )
        obs = execute_save_memory(call, project=repo)
        assert obs.ok
        assert "saved" in obs.content.lower()
        assert ".md" in obs.content

    def test_saves_user_preference(self, tmp_path: Path, monkeypatch: object) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        mem_dir = tmp_path / "memory"
        import looplane.memory

        monkeypatch.setattr(looplane.memory, "default_memory_dir", lambda: mem_dir)  # type: ignore[attr-defined]
        call = ToolCall(
            name="save_memory",
            arguments={
                "description": "User prefers concise answers",
                "body": "Keep responses short and to the point.",
                "type": "user_preference",
            },
        )
        obs = execute_save_memory(call, project=repo)
        assert obs.ok
        files = load_memory_files(mem_dir)
        assert len(files) == 1
        assert files[0].type == "user_preference"
        assert files[0].project is None

    def test_rejects_empty_description(self, tmp_path: Path) -> None:
        call = ToolCall(
            name="save_memory",
            arguments={"description": "", "body": "some body"},
        )
        obs = execute_save_memory(call, project=tmp_path)
        assert not obs.ok
        assert "description" in obs.error.lower()

    def test_rejects_empty_body(self, tmp_path: Path) -> None:
        call = ToolCall(
            name="save_memory",
            arguments={"description": "some desc", "body": ""},
        )
        obs = execute_save_memory(call, project=tmp_path)
        assert not obs.ok
        assert "body" in obs.error.lower()

    def test_defaults_to_project_fact(self, tmp_path: Path, monkeypatch: object) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        mem_dir = tmp_path / "memory"
        import looplane.memory

        monkeypatch.setattr(looplane.memory, "default_memory_dir", lambda: mem_dir)  # type: ignore[attr-defined]
        call = ToolCall(
            name="save_memory",
            arguments={
                "description": "Uses ruff for linting",
                "body": "Ruff is configured in pyproject.toml.",
            },
        )
        obs = execute_save_memory(call, project=repo)
        assert obs.ok
        files = load_memory_files(mem_dir)
        assert files[0].type == "project_fact"


class TestRecallMemory:
    def test_returns_memories(self, tmp_path: Path, monkeypatch: object) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        mem_dir = tmp_path / "memory"
        import looplane.memory

        monkeypatch.setattr(looplane.memory, "default_memory_dir", lambda: mem_dir)  # type: ignore[attr-defined]
        save_memory_file(
            memory_type="project_fact",
            description="Uses pytest",
            body="Run pytest with -x.",
            slug="fact-pytest",
            project=repo,
            memory_dir=mem_dir,
        )
        save_memory_file(
            memory_type="session_summary",
            description="Fixed auth bug",
            body="Corrected session expiry check.",
            slug="session-auth",
            project=repo,
            memory_dir=mem_dir,
        )
        call = ToolCall(name="recall_memory", arguments={"limit": 10})
        obs = execute_recall_memory(call, project=repo)
        assert obs.ok
        assert "2 memories" in obs.content
        assert "Uses pytest" in obs.content
        assert "Fixed auth bug" in obs.content

    def test_returns_empty_message(self, tmp_path: Path, monkeypatch: object) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        import looplane.memory

        monkeypatch.setattr(looplane.memory, "default_memory_dir", lambda: tmp_path / "empty")  # type: ignore[attr-defined]
        call = ToolCall(name="recall_memory", arguments={})
        obs = execute_recall_memory(call, project=repo)
        assert obs.ok
        assert "no memories" in obs.content.lower()

    def test_respects_limit(self, tmp_path: Path, monkeypatch: object) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        mem_dir = tmp_path / "memory"
        import looplane.memory

        monkeypatch.setattr(looplane.memory, "default_memory_dir", lambda: mem_dir)  # type: ignore[attr-defined]
        for i in range(5):
            save_memory_file(
                memory_type="project_fact",
                description=f"Fact {i}",
                body=f"Detail about fact {i}.",
                slug=f"fact-{i}",
                project=repo,
                memory_dir=mem_dir,
            )
        call = ToolCall(name="recall_memory", arguments={"limit": 2})
        obs = execute_recall_memory(call, project=repo)
        assert obs.ok
        assert "2 memories" in obs.content
