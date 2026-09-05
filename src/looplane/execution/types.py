from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MAX_STDIN_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    output_incomplete: bool = False
    stdout_callback_incomplete: bool = False

    @property
    def ok(self) -> bool:
        return (
            not self.timed_out
            and self.returncode == 0
            and not self.output_incomplete
            and not self.stdout_callback_incomplete
        )
