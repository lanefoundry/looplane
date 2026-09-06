"""Tests for tooling.pty_session — interactive PTY session management."""

from __future__ import annotations

import asyncio
import sys

import pytest

from looplane.tooling.pty_session import PtySessionManager, _strip_ansi
from looplane.tooling.types import ToolExecutionError

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="PTY not available on Windows")


class TestStripAnsi:
    def test_strips_color_codes(self):
        assert _strip_ansi("\x1b[32mhello\x1b[0m") == "hello"

    def test_strips_cursor_codes(self):
        assert _strip_ansi("\x1b[2Jcleared") == "cleared"

    def test_preserves_plain_text(self):
        assert _strip_ansi("just text") == "just text"


@pytest.fixture
def manager():
    return PtySessionManager()


class TestStartSession:
    @pytest.mark.asyncio
    async def test_starts_session(self, manager):
        result = await manager.start_session("cat", "test-cat")
        assert "Started interactive session" in result
        assert "test-cat" in result
        await manager.cleanup()

    @pytest.mark.asyncio
    async def test_rejects_empty_command(self, manager):
        with pytest.raises(ToolExecutionError, match="non-empty"):
            await manager.start_session("", "test")

    @pytest.mark.asyncio
    async def test_enforces_max_sessions(self, manager):
        await manager.start_session("cat", "s1")
        await manager.start_session("cat", "s2")
        with pytest.raises(ToolExecutionError, match="max 2"):
            await manager.start_session("cat", "s3")
        await manager.cleanup()


class TestSendInput:
    @pytest.mark.asyncio
    async def test_sends_and_reads(self, manager):
        result = await manager.start_session("cat", "echo-test")
        session_id = result.split("[")[1].split("]")[0]
        await asyncio.sleep(0.3)
        output = await manager.send_input(session_id, "hello world", wait_ms=500)
        assert "hello world" in output
        await manager.cleanup()


class TestReadSession:
    @pytest.mark.asyncio
    async def test_reads_buffer(self, manager):
        result = await manager.start_session("echo 'line1'; echo 'line2'; cat", "reader")
        session_id = result.split("[")[1].split("]")[0]
        await asyncio.sleep(0.5)
        output = manager.read_session(session_id)
        assert session_id in output
        await manager.cleanup()

    def test_rejects_unknown_session(self, manager):
        with pytest.raises(ToolExecutionError, match="unknown session"):
            manager.read_session("nonexistent")


class TestEndSession:
    @pytest.mark.asyncio
    async def test_ends_session(self, manager):
        result = await manager.start_session("cat", "to-end")
        session_id = result.split("[")[1].split("]")[0]
        end_result = await manager.end_session(session_id)
        assert "ended" in end_result


class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_ends_all(self, manager):
        await manager.start_session("cat", "s1")
        await manager.start_session("cat", "s2")
        await manager.cleanup()
        assert manager.active_count == 0
