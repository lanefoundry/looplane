"""Execute named, already-authorized checks and retain redacted evidence."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from looplane.contracts import VerificationCommand, VerificationOutcome
from looplane.execution.capture import bounded_text
from looplane.execution.environment import sanitized_subprocess_env
from looplane.execution.local_process import run_local_process
from looplane.execution.types import CommandResult
from looplane.sandbox.policy import CommandSandbox, resolve_command_sandbox
from looplane.secret_scan import redact_secrets, scan_text_for_secrets
from looplane.tooling.filesystem import OutputLimits
from looplane.tooling.git import TaskEnvironment, WorkspaceGit
from looplane.tooling.timeouts import effective_timeout
from looplane.tooling.types import ToolExecutionError


class VerificationProcess(Protocol):
    """Callable execution seam, without check selection or approval semantics."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        max_output_chars: int,
        env: Mapping[str, str],
        sandbox: CommandSandbox | None = None,
    ) -> CommandResult: ...


class VerificationSandbox(Protocol):
    def __call__(
        self,
        *,
        profile: str | None = "verification",
        backend: str | None = "auto",
        cwd: Path,
        task_home: Path,
        extra_read_roots: Sequence[Path] = (),
    ) -> CommandSandbox: ...


@dataclass(frozen=True)
class VerificationSandboxSettings:
    enabled: bool = False
    profile: str | None = "verification"
    backend: str | None = "auto"
    read_roots: tuple[Path, ...] = ()


class AuthorizedChecks:
    """Allowlisted execution only; authorization and scheduling stay with callers."""

    def __init__(
        self,
        *,
        git: WorkspaceGit,
        verification_commands: Sequence[VerificationCommand],
        sandbox: VerificationSandboxSettings | None = None,
        run_command: VerificationProcess = run_local_process,
        environment: TaskEnvironment = sanitized_subprocess_env,
        resolve_sandbox: VerificationSandbox = resolve_command_sandbox,
        clock: Callable[[], float] = time.monotonic,
        bound: Callable[[str, int], str] = bounded_text,
    ) -> None:
        self.git = git
        settings = sandbox if sandbox is not None else VerificationSandboxSettings()
        self.sandbox = VerificationSandboxSettings(
            enabled=settings.enabled,
            profile=settings.profile or "verification",
            backend=settings.backend or "auto",
            read_roots=tuple(Path(root) for root in settings.read_roots),
        )
        self.run_command = run_command
        self.environment = environment
        self.resolve_sandbox = resolve_sandbox
        self.clock = clock
        self.bound = bound
        self.commands: dict[str, VerificationCommand] = {}
        self.outcomes: dict[str, VerificationOutcome] = {}
        for command in verification_commands:
            name = str(command.name)
            argv = tuple(command.argv)
            timeout_seconds = float(command.timeout_seconds)
            if not name or not argv or not all(isinstance(arg, str) and arg for arg in argv):
                raise ValueError("verification commands require a name and exact non-empty argv")
            if timeout_seconds <= 0:
                raise ValueError("verification command timeout_seconds must be positive")
            if name in self.commands:
                raise ValueError(f"duplicate verification command name: {name}")
            self.commands[name] = command

    @property
    def output_limits(self) -> OutputLimits:
        """Use the same mutable record as Git and the Slice 2.2 owners."""

        return self.git.output_limits

    def run_check(
        self, name: str, *, timeout_seconds: float | None = None,
    ) -> VerificationOutcome:
        command = self.commands.get(name)
        if command is None:
            raise ToolExecutionError(f"verification command is not allowlisted: {name!r}")
        started_at = self.clock()
        timeout = effective_timeout(float(command.timeout_seconds), timeout_seconds)
        if tuple(command.argv) == ("git", "diff", "--check"):
            result = self.git.run(
                ("diff", "--check"),
                timeout_seconds=timeout,
                max_output_bytes=self.output_limits.max_output_chars,
            )
        else:
            command_env = self.environment(task_home=self.git.task_home)
            sandbox = (
                self.resolve_sandbox(
                    profile=self.sandbox.profile,
                    backend=self.sandbox.backend,
                    cwd=self.git.workspace,
                    task_home=self.git.task_home,
                    extra_read_roots=self.sandbox.read_roots,
                )
                if self.sandbox.enabled
                else None
            )
            result = self.run_command(
                tuple(command.argv),
                cwd=self.git.workspace,
                timeout_seconds=timeout,
                max_output_chars=self.output_limits.max_output_chars,
                env=command_env,
                sandbox=sandbox,
            )
            if (
                sandbox is not None
                and self.sandbox.backend == "auto"
                and result.returncode == 126
                and (
                    result.stderr.startswith("macOS sandbox-exec is unavailable")
                    or result.stderr.startswith("OS command sandbox is unavailable")
                )
            ):
                result = self.run_command(
                    tuple(command.argv),
                    cwd=self.git.workspace,
                    timeout_seconds=timeout,
                    max_output_chars=self.output_limits.max_output_chars,
                    env=command_env,
                )
        duration = self.clock() - started_at
        status = "passed" if result.ok else "failed"
        sections = [f"check {name!r} {status} (exit {result.returncode})"]
        if result.timed_out:
            sections.append(f"timed out after {command.timeout_seconds} seconds")
        output_findings = (
            *scan_text_for_secrets(result.stdout, path=f"run_check:{name}:stdout"),
            *scan_text_for_secrets(result.stderr, path=f"run_check:{name}:stderr"),
        )
        if result.stdout:
            sections.append(f"stdout:\n{redact_secrets(result.stdout)}")
        if result.stderr:
            sections.append(f"stderr:\n{redact_secrets(result.stderr)}")
        findings = tuple(output_findings)
        if findings:
            sections.append(
                "secret scan: redacted " + ", ".join(finding.label() for finding in findings)
            )
        outcome = VerificationOutcome(
            name=name,
            argv=tuple(command.argv),
            ok=result.ok,
            exit_code=result.returncode,
            duration_seconds=duration,
            output=self.bound("\n".join(sections), self.output_limits.max_output_chars),
        )
        self.outcomes[name] = outcome
        return outcome
