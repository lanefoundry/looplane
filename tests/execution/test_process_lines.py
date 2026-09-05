import sys
import time
from pathlib import Path

from looplane.execution.local_process import run_local_process as run_bounded_command


def test_bounded_command_delivers_complete_stdout_lines_before_exit(tmp_path: Path) -> None:
    delivered: list[tuple[str, bool, float]] = []

    result = run_bounded_command(
        (
            sys.executable,
            "-c",
            "import time; print('ready', flush=True); time.sleep(0.4); print('done')",
        ),
        cwd=tmp_path,
        timeout_seconds=2,
        max_output_chars=1_000,
        stdout_line_callback=lambda line, truncated: delivered.append(
            (line, truncated, time.monotonic())
        ),
        max_stdout_line_bytes=64,
    )

    assert result.ok
    assert [(line, truncated) for line, truncated, _ in delivered] == [
        ("ready", False),
        ("done", False),
    ]
    assert delivered[1][2] - delivered[0][2] >= 0.3


def test_bounded_command_bounds_each_callback_line_without_losing_capture(
    tmp_path: Path,
) -> None:
    delivered: list[tuple[str, bool]] = []

    result = run_bounded_command(
        (sys.executable, "-c", "print('x' * 1000)"),
        cwd=tmp_path,
        timeout_seconds=2,
        max_output_chars=2_000,
        stdout_line_callback=lambda line, truncated: delivered.append((line, truncated)),
        max_stdout_line_bytes=32,
    )

    assert result.ok
    assert delivered == [("x" * 32, True)]
    assert result.stdout == "x" * 1000 + "\n"
