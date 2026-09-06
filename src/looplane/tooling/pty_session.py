"""Interactive PTY session management for REPL-style tools."""

from __future__ import annotations

import asyncio
import os
import pty
import re
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from uuid import uuid4

from looplane.tooling.types import ToolExecutionError

_MAX_SESSIONS = 2
_MAX_BUFFER_LINES = 1000
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[()][A-Z0-9]")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


@dataclass
class PtySession:
    session_id: str
    label: str
    command: str
    pid: int | None = None
    _master_fd: int | None = field(default=None, repr=False)
    _buffer: deque[str] = field(default_factory=lambda: deque(maxlen=_MAX_BUFFER_LINES))
    _reader_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _exited: bool = False
    _exit_code: int | None = None

    @property
    def running(self) -> bool:
        return not self._exited and self.pid is not None

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    @property
    def buffer_lines(self) -> list[str]:
        return list(self._buffer)


class PtySessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, PtySession] = {}

    @property
    def active_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.running)

    async def start_session(self, command: str, label: str) -> str:
        if not command or not command.strip():
            raise ToolExecutionError("command must be non-empty")
        if not label or not label.strip():
            raise ToolExecutionError("label must be non-empty")
        if self.active_count >= _MAX_SESSIONS:
            raise ToolExecutionError(
                f"max {_MAX_SESSIONS} interactive sessions allowed; end one first"
            )

        session_id = uuid4().hex[:8]
        master_fd, slave_fd = pty.openpty()

        pid = os.fork()
        if pid == 0:
            os.close(master_fd)
            os.setsid()
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)
            os.execvp("/bin/sh", ["/bin/sh", "-c", command])
        else:
            os.close(slave_fd)

        session = PtySession(
            session_id=session_id,
            label=label,
            command=command,
            pid=pid,
            _master_fd=master_fd,
        )
        loop = asyncio.get_event_loop()
        session._reader_task = asyncio.ensure_future(
            loop.run_in_executor(None, self._read_output_sync, session)
        )
        self._sessions[session_id] = session

        return (
            f"Started interactive session [{session_id}] '{label}'\nPID: {pid}\nCommand: {command}"
        )

    async def send_input(
        self,
        session_id: str,
        text: str,
        *,
        wait_ms: int = 2000,
    ) -> str:
        session = self._get(session_id)
        if not session.running:
            raise ToolExecutionError(f"session [{session_id}] is not running")
        if session._master_fd is None:
            raise ToolExecutionError(f"session [{session_id}] has no PTY")

        before_count = len(session._buffer)

        input_bytes = (text + "\n").encode("utf-8")
        os.write(session._master_fd, input_bytes)

        wait_ms = min(max(wait_ms, 100), 10_000)
        await asyncio.sleep(wait_ms / 1000.0)

        new_lines = list(session._buffer)[before_count:]
        if not new_lines:
            return f"[{session_id}] (no new output after {wait_ms}ms)"
        return f"[{session_id}] output:\n" + "\n".join(new_lines)

    def read_session(self, session_id: str, lines: int = 50) -> str:
        session = self._get(session_id)
        tail = session.buffer_lines[-lines:]
        status = "running" if session.running else f"exited ({session.exit_code})"
        header = f"[{session_id}] {session.label} ({status})"
        if not tail:
            return f"{header}\n(no output)"
        return f"{header}\n" + "\n".join(tail)

    async def end_session(self, session_id: str) -> str:
        session = self._get(session_id)
        session._exited = True

        if session.pid is not None:
            with suppress(OSError, ProcessLookupError):
                os.kill(session.pid, 9)
            with suppress(ChildProcessError):
                os.waitpid(session.pid, 0)

        if session._master_fd is not None:
            with suppress(OSError):
                os.close(session._master_fd)
            session._master_fd = None

        if session._reader_task and not session._reader_task.done():
            with suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(session._reader_task, timeout=2.0)

        return f"[{session_id}] session ended"

    async def cleanup(self) -> None:
        for session_id in list(self._sessions):
            await self.end_session(session_id)
        self._sessions.clear()

    def _get(self, session_id: str) -> PtySession:
        session = self._sessions.get(session_id)
        if session is None:
            raise ToolExecutionError(f"unknown session: {session_id}")
        return session

    def _read_output_sync(self, session: PtySession) -> None:
        import select

        master_fd = session._master_fd
        if master_fd is None:
            return

        partial = ""
        try:
            while not session._exited:
                readable, _, _ = select.select([master_fd], [], [], 0.5)
                if not readable:
                    continue
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                text = _strip_ansi(data.decode("utf-8", errors="replace"))
                partial += text
                while "\n" in partial:
                    line, partial = partial.split("\n", 1)
                    session._buffer.append(line)
        except OSError:
            pass

        if partial:
            session._buffer.append(partial)

        if session.pid is not None:
            try:
                _, status = os.waitpid(session.pid, os.WNOHANG)
                session._exit_code = os.waitstatus_to_exitcode(status) if status else None
            except ChildProcessError:
                pass
        session._exited = True
