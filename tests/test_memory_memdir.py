"""Tests for the Markdown memdir memory system and session extraction."""

from __future__ import annotations

from pathlib import Path

from looplane.contracts import (
    Message,
    RunStatus,
    ToolCall,
    ToolObservation,
    VerificationOutcome,
)
from looplane.memory import (
    MemoryFile,
    load_memory_files,
    relevant_memory_files,
    render_known_context,
    save_memory_file,
)


class TestSaveAndLoad:
    def test_save_creates_markdown_with_frontmatter(self, tmp_path: Path) -> None:
        mem = save_memory_file(
            memory_type="project_fact",
            description="Uses pytest for testing",
            body="The project runs pytest with -x -q flags.",
            slug="testing-setup",
            project=tmp_path / "repo",
            memory_dir=tmp_path / "memory",
        )
        assert mem.slug == "testing-setup"
        assert mem.type == "project_fact"
        path = tmp_path / "memory" / "testing-setup.md"
        assert path.exists()
        content = path.read_text()
        assert "type: project_fact" in content
        assert "description: Uses pytest for testing" in content
        assert "The project runs pytest with -x -q flags." in content

    def test_load_reads_saved_files(self, tmp_path: Path) -> None:
        mem_dir = tmp_path / "memory"
        save_memory_file(
            memory_type="session_summary",
            description="Worked on auth module",
            body="Fixed login bug in auth.py, added tests.",
            slug="session-abc",
            memory_dir=mem_dir,
        )
        save_memory_file(
            memory_type="user_preference",
            description="Prefers concise answers",
            body="User asked for terse responses.",
            slug="user-concise",
            memory_dir=mem_dir,
        )
        loaded = load_memory_files(mem_dir)
        assert len(loaded) == 2
        slugs = {m.slug for m in loaded}
        assert "session-abc" in slugs
        assert "user-concise" in slugs

    def test_load_skips_malformed_files(self, tmp_path: Path) -> None:
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir(parents=True)
        (mem_dir / "bad.md").write_text("no frontmatter here")
        (mem_dir / "empty.md").write_text("---\ntype: project_fact\n---\n")
        save_memory_file(
            memory_type="project_fact",
            description="Good file",
            body="This one is valid.",
            slug="good",
            memory_dir=mem_dir,
        )
        loaded = load_memory_files(mem_dir)
        assert len(loaded) == 1
        assert loaded[0].slug == "good"

    def test_auto_slugify_from_description(self, tmp_path: Path) -> None:
        mem = save_memory_file(
            memory_type="session_summary",
            description="Debugged the WebSocket reconnect issue",
            body="Found the root cause in connection handler.",
            memory_dir=tmp_path / "memory",
        )
        assert mem.slug
        assert "/" not in mem.slug
        assert " " not in mem.slug

    def test_load_empty_directory(self, tmp_path: Path) -> None:
        assert load_memory_files(tmp_path / "nonexistent") == ()

    def test_load_respects_max_files(self, tmp_path: Path) -> None:
        mem_dir = tmp_path / "memory"
        for i in range(5):
            save_memory_file(
                memory_type="project_fact",
                description=f"Fact {i}",
                body=f"Body {i}",
                slug=f"fact-{i}",
                memory_dir=mem_dir,
            )
        loaded = load_memory_files(mem_dir, max_files=3)
        assert len(loaded) == 3


class TestRelevantMemoryFiles:
    def test_filters_by_project(self, tmp_path: Path) -> None:
        mem_dir = tmp_path / "memory"
        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        repo_a.mkdir()
        repo_b.mkdir()
        save_memory_file(
            memory_type="session_summary",
            description="Work on repo A",
            body="Fixed bugs in repo A.",
            slug="session-a",
            project=repo_a,
            memory_dir=mem_dir,
        )
        save_memory_file(
            memory_type="session_summary",
            description="Work on repo B",
            body="Fixed bugs in repo B.",
            slug="session-b",
            project=repo_b,
            memory_dir=mem_dir,
        )
        save_memory_file(
            memory_type="user_preference",
            description="Global preference",
            body="Likes concise code.",
            slug="user-pref",
            memory_dir=mem_dir,
        )
        relevant = relevant_memory_files(project=repo_a, memory_dir=mem_dir)
        slugs = {m.slug for m in relevant}
        assert "session-a" in slugs
        assert "user-pref" in slugs
        assert "session-b" not in slugs


class TestRenderKnownContext:
    def test_renders_both_entries_and_files(self, tmp_path: Path) -> None:
        from looplane.memory import MemoryEntry, remember

        remember("user: likes tests", memory_path=tmp_path / "m.jsonl")
        entries = (
            MemoryEntry(
                type="user_preference",
                name="user preference",
                description="likes tests",
            ),
        )
        files = (
            MemoryFile(
                slug="session-x",
                type="session_summary",
                description="Worked on auth",
                body="Fixed login flow.",
            ),
        )
        rendered = render_known_context(entries, files)
        assert "[user] likes tests" in rendered
        assert "Worked on auth" in rendered
        assert "Fixed login flow." in rendered

    def test_empty_returns_empty(self) -> None:
        assert render_known_context((), ()) == ""


class TestSessionExtraction:
    def test_extract_session_memory(self, tmp_path: Path) -> None:
        from looplane.agent.memory_extraction import extract_session_memory

        messages = [
            Message(role="user", content="Fix the login bug"),
            Message(
                role="assistant",
                content="I'll look at auth.py",
                tool_calls=(ToolCall(name="read_file", arguments={"path": "src/auth.py"}),),
            ),
            ToolObservation(tool_call_id="tc1", name="read_file", ok=True, content="file content"),
            Message(
                role="assistant",
                content="Found the issue, fixing now",
                tool_calls=(
                    ToolCall(
                        name="apply_patch",
                        arguments={"path": "src/auth.py"},
                    ),
                ),
            ),
            ToolObservation(tool_call_id="tc2", name="apply_patch", ok=True, content="applied"),
            Message(role="assistant", content="Fixed the login bug."),
        ]
        repo = tmp_path / "repo"
        repo.mkdir()
        mem = extract_session_memory(
            run_id="abc123",
            instruction="Fix the login bug in auth.py",
            messages=messages,
            status=RunStatus.COMPLETED,
            summary="Fixed login bug by correcting session check.",
            verification=(
                VerificationOutcome(name="pytest", argv=("pytest",), ok=True, exit_code=0),
            ),
            step_count=5,
            project=repo,
            memory_dir=tmp_path / "memory",
        )
        assert mem is not None
        assert mem.type == "session_summary"
        assert "auth.py" in mem.body
        assert "pytest: passed" in mem.body
        assert "read_file" in mem.body

    def test_extract_captures_errors(self, tmp_path: Path) -> None:
        from looplane.agent.memory_extraction import extract_session_memory

        messages = [
            Message(role="user", content="Run tests"),
            Message(
                role="assistant",
                content="Running tests",
                tool_calls=(ToolCall(name="shell", arguments={"command": "pytest"}),),
            ),
            ToolObservation(
                tool_call_id="tc1",
                name="shell",
                ok=False,
                content="",
                error="FileNotFoundError: conftest.py not found",
            ),
        ]
        repo = tmp_path / "repo"
        repo.mkdir()
        mem = extract_session_memory(
            run_id="def456",
            instruction="Run tests",
            messages=messages,
            status=RunStatus.FAILED,
            summary="Tests failed.",
            verification=(),
            step_count=2,
            project=repo,
            memory_dir=tmp_path / "memory",
        )
        assert mem is not None
        assert "FileNotFoundError" in mem.body
        assert "failed" in mem.body

    def test_persist_compaction_summary(self, tmp_path: Path) -> None:
        from looplane.agent.memory_extraction import persist_compaction_summary

        repo = tmp_path / "repo"
        repo.mkdir()
        mem = persist_compaction_summary(
            run_id="run-789",
            summary_text="The user asked about authentication. "
            "We found a bug in session handling and fixed it by adding a check for expired tokens.",
            project=repo,
            memory_dir=tmp_path / "memory",
        )
        assert mem is not None
        assert mem.type == "session_summary"
        assert "authentication" in mem.body

    def test_persist_compaction_skips_short_summaries(self, tmp_path: Path) -> None:
        from looplane.agent.memory_extraction import persist_compaction_summary

        repo = tmp_path / "repo"
        repo.mkdir()
        mem = persist_compaction_summary(
            run_id="run-short",
            summary_text="ok",
            project=repo,
            memory_dir=tmp_path / "memory",
        )
        assert mem is None
