#!/usr/bin/env python3
"""Run a local subagent dispatch smoke and analyze its persisted schedule trace."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from looplane.contracts import Limits, ModelTurn, TaskContract, ToolCall, VerificationCommand
from looplane.loop import AgentRunner
from looplane.models import ScriptedModel
from looplane.subagents import analyze_subagent_schedule_jsonl


def _run_git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", *args),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


async def _run(output_root: Path) -> dict[str, object]:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    work_dir = output_root / stamp
    repository = work_dir / "repo"
    run_root = work_dir / "runs"
    fixture = Path("evals/fixtures/tiny-python-bug").resolve(strict=True)
    shutil.copytree(
        fixture,
        repository,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
    )
    _run_git(repository, "init", "-q")
    _run_git(repository, "config", "user.name", "looplane Smoke")
    _run_git(repository, "config", "user.email", "looplane@example.invalid")
    _run_git(repository, "add", ".")
    _run_git(repository, "commit", "-q", "-m", "fixture: add tiny calculator bug")
    task = TaskContract(
        task_id="a10-subagent-trace-smoke",
        repository=repository,
        base_sha=_run_git(repository, "rev-parse", "HEAD"),
        instruction="Run a subagent schedule trace smoke.",
        allowed_paths=("src/**",),
        verification=(
            VerificationCommand(
                name="tests",
                argv=("python", "-m", "pytest", "-q"),
                timeout_seconds=30,
            ),
        ),
        limits=Limits(max_steps=2, wall_time_seconds=30),
    )
    parent_model = ScriptedModel(
        [
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        name="dispatch_subagents",
                        arguments={
                            "agents": [
                                {
                                    "id": "analysis",
                                    "role": "analyst",
                                    "instruction": "Inspect calculator behavior.",
                                    "allowed_paths": ["src/tiny_python_bug/**"],
                                    "max_steps": 1,
                                },
                                {
                                    "id": "review",
                                    "role": "reviewer",
                                    "instruction": "Review the analyst report.",
                                    "depends_on": ["analysis"],
                                    "allowed_paths": ["src/tiny_python_bug/**"],
                                    "max_steps": 1,
                                },
                            ]
                        },
                    ),
                )
            ),
            ModelTurn(content="Analyst says calculator subtracts."),
            ModelTurn(content="Parent done."),
        ],
        model_id="parent-smoke",
    )
    reviewer_model = ScriptedModel(
        [ModelTurn(content="Reviewer confirms the analyst report.")],
        model_id="reviewer-smoke",
    )
    result = await AgentRunner(
        task,
        parent_model,
        run_root,
        allow_unsafe_local_exec=True,
        subagent_models={"reviewer": reviewer_model},
    ).run()
    events_path = Path(result.artifacts["events"])
    analysis = analyze_subagent_schedule_jsonl(events_path)
    return {
        "status": result.status.value,
        "run_id": result.run_id,
        "run_dir": str(events_path.parent),
        "events": str(events_path),
        "analysis": analysis.as_dict(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(".agent-work/a10-subagent-trace-smoke"),
    )
    args = parser.parse_args()
    payload = asyncio.run(_run(args.output_root))
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
