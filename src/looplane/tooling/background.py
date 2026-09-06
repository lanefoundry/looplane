"""Background process management with ring-buffer output capture."""

from __future__ import annotations

import asyncio
import re
import signal
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from looplane.tooling.types import ToolExecutionError

_MAX_PROCESSES = 3
_MAX_BUFFER_LINES = 2000
_DEFAULT_IDLE_MINUTES = 30


@dataclass
class BackgroundProcess:
    process_id: str
    label: str
    command: str
    pid: int | None = None
    _process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _buffer: deque[str] = field(default_factory=lambda: deque(maxlen=_MAX_BUFFER_LINES))
    _reader_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _stderr_task: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def exit_code(self) -> int | None:
        if self._process is None:
            return None
        return self._process.returncode

    @property
    def buffer_lines(self) -> list[str]:
        return list(self._buffer)


class BackgroundProcessManager:
    def __init__(self, cwd: Path | None = None) -> None:
        self._processes: dict[str, BackgroundProcess] = {}
        self._cwd = cwd

    @property
    def active_count(self) -> int:
        return sum(1 for p in self._processes.values() if p.running)

    async def start_process(
        self,
        command: str,
        label: str,
        *,
        cwd: Path | None = None,
    ) -> str:
        if not command or not command.strip():
            raise ToolExecutionError("command must be non-empty")
        if not label or not label.strip():
            raise ToolExecutionError("label must be non-empty")
        if self.active_count >= _MAX_PROCESSES:
            raise ToolExecutionError(
                f"max {_MAX_PROCESSES} background processes allowed; stop one first"
            )

        process_id = uuid4().hex[:8]
        work_dir = cwd or self._cwd

        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=work_dir,
        )

        bg = BackgroundProcess(
            process_id=process_id,
            label=label,
            command=command,
            pid=proc.pid,
            _process=proc,
        )
        bg._reader_task = asyncio.create_task(self._drain(bg))
        self._processes[process_id] = bg

        return (
            f"Started background process [{process_id}] '{label}'\n"
            f"PID: {proc.pid}\n"
            f"Command: {command}"
        )

    def read_process(
        self,
        process_id: str,
        lines: int = 50,
        pattern: str | None = None,
    ) -> str:
        bg = self._get(process_id)
        all_lines = bg.buffer_lines

        if pattern:
            try:
                regex = re.compile(pattern)
            except re.error as exc:
                raise ToolExecutionError(f"invalid regex pattern: {exc}") from exc
            all_lines = [line for line in all_lines if regex.search(line)]

        tail = all_lines[-lines:]
        status = "running" if bg.running else f"exited ({bg.exit_code})"

        header = f"[{bg.process_id}] {bg.label} ({status}) — {len(tail)}/{len(all_lines)} lines"
        if not tail:
            return f"{header}\n(no output)"
        return f"{header}\n" + "\n".join(tail)

    async def stop_process(self, process_id: str) -> str:
        bg = self._get(process_id)
        if not bg.running:
            return f"[{process_id}] already stopped (exit {bg.exit_code})"

        proc = bg._process
        assert proc is not None
        proc.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            proc.kill()
            await proc.wait()

        return f"[{process_id}] stopped (exit {proc.returncode})"

    def list_processes(self) -> str:
        if not self._processes:
            return "No background processes."
        lines = ["# Background Processes\n"]
        for bg in self._processes.values():
            status = "running" if bg.running else f"exited ({bg.exit_code})"
            buf_count = len(bg.buffer_lines)
            lines.append(
                f"  [{bg.process_id}] {bg.label} — {status} — "
                f"PID {bg.pid} — {buf_count} lines buffered"
            )
        return "\n".join(lines)

    async def wait_for_output(
        self,
        process_id: str,
        pattern: str,
        *,
        timeout_seconds: float = 60.0,
    ) -> str:
        bg = self._get(process_id)
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ToolExecutionError(f"invalid regex pattern: {exc}") from exc

        deadline = asyncio.get_event_loop().time() + min(timeout_seconds, 120.0)
        poll_interval = 0.5

        while asyncio.get_event_loop().time() < deadline:
            for line in bg.buffer_lines:
                if regex.search(line):
                    idx = bg.buffer_lines.index(line)
                    context_start = max(0, idx - 2)
                    context_end = min(len(bg.buffer_lines), idx + 3)
                    context = bg.buffer_lines[context_start:context_end]
                    return f"[{process_id}] Pattern matched: {pattern}\n\n" + "\n".join(context)
            if not bg.running:
                return (
                    f"[{process_id}] Process exited ({bg.exit_code}) "
                    f"before pattern was found: {pattern}"
                )
            await asyncio.sleep(poll_interval)

        return f"[{process_id}] Timed out after {timeout_seconds}s waiting for: {pattern}"

    async def cleanup(self) -> None:
        for bg in list(self._processes.values()):
            if bg.running:
                await self.stop_process(bg.process_id)
        self._processes.clear()

    def _get(self, process_id: str) -> BackgroundProcess:
        bg = self._processes.get(process_id)
        if bg is None:
            raise ToolExecutionError(f"unknown process: {process_id}")
        return bg

    async def _drain(self, bg: BackgroundProcess) -> None:
        proc = bg._process
        if proc is None or proc.stdout is None:
            return
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            bg._buffer.append(line)
        await proc.wait()
