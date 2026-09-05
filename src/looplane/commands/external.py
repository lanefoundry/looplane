"""External command services."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from looplane.commands import bootstrap as _bootstrap
from looplane.commands import common as _common
from looplane.commands import paths as _paths
from looplane.contracts import Limits, TaskContract


def run_claude_code_backend(
    prompt: str | None = None,
    instruction: str | None = None,
    repository: Path | None = None,
    check: list[str] | None = None,
    allowed_path: list[str] | None = None,
    run_root: Path = _paths.DEFAULT_RUN_ROOT,
    task_id: str = "claude-code-task",
    timeout_seconds: float = 300.0,
    experimental_subscription: bool = False,
    allow_external_modify: bool = False,
    unsafe_local_exec: bool = False,
) -> None:
    """Let official Claude Code edit a disposable clone, then audit it with looplane."""

    from looplane.external_runner import (
        ExternalModificationApprovalError,
        UnsafeExternalVerificationError,
    )

    instruction = _common._prompt_or_task(prompt, instruction)
    if instruction is None:
        raise typer.BadParameter("PROMPT or --task is required")
    if not experimental_subscription:
        raise typer.BadParameter(
            "Claude Code delegation is local-only and experimental; pass "
            "--experimental-subscription"
        )
    if not check:
        raise typer.BadParameter("external coding requires at least one explicit --check command")
    backend = _bootstrap.build_external_backend("claude-code", timeout_seconds=timeout_seconds)
    try:
        result = asyncio.run(
            _bootstrap.build_external_runner(
                TaskContract(
                    repository=repository or Path.cwd(),
                    instruction=instruction,
                    allowed_paths=tuple(allowed_path or ("**",)),
                    verification=_common._commands(check),
                    limits=Limits(wall_time_seconds=timeout_seconds),
                    task_id=task_id,
                ),
                backend,
                run_root,
                allow_external_modify=allow_external_modify,
                allow_unsafe_local_exec=unsafe_local_exec,
            ).run()
        )
    except (
        ExternalModificationApprovalError,
        UnsafeExternalVerificationError,
        OSError,
        ValueError,
    ) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _common._show_result(result)
    if result.status != "completed":
        raise typer.Exit(code=1)


def run_codex_cli_backend(
    prompt: str | None = None,
    instruction: str | None = None,
    repository: Path | None = None,
    check: list[str] | None = None,
    allowed_path: list[str] | None = None,
    run_root: Path = _paths.DEFAULT_RUN_ROOT,
    task_id: str = "codex-cli-task",
    timeout_seconds: float = 300.0,
    experimental_subscription: bool = False,
    allow_external_modify: bool = False,
    unsafe_local_exec: bool = False,
) -> None:
    """Let official Codex CLI edit a sandboxed clone, then audit it with looplane."""

    from looplane.external_runner import (
        ExternalModificationApprovalError,
        UnsafeExternalVerificationError,
    )

    instruction = _common._prompt_or_task(prompt, instruction)
    if instruction is None:
        raise typer.BadParameter("PROMPT or --task is required")
    if not experimental_subscription:
        raise typer.BadParameter(
            "Codex CLI delegation is local-only and experimental; pass --experimental-subscription"
        )
    if not check:
        raise typer.BadParameter("external coding requires at least one explicit --check command")
    backend = _bootstrap.build_external_backend("codex-cli", timeout_seconds=timeout_seconds)
    try:
        result = asyncio.run(
            _bootstrap.build_external_runner(
                TaskContract(
                    repository=repository or Path.cwd(),
                    instruction=instruction,
                    allowed_paths=tuple(allowed_path or ("**",)),
                    verification=_common._commands(check),
                    limits=Limits(wall_time_seconds=timeout_seconds),
                    task_id=task_id,
                ),
                backend,
                run_root,
                allow_external_modify=allow_external_modify,
                allow_unsafe_local_exec=unsafe_local_exec,
            ).run()
        )
    except (
        ExternalModificationApprovalError,
        UnsafeExternalVerificationError,
        OSError,
        ValueError,
    ) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _common._show_result(result)
    if result.status != "completed":
        raise typer.Exit(code=1)


def _run_external_coding(
    *,
    prompt: str | None,
    instruction: str | None,
    repository: Path | None,
    check: list[str] | None,
    allowed_path: list[str] | None,
    run_root: Path,
    task_id: str,
    timeout_seconds: float,
    allow_external_modify: bool,
    unsafe_local_exec: bool,
    backend: object,
    require_model: bool,
    model: str | None,
) -> None:
    """Shared runner path for the registry-backed external coding CLIs."""

    from looplane.external_runner import (
        ExternalModificationApprovalError,
        UnsafeExternalVerificationError,
    )

    instruction = _common._prompt_or_task(prompt, instruction)
    if instruction is None:
        raise typer.BadParameter("PROMPT or --task is required")
    if require_model and not model:
        raise typer.BadParameter("--model is required for this external runtime")
    if not check:
        raise typer.BadParameter("external coding requires at least one explicit --check command")
    try:
        result = asyncio.run(
            _bootstrap.build_external_runner(
                TaskContract(
                    repository=repository or Path.cwd(),
                    instruction=instruction,
                    allowed_paths=tuple(allowed_path or ("**",)),
                    verification=_common._commands(check),
                    limits=Limits(wall_time_seconds=timeout_seconds),
                    task_id=task_id,
                ),
                backend,  # type: ignore[arg-type]
                run_root,
                allow_external_modify=allow_external_modify,
                allow_unsafe_local_exec=unsafe_local_exec,
            ).run()
        )
    except (
        ExternalModificationApprovalError,
        UnsafeExternalVerificationError,
        OSError,
        ValueError,
    ) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _common._show_result(result)
    if result.status != "completed":
        raise typer.Exit(code=1)


def _run_opencode_backend(
    prompt: str | None = None,
    instruction: str | None = None,
    repository: Path | None = None,
    check: list[str] | None = None,
    allowed_path: list[str] | None = None,
    model: str | None = None,
    run_root: Path = _paths.DEFAULT_RUN_ROOT,
    task_id: str = "opencode-task",
    timeout_seconds: float = 300.0,
    allow_external_modify: bool = False,
    unsafe_local_exec: bool = False,
) -> None:
    """Delegate to the installed OpenCode CLI in an isolated clone, then audit it with looplane."""

    backend = _bootstrap.build_external_backend(
        "opencode", executable="opencode", model=model, timeout_seconds=timeout_seconds
    )
    _run_external_coding(
        require_model=True,
        backend=backend,
        prompt=prompt,
        instruction=instruction,
        repository=repository,
        check=check,
        allowed_path=allowed_path,
        run_root=run_root,
        task_id=task_id,
        timeout_seconds=timeout_seconds,
        allow_external_modify=allow_external_modify,
        unsafe_local_exec=unsafe_local_exec,
        model=model,
    )


def _run_pi_backend(
    prompt: str | None = None,
    instruction: str | None = None,
    repository: Path | None = None,
    check: list[str] | None = None,
    allowed_path: list[str] | None = None,
    model: str | None = None,
    run_root: Path = _paths.DEFAULT_RUN_ROOT,
    task_id: str = "pi-task",
    timeout_seconds: float = 300.0,
    allow_external_modify: bool = False,
    unsafe_local_exec: bool = False,
) -> None:
    """Delegate to the installed Pi coding agent in an isolated clone,
    then audit it with looplane."""

    backend = _bootstrap.build_external_backend(
        "pi", executable="pi", model=model, timeout_seconds=timeout_seconds
    )
    _run_external_coding(
        require_model=True,
        backend=backend,
        prompt=prompt,
        instruction=instruction,
        repository=repository,
        check=check,
        allowed_path=allowed_path,
        run_root=run_root,
        task_id=task_id,
        timeout_seconds=timeout_seconds,
        allow_external_modify=allow_external_modify,
        unsafe_local_exec=unsafe_local_exec,
        model=model,
    )


def _run_omp_backend(
    prompt: str | None = None,
    instruction: str | None = None,
    repository: Path | None = None,
    check: list[str] | None = None,
    allowed_path: list[str] | None = None,
    model: str | None = None,
    run_root: Path = _paths.DEFAULT_RUN_ROOT,
    task_id: str = "omp-task",
    timeout_seconds: float = 300.0,
    allow_external_modify: bool = False,
    unsafe_local_exec: bool = False,
) -> None:
    """Delegate to the installed OMP agent in an isolated clone, then audit it with looplane."""

    backend = _bootstrap.build_external_backend(
        "omp", executable="omp", model=model, timeout_seconds=timeout_seconds
    )
    _run_external_coding(
        require_model=True,
        backend=backend,
        prompt=prompt,
        instruction=instruction,
        repository=repository,
        check=check,
        allowed_path=allowed_path,
        run_root=run_root,
        task_id=task_id,
        timeout_seconds=timeout_seconds,
        allow_external_modify=allow_external_modify,
        unsafe_local_exec=unsafe_local_exec,
        model=model,
    )
