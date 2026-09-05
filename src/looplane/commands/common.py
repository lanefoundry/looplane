"""Common command services."""

from __future__ import annotations

import shlex

import typer

from looplane.contracts import RunResult, VerificationCommand


def _commands(values: list[str] | None) -> tuple[VerificationCommand, ...]:
    configured = values or ["git diff --check"]
    return tuple(
        VerificationCommand(name=f"check-{index}", argv=tuple(shlex.split(value)))
        for index, value in enumerate(configured, 1)
    )


def _show_result(result: RunResult) -> None:
    status = result.status.value
    typer.echo(f"\n{status}: {result.summary}")
    typer.echo(f"session: {result.run_id}")
    typer.echo(f"patch: {result.artifacts['patch']}")


def _prompt_or_task(prompt: str | None, task: str | None) -> str | None:
    if prompt is not None and task is not None:
        raise typer.BadParameter("use either positional PROMPT or --task, not both")
    return prompt if prompt is not None else task
