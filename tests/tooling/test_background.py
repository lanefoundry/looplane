"""Tests for tooling.background — background process management."""

from __future__ import annotations

import asyncio

import pytest

from looplane.tooling.background import BackgroundProcessManager
from looplane.tooling.types import ToolExecutionError


@pytest.fixture
def manager(tmp_path):
    return BackgroundProcessManager(cwd=tmp_path)


class TestStartProcess:
    @pytest.mark.asyncio
    async def test_starts_and_returns_id(self, manager):
        result = await manager.start_process("echo hello", "test-echo")
        assert "Started background process" in result
        assert "test-echo" in result
        assert manager.active_count >= 0

    @pytest.mark.asyncio
    async def test_rejects_empty_command(self, manager):
        with pytest.raises(ToolExecutionError, match="non-empty"):
            await manager.start_process("", "test")

    @pytest.mark.asyncio
    async def test_rejects_empty_label(self, manager):
        with pytest.raises(ToolExecutionError, match="non-empty"):
            await manager.start_process("echo hi", "")

    @pytest.mark.asyncio
    async def test_enforces_max_processes(self, manager):
        for i in range(3):
            await manager.start_process("sleep 60", f"proc-{i}")
        with pytest.raises(ToolExecutionError, match="max 3"):
            await manager.start_process("echo overflow", "overflow")
        await manager.cleanup()


class TestReadProcess:
    @pytest.mark.asyncio
    async def test_reads_output(self, manager):
        result = await manager.start_process("echo 'line1'; echo 'line2'; echo 'line3'", "test")
        process_id = result.split("[")[1].split("]")[0]
        await asyncio.sleep(0.5)
        output = manager.read_process(process_id)
        assert "line1" in output or "line2" in output or "line3" in output

    @pytest.mark.asyncio
    async def test_filters_with_pattern(self, manager):
        result = await manager.start_process(
            "echo 'INFO: ok'; echo 'ERROR: bad'; echo 'INFO: done'", "test"
        )
        process_id = result.split("[")[1].split("]")[0]
        await asyncio.sleep(0.5)
        output = manager.read_process(process_id, pattern="ERROR")
        assert "ERROR" in output

    def test_rejects_unknown_process(self, manager):
        with pytest.raises(ToolExecutionError, match="unknown process"):
            manager.read_process("nonexistent")


class TestStopProcess:
    @pytest.mark.asyncio
    async def test_stops_running_process(self, manager):
        result = await manager.start_process("sleep 60", "sleeper")
        process_id = result.split("[")[1].split("]")[0]
        stop_result = await manager.stop_process(process_id)
        assert "stopped" in stop_result


class TestListProcesses:
    def test_empty_list(self, manager):
        result = manager.list_processes()
        assert "No background" in result

    @pytest.mark.asyncio
    async def test_lists_processes(self, manager):
        await manager.start_process("sleep 60", "my-server")
        result = manager.list_processes()
        assert "my-server" in result
        assert "running" in result
        await manager.cleanup()


class TestWaitForOutput:
    @pytest.mark.asyncio
    async def test_waits_and_matches(self, manager):
        result = await manager.start_process("sleep 0.2; echo 'ready on port 3000'", "dev-server")
        process_id = result.split("[")[1].split("]")[0]
        wait_result = await manager.wait_for_output(process_id, "ready on port", timeout_seconds=5)
        assert "Pattern matched" in wait_result
        assert "ready on port 3000" in wait_result

    @pytest.mark.asyncio
    async def test_timeout(self, manager):
        result = await manager.start_process("sleep 10", "slow")
        process_id = result.split("[")[1].split("]")[0]
        wait_result = await manager.wait_for_output(process_id, "never-match", timeout_seconds=1)
        assert "Timed out" in wait_result
        await manager.cleanup()


class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_stops_all(self, manager):
        await manager.start_process("sleep 60", "proc1")
        await manager.start_process("sleep 60", "proc2")
        await manager.cleanup()
        assert manager.active_count == 0
