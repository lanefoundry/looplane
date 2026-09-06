"""Integration tests for AsyncToolDispatch — real managers, no mocks."""

from __future__ import annotations

import asyncio
import re
import sys

import pytest

from looplane.agent.async_tool_dispatch import AsyncToolDispatch, is_async_tool
from looplane.contracts import ToolCall
from looplane.tooling.background import BackgroundProcessManager
from looplane.tooling.pty_session import PtySessionManager

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="PTY not available on Windows")


def _call(name: str, **kwargs: object) -> ToolCall:
    return ToolCall(name=name, arguments=dict(kwargs))


def _extract_id(content: str) -> str:
    """Extract bracketed process/session id from dispatch output."""
    match = re.search(r"\[([0-9a-f]{8})\]", content)
    assert match, f"no id found in: {content!r}"
    return match.group(1)


# ── is_async_tool ──────────────────────────────────────────────────────


class TestIsAsyncTool:
    def test_background_tools(self):
        for name in ("start_process", "read_process", "stop_process", "list_processes"):
            assert is_async_tool(name)

    def test_pty_tools(self):
        for name in ("start_session", "send_input", "read_session", "end_session"):
            assert is_async_tool(name)

    def test_lsp_tools(self):
        for name in ("lsp_symbols", "lsp_references", "lsp_definition", "lsp_diagnostics"):
            assert is_async_tool(name)

    def test_non_async_tools(self):
        for name in ("read_file", "shell", "web_fetch", "apply_patch"):
            assert not is_async_tool(name)


# ── Background process e2e ─────────────────────────────────────────────


class TestBackgroundDispatchE2E:
    @pytest.fixture
    def dispatch(self, tmp_path):
        bg = BackgroundProcessManager(cwd=tmp_path)
        return AsyncToolDispatch(background=bg)

    @pytest.mark.asyncio
    async def test_start_read_stop_lifecycle(self, dispatch):
        start_obs = await dispatch.execute(
            _call("start_process", command="echo hello-bg", label="echo-test")
        )
        assert start_obs.ok
        assert "echo-test" in start_obs.content

        process_id = _extract_id(start_obs.content)

        await asyncio.sleep(0.5)

        read_obs = await dispatch.execute(_call("read_process", process_id=process_id))
        assert read_obs.ok
        assert "hello-bg" in read_obs.content

        stop_obs = await dispatch.execute(_call("stop_process", process_id=process_id))
        assert stop_obs.ok
        assert "stopped" in stop_obs.content or "already stopped" in stop_obs.content

    @pytest.mark.asyncio
    async def test_list_processes(self, dispatch):
        start_obs = await dispatch.execute(
            _call("start_process", command="sleep 30", label="sleeper")
        )
        assert start_obs.ok

        list_obs = await dispatch.execute(_call("list_processes"))
        assert list_obs.ok
        assert "sleeper" in list_obs.content

        process_id = _extract_id(start_obs.content)
        await dispatch.execute(_call("stop_process", process_id=process_id))

    @pytest.mark.asyncio
    async def test_wait_for_output(self, dispatch):
        start_obs = await dispatch.execute(
            _call(
                "start_process",
                command="sleep 0.2; echo 'server ready on port 8080'",
                label="wait-test",
            )
        )
        assert start_obs.ok
        process_id = _extract_id(start_obs.content)

        wait_obs = await dispatch.execute(
            _call(
                "wait_for_output",
                process_id=process_id,
                pattern="server ready",
                timeout_seconds=5,
            )
        )
        assert wait_obs.ok
        assert "Pattern matched" in wait_obs.content
        assert "server ready on port 8080" in wait_obs.content

    @pytest.mark.asyncio
    async def test_unknown_process_id(self, dispatch):
        obs = await dispatch.execute(_call("read_process", process_id="nonexistent"))
        assert not obs.ok
        assert "unknown process" in obs.error


# ── PTY session e2e ────────────────────────────────────────────────────


class TestPtyDispatchE2E:
    @pytest.fixture
    def dispatch(self):
        pty_mgr = PtySessionManager()
        return AsyncToolDispatch(pty=pty_mgr)

    @pytest.mark.asyncio
    async def test_start_send_read_end_lifecycle(self, dispatch):
        start_obs = await dispatch.execute(_call("start_session", command="cat", label="cat-test"))
        assert start_obs.ok
        session_id = _extract_id(start_obs.content)

        await asyncio.sleep(0.3)

        send_obs = await dispatch.execute(
            _call("send_input", session_id=session_id, text="hello-pty", wait_ms=500)
        )
        assert send_obs.ok
        assert "hello-pty" in send_obs.content

        read_obs = await dispatch.execute(_call("read_session", session_id=session_id))
        assert read_obs.ok
        assert session_id in read_obs.content

        end_obs = await dispatch.execute(_call("end_session", session_id=session_id))
        assert end_obs.ok
        assert "ended" in end_obs.content

    @pytest.mark.asyncio
    async def test_unknown_session_id(self, dispatch):
        obs = await dispatch.execute(_call("read_session", session_id="nonexistent"))
        assert not obs.ok
        assert "unknown session" in obs.error


# ── Graceful errors when manager is None ───────────────────────────────


class TestNoneManagerErrors:
    @pytest.fixture
    def dispatch(self):
        return AsyncToolDispatch()

    @pytest.mark.asyncio
    async def test_background_not_available(self, dispatch):
        obs = await dispatch.execute(_call("start_process", command="echo hi", label="test"))
        assert not obs.ok
        assert "not available" in obs.error

    @pytest.mark.asyncio
    async def test_read_process_not_available(self, dispatch):
        obs = await dispatch.execute(_call("read_process", process_id="abc"))
        assert not obs.ok
        assert "not available" in obs.error

    @pytest.mark.asyncio
    async def test_pty_not_available(self, dispatch):
        obs = await dispatch.execute(_call("start_session", command="python3", label="py"))
        assert not obs.ok
        assert "not available" in obs.error

    @pytest.mark.asyncio
    async def test_lsp_not_configured(self, dispatch):
        obs = await dispatch.execute(_call("lsp_symbols", query="Foo"))
        assert not obs.ok
        assert "not configured" in obs.error

    @pytest.mark.asyncio
    async def test_all_none_manager_tools_return_error(self, dispatch):
        """Every async tool returns a graceful error when its manager is None."""
        tool_args = {
            "start_process": {"command": "x", "label": "x"},
            "read_process": {"process_id": "x"},
            "stop_process": {"process_id": "x"},
            "list_processes": {},
            "wait_for_output": {"process_id": "x", "pattern": "x"},
            "start_session": {"command": "x", "label": "x"},
            "send_input": {"session_id": "x", "text": "x"},
            "read_session": {"session_id": "x"},
            "end_session": {"session_id": "x"},
            "lsp_symbols": {"query": "x"},
            "lsp_references": {"path": "x", "line": 1, "character": 1},
            "lsp_definition": {"path": "x", "line": 1, "character": 1},
            "lsp_diagnostics": {},
        }
        for name, args in tool_args.items():
            obs = await dispatch.execute(_call(name, **args))
            assert not obs.ok, f"{name} should fail when manager is None"
            assert obs.error, f"{name} should have an error message"


# ── Cleanup ────────────────────────────────────────────────────────────


class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_with_running_processes(self, tmp_path):
        bg = BackgroundProcessManager(cwd=tmp_path)
        pty_mgr = PtySessionManager()
        dispatch = AsyncToolDispatch(background=bg, pty=pty_mgr)

        await dispatch.execute(_call("start_process", command="sleep 60", label="bg-cleanup"))
        await dispatch.execute(_call("start_session", command="cat", label="pty-cleanup"))

        await dispatch.cleanup()
        assert bg.active_count == 0
        assert pty_mgr.active_count == 0

    @pytest.mark.asyncio
    async def test_cleanup_with_none_managers(self):
        dispatch = AsyncToolDispatch()
        await dispatch.cleanup()
