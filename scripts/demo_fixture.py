"""Run the tiny bug fixture through the real offline agent loop."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

from rivumi.contracts import (
    Limits,
    ModelTurn,
    TaskContract,
    ToolCall,
    VerificationCommand,
)
from rivumi.loop import AgentRunner
from rivumi.models import ScriptedModel

PATCH = """\
diff --git a/src/tiny_python_bug/calculator.py b/src/tiny_python_bug/calculator.py
--- a/src/tiny_python_bug/calculator.py
+++ b/src/tiny_python_bug/calculator.py
@@ -1,3 +1,3 @@
 def add(left: int, right: int) -> int:
     \"\"\"Return the sum of two integers.\"\"\"
-    return left - right
+    return left + right
"""


def git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", *args),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


async def demo(run_root: Path) -> None:
    project = Path(__file__).resolve().parents[1]
    fixture = project / "evals" / "fixtures" / "tiny-python-bug"
    with tempfile.TemporaryDirectory(prefix="coding-agent-demo-") as temporary:
        source = Path(temporary) / "source"
        shutil.copytree(
            fixture,
            source,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
        )
        git(source, "init", "-q")
        git(source, "config", "user.name", "Fixture Author")
        git(source, "config", "user.email", "fixture@example.invalid")
        git(source, "add", ".")
        git(source, "commit", "-q", "-m", "fixture: add tiny calculator bug")
        base_sha = git(source, "rev-parse", "HEAD")

        task = TaskContract(
            task_id="tiny-python-bug-demo",
            repository=source,
            base_sha=base_sha,
            instruction="Fix add so the existing test passes. Do not change tests.",
            allowed_paths=("src/**",),
            verification=(
                VerificationCommand(name="tests", argv=("pytest", "-q"), timeout_seconds=30),
            ),
            limits=Limits(max_steps=8, wall_time_seconds=60),
        )
        model = ScriptedModel(
            [
                ModelTurn(
                    tool_calls=(
                        ToolCall(
                            name="read_file",
                            arguments={"path": "src/tiny_python_bug/calculator.py"},
                        ),
                    )
                ),
                ModelTurn(tool_calls=(ToolCall(name="apply_patch", arguments={"patch": PATCH}),)),
                ModelTurn(tool_calls=(ToolCall(name="run_check", arguments={"name": "tests"}),)),
                ModelTurn(content="Fixed the calculator and verified the test suite."),
            ]
        )
        try:
            result = await AgentRunner(
                task,
                model,
                run_root,
                allow_unsafe_local_exec=True,
            ).run()
        finally:
            await model.aclose()
        print(result.model_dump_json(indent=2))
        if result.status != "completed":
            raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, default=Path("runs"))
    args = parser.parse_args()
    asyncio.run(demo(args.run_root.resolve()))


if __name__ == "__main__":
    main()
