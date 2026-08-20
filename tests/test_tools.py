import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coding_agent.contracts import Limits, ToolCall, VerificationCommand
from coding_agent.policy import SafePathPolicy
from coding_agent.tools import ToolExecutor


def make_executor(
    workspace: Path,
    *,
    allowed_paths: tuple[str, ...] = ("src/**",),
    limits: Limits | None = None,
    verification_commands: tuple[VerificationCommand, ...] | None = None,
) -> ToolExecutor:
    return ToolExecutor(
        workspace=workspace,
        policy=SafePathPolicy(workspace, allowed_paths=allowed_paths),
        verification_commands=verification_commands
        or (VerificationCommand(name="tests", argv=("pytest", "-q"), timeout_seconds=30),),
        limits=limits,
    )


def test_run_check_rejects_command_not_in_exact_allowlist(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "must-not-exist"
    executor = make_executor(tiny_bug_repo)
    call = ToolCall(
        name="run_check",
        arguments={"name": "python"},
    )

    observation = executor.execute(call)

    assert observation.ok is False
    assert observation.error is not None
    assert "allow" in observation.error.lower() or "unknown" in observation.error.lower()
    assert not marker.exists(), "a rejected check must never reach process execution"


def test_apply_patch_rejects_changes_outside_allowed_paths(tiny_bug_repo: Path) -> None:
    executor = make_executor(tiny_bug_repo)
    patch = """\
diff --git a/TASK.md b/TASK.md
--- a/TASK.md
+++ b/TASK.md
@@ -1,3 +1,3 @@
 # Fix the calculator

-`add(2, 3)` should return `5`. Fix the implementation without changing the tests.
+Change the task instead of the implementation.
"""

    observation = executor.execute(ToolCall(name="apply_patch", arguments={"patch": patch}))

    assert observation.ok is False
    assert observation.error is not None
    assert "allow" in observation.error.lower() or "path" in observation.error.lower()
    assert "Change the task" not in (tiny_bug_repo / "TASK.md").read_text()


def test_apply_patch_rejects_patch_over_byte_limit(tiny_bug_repo: Path) -> None:
    executor = make_executor(tiny_bug_repo, limits=Limits(max_patch_bytes=80))
    patch = """\
diff --git a/src/tiny_python_bug/calculator.py b/src/tiny_python_bug/calculator.py
--- a/src/tiny_python_bug/calculator.py
+++ b/src/tiny_python_bug/calculator.py
@@ -1,3 +1,3 @@
 def add(left: int, right: int) -> int:
     \"\"\"Return the sum of two integers.\"\"\"
-    return left - right
+    return left + right
"""

    observation = executor.execute(ToolCall(name="apply_patch", arguments={"patch": patch}))

    assert len(patch.encode()) > 80
    assert observation.ok is False
    assert observation.error is not None
    assert "exceed" in observation.error.lower()
    assert "bytes" in observation.error.lower()
    assert "left - right" in (
        tiny_bug_repo / "src" / "tiny_python_bug" / "calculator.py"
    ).read_text()


def test_tool_output_limit_is_a_true_utf8_byte_cap(tiny_bug_repo: Path) -> None:
    target = tiny_bug_repo / "src" / "tiny_python_bug" / "unicode.txt"
    target.write_text("界" * 100)
    executor = make_executor(tiny_bug_repo, limits=Limits(max_tool_output_bytes=64))

    observation = executor.execute(
        ToolCall(name="read_file", arguments={"path": "src/tiny_python_bug/unicode.txt"})
    )

    assert observation.ok is True
    assert len(observation.content.encode("utf-8")) <= 64


def test_verification_process_does_not_receive_model_or_github_secrets(
    tiny_bug_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret-value")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret-value")
    script = (
        "import os; "
        "print(os.getenv('OPENAI_API_KEY', 'missing')); "
        "print(os.getenv('GITHUB_TOKEN', 'missing')); "
        "print(os.getenv('PYTHONDONTWRITEBYTECODE', 'missing'))"
    )
    command = VerificationCommand(
        name="env",
        argv=(sys.executable, "-c", script),
        timeout_seconds=5,
    )
    executor = make_executor(tiny_bug_repo, verification_commands=(command,))

    outcome = executor.run_check("env")

    assert outcome.ok is True
    assert "openai-secret-value" not in outcome.output
    assert "github-secret-value" not in outcome.output
    assert outcome.output.count("missing") == 2
    assert "\n1\n" in outcome.output


@pytest.mark.skipif(os.name != "posix", reason="process-group assertion is POSIX-specific")
def test_timed_out_check_kills_child_process_group(tiny_bug_repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "child-survived"
    child = (
        "import pathlib,time; "
        "time.sleep(0.35); "
        f"pathlib.Path({str(marker)!r}).write_text('survived')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "time.sleep(5)"
    )
    command = VerificationCommand(
        name="timeout",
        argv=(sys.executable, "-c", parent),
        timeout_seconds=0.1,
    )
    executor = make_executor(tiny_bug_repo, verification_commands=(command,))

    outcome = executor.run_check("timeout")
    time.sleep(0.5)

    assert outcome.ok is False
    assert outcome.exit_code == 124
    assert "timed out" in outcome.output.lower()
    assert not marker.exists()


def test_new_and_deleted_files_remain_reviewable(tiny_bug_repo: Path) -> None:
    obsolete = tiny_bug_repo / "src" / "tiny_python_bug" / "obsolete.py"
    obsolete.write_text("OLD = True\n")
    subprocess.run(["git", "add", str(obsolete)], cwd=tiny_bug_repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "fixture: add obsolete module"],
        cwd=tiny_bug_repo,
        check=True,
    )
    executor = make_executor(tiny_bug_repo)
    patch = """\
diff --git a/src/tiny_python_bug/new_module.py b/src/tiny_python_bug/new_module.py
new file mode 100644
--- /dev/null
+++ b/src/tiny_python_bug/new_module.py
@@ -0,0 +1 @@
+NEW = True
diff --git a/src/tiny_python_bug/obsolete.py b/src/tiny_python_bug/obsolete.py
deleted file mode 100644
--- a/src/tiny_python_bug/obsolete.py
+++ /dev/null
@@ -1 +0,0 @@
-OLD = True
"""

    observation = executor.execute(ToolCall(name="apply_patch", arguments={"patch": patch}))
    review = executor.reviewable_patch()

    assert observation.ok is True
    assert review.changed_paths == (
        "src/tiny_python_bug/new_module.py",
        "src/tiny_python_bug/obsolete.py",
    )
    assert "new file mode 100644" in review.content
    assert "deleted file mode 100644" in review.content


def test_cumulative_workspace_patch_cannot_exceed_limit(tiny_bug_repo: Path) -> None:
    first = """\
diff --git a/src/tiny_python_bug/first.py b/src/tiny_python_bug/first.py
new file mode 100644
--- /dev/null
+++ b/src/tiny_python_bug/first.py
@@ -0,0 +1 @@
+FIRST = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
"""
    second = """\
diff --git a/src/tiny_python_bug/second.py b/src/tiny_python_bug/second.py
new file mode 100644
--- /dev/null
+++ b/src/tiny_python_bug/second.py
@@ -0,0 +1 @@
+SECOND = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
"""
    limit = max(len(first.encode()), len(second.encode())) + 40
    executor = make_executor(tiny_bug_repo, limits=Limits(max_patch_bytes=limit))

    first_observation = executor.execute(
        ToolCall(name="apply_patch", arguments={"patch": first})
    )
    second_observation = executor.execute(
        ToolCall(name="apply_patch", arguments={"patch": second})
    )

    assert first_observation.ok is True
    assert second_observation.ok is False
    assert second_observation.error is not None
    assert "final patch exceeds" in second_observation.error.lower()
    assert (tiny_bug_repo / "src" / "tiny_python_bug" / "first.py").is_file()
    assert not (tiny_bug_repo / "src" / "tiny_python_bug" / "second.py").exists()
