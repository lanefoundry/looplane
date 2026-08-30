"""Real-PTY integration tests for looplane's terminal lifecycle.

Headless Textual tests cannot observe terminal escape sequences, so these
tests run the app inside a real pseudo-terminal and assert on the raw byte
stream: a normal exit must leave the alternate screen and print the bounded
semantic transcript into the primary buffer afterwards.
"""

from __future__ import annotations

import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import textwrap
import time
from pathlib import Path

_DRIVER_TEMPLATE = textwrap.dedent(
    """
    from pathlib import Path

    from looplane.cli_config import CliConfig
    from looplane.conversation import ConversationStore
    from looplane.contracts import RunResult, RunStatus
    from looplane.tui import looplaneApp


    class FakeRunner:
        def __init__(self, approval_policy=None, event_sink=None):
            self.approval_policy = approval_policy
            self.event_sink = event_sink

        def request_cancel(self):
            pass

        async def run(self):
            return RunResult(
                run_id="pty-run",
                task_id="pty-task",
                status=RunStatus.COMPLETED,
                summary="Pty summary line.",
                terminal_reason="verified",
            )


    class FakeModel:
        async def aclose(self):
            return None


    app = looplaneApp(
        repository=Path.cwd(),
        config=CliConfig(runtime="codex-cli"),
        runner_factory=lambda request, policy, sink: (FakeRunner(policy, sink), None),
        providers=(("ollama", "Ollama"),),
        initial_prompt="hello from pty",
        conversation_store=ConversationStore({store!r}, durable=False),
    )
    result = app.run()
    transcript = app.final_transcript_text
    if transcript:
        print(transcript)
    """
)

_LEAVE_ALTERNATE_SCREEN = b"\x1b[?1049l"


def _run_driver_in_pty(driver_path: Path, *, timeout_s: float = 60.0) -> bytes:
    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        process = subprocess.Popen(
            [sys.executable, str(driver_path)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={**os.environ, "TERM": "xterm-256color"},
            close_fds=True,
        )
    finally:
        os.close(slave)
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout_s
    exit_sent = False
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            readable, _, _ = select.select([master], [], [], 0.2)
            if readable:
                try:
                    data = os.read(master, 65_536)
                except OSError:
                    break
                if data:
                    chunks.append(data)
            output_so_far = b"".join(chunks)
            # Once the seeded prompt has completed, confirm-exit via the new
            # idle double Ctrl+C contract through the real terminal.
            if not exit_sent and b"verified" in output_so_far:
                exit_sent = True
                time.sleep(0.3)
                os.write(master, b"\x03")
                time.sleep(0.2)
                os.write(master, b"\x03")
        output = b"".join(chunks)
        # Drain whatever remains after exit.
        while True:
            readable, _, _ = select.select([master], [], [], 0.2)
            if not readable:
                break
            try:
                data = os.read(master, 65_536)
            except OSError:
                break
            if not data:
                break
            output += data
    finally:
        if process.poll() is None:
            process.kill()
        os.close(master)
    assert process.wait(timeout=15) == 0, output.decode(errors="replace")[-2_000:]
    return output


def test_normal_exit_returns_to_primary_buffer_with_transcript(tmp_path: Path) -> None:
    driver = tmp_path / "pty_driver.py"
    driver.write_text(_DRIVER_TEMPLATE.format(store=str(tmp_path / "conversations")))

    output = _run_driver_in_pty(driver)

    # The app entered and then left DEC alternate screen (1049).
    assert b"\x1b[?1049h" in output
    assert _LEAVE_ALTERNATE_SCREEN in output
    primary_tail = output.rsplit(_LEAVE_ALTERNATE_SCREEN, 1)[-1].decode(errors="replace")

    # Finalized semantic history lands in the primary buffer after exit.
    assert "You › hello from pty" in primary_tail
    assert "Assistant › Pty summary line." in primary_tail

    # A copyable resume command for the persisted conversation is included.
    resume_lines = [
        line for line in primary_tail.splitlines() if line.startswith("Resume with: /resume ")
    ]
    assert len(resume_lines) == 1
    conversation_id = resume_lines[0].removeprefix("Resume with: /resume ").strip()
    assert len(conversation_id) == 32
    assert (Path(driver.parent) / "conversations" / conversation_id).is_dir()

    # Transient chrome never leaks into the export.
    assert "composer" not in primary_tail.lower()


def test_transcript_survives_when_conversation_was_never_persisted(
    tmp_path: Path,
) -> None:
    driver = tmp_path / "pty_driver_no_store.py"
    driver.write_text(
        textwrap.dedent(
            """
            from pathlib import Path

            from looplane.cli_config import CliConfig
            from looplane.contracts import RunResult, RunStatus
            from looplane.tui import looplaneApp


            class FakeRunner:
                def __init__(self, approval_policy=None, event_sink=None):
                    pass

                def request_cancel(self):
                    pass

                async def run(self):
                    return RunResult(
                        run_id="pty-run-2",
                        task_id="pty-task-2",
                        status=RunStatus.COMPLETED,
                        summary="No store summary.",
                        terminal_reason="verified",
                    )


            app = looplaneApp(
                repository=Path.cwd(),
                config=CliConfig(provider="ollama", model="qwen3:4b"),
                runner_factory=lambda request, policy, sink: (FakeRunner(), None),
                providers=(("ollama", "Ollama"),),
                initial_prompt="no store prompt",
                conversation_store=None,
            )
            result = app.run()
            transcript = app.final_transcript_text
            if transcript:
                print(transcript)
            """
        )
    )

    output = _run_driver_in_pty(driver)
    primary_tail = output.rsplit(_LEAVE_ALTERNATE_SCREEN, 1)[-1].decode(errors="replace")
    assert "You › no store prompt" in primary_tail
    assert "Assistant › No store summary." in primary_tail
    assert "looplane session" in primary_tail
