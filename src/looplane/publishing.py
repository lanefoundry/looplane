"""Host-only, explicitly approved publication of isolated coding results."""

from __future__ import annotations

import os
import re
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from looplane.contracts import VerificationCommand
from looplane.execution.environment import sanitized_subprocess_env
from looplane.execution.local_process import run_local_process
from looplane.sandbox.policy import resolve_command_sandbox


class PublishError(RuntimeError):
    """Publication failed; messages deliberately exclude subprocess output and URLs."""


@dataclass(frozen=True)
class PublishResult:
    head: str
    branch: str
    remote: str
    checks: tuple[str, ...]


def publish_changes(
    *,
    source_repo: Path,
    workspace: Path,
    base_sha: str,
    branch: str,
    verification: Sequence[VerificationCommand],
    remote: str = "origin",
    patch: str | None = None,
    timeout_seconds: float = 600.0,
    sandbox_checks: bool = False,
    sandbox_profile: str | None = None,
    sandbox_backend: str | None = None,
    sandbox_read_roots: Sequence[Path] = (),
) -> PublishResult:
    """Verify an isolated result and push a non-forced branch using host authentication.

    The caller must obtain explicit publication approval before invoking this function.
    Source files, index and refs are never changed. Verification gets a sanitized
    environment; only the final host Git push can use the host's authentication.
    """
    if timeout_seconds <= 0:
        raise PublishError("publication timeout must be positive")
    deadline = time.monotonic() + timeout_seconds
    if not verification:
        raise PublishError("publication requires verification commands")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", base_sha):
        raise PublishError("base SHA must be a full commit identifier")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", remote):
        raise PublishError("invalid remote name")
    source = Path(source_repo).resolve(strict=True)
    work = Path(workspace).resolve(strict=True)
    safe_env = sanitized_subprocess_env()

    def run(
        argv,
        cwd,
        *,
        label,
        env=None,
        stdin=None,
        timeout=60.0,
        allow_truncated=False,
        sandbox=None,
    ):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PublishError("publication timeout exceeded")
        if argv[0] == "git":
            argv = (
                "git",
                "-c",
                "core.hooksPath=" + os.devnull,
                "-c",
                "protocol.ext.allow=never",
                *argv[1:],
            )
        result = run_local_process(
            tuple(argv),
            cwd=cwd,
            timeout_seconds=min(timeout, remaining),
            max_output_chars=20_000,
            env=safe_env if env is None else env,
            stdin=stdin,
            sandbox=sandbox,
        )
        if not result.ok or (
            not allow_truncated and (result.stdout_truncated or result.stderr_truncated)
        ):
            raise PublishError(f"{label} failed")
        return result.stdout.strip()

    run(("git", "check-ref-format", "--branch", branch), source, label="branch validation")
    if branch.startswith("-") or branch == "HEAD":
        raise PublishError("invalid publication branch")
    # Host configuration may contain URL rewriting and credential helpers. Resolve
    # the configured push target here, never disclose it in model-visible output.
    host_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    host_env["GIT_TERMINAL_PROMPT"] = "0"
    host_env["GIT_ALLOW_PROTOCOL"] = "file:http:https:ssh:git"
    urls = run(
        ("git", "remote", "get-url", "--push", "--all", remote),
        source,
        label="remote lookup",
        env=host_env,
    ).splitlines()
    if len(urls) != 1 or not urls[0] or urls[0].startswith("-"):
        raise PublishError("publication requires one unambiguous remote push URL")
    if ":" not in urls[0] and not Path(urls[0]).is_absolute():
        urls[0] = str((source / urls[0]).resolve())
    if patch is None:
        if run(
            ("git", "status", "--porcelain", "--untracked-files=all"),
            work,
            label="workspace status",
        ):
            raise PublishError("workspace must be clean or supply an audited patch")
        head = run(("git", "rev-parse", "HEAD"), work, label="workspace HEAD")
        run(
            ("git", "merge-base", "--is-ancestor", base_sha, head), work, label="workspace ancestry"
        )
        clone_source = work
    else:
        if not patch.strip():
            raise PublishError("publication patch is empty")
        head = base_sha
        clone_source = source
    with tempfile.TemporaryDirectory(prefix="looplane-publish-") as directory:
        root = Path(directory)
        safe_env = sanitized_subprocess_env(task_home=root / "task-home")
        checkout = root / "checkout"
        run(
            (
                "git",
                "clone",
                "--no-hardlinks",
                "--no-checkout",
                "--",
                str(clone_source),
                str(checkout),
            ),
            root,
            label="publication clone",
        )
        run(("git", "checkout", "-B", branch, head), checkout, label="publication checkout")
        run(("git", "config", "core.hooksPath", os.devnull), checkout, label="disable hooks")
        if patch is not None:
            run(
                ("git", "apply", "--index", "--binary", "-"),
                checkout,
                label="audited patch application",
                stdin=patch,
            )
            run(
                (
                    "git",
                    "-c",
                    "user.name=Looplane",
                    "-c",
                    "user.email=looplane@localhost",
                    "commit",
                    "-m",
                    "Apply verified Looplane changes",
                ),
                checkout,
                label="publication commit",
            )
        expected_head = run(("git", "rev-parse", "HEAD"), checkout, label="publication HEAD")
        git_config = checkout / ".git" / "config"
        original_config = git_config.read_bytes()
        check_sandbox = None
        if sandbox_checks:
            try:
                check_sandbox = resolve_command_sandbox(
                    profile=sandbox_profile,
                    backend=sandbox_backend,
                    cwd=checkout,
                    task_home=root / "task-home",
                    extra_read_roots=sandbox_read_roots,
                )
            except ValueError as exc:
                raise PublishError("publication sandbox configuration failed") from exc
        for check in verification:
            run(
                check.argv,
                checkout,
                label=f"verification {check.name!r}",
                timeout=check.timeout_seconds,
                allow_truncated=True,
                sandbox=check_sandbox,
            )
        if run(
            ("git", "status", "--porcelain", "--untracked-files=all"),
            checkout,
            label="post-verification status",
        ):
            raise PublishError("verification modified the publication checkout")
        if run(("git", "rev-parse", "HEAD"), checkout, label="verified HEAD") != expected_head:
            raise PublishError("verification changed the publication commit")
        if git_config.is_symlink() or git_config.read_bytes() != original_config:
            raise PublishError("verification changed Git configuration")
        run(
            ("git", "push", "--", urls[0], f"{expected_head}:refs/heads/{branch}"),
            checkout,
            label="remote push",
            env=host_env,
            timeout=120,
        )
        return PublishResult(
            expected_head, branch, remote, tuple(check.name for check in verification)
        )
