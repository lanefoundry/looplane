import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from rivumi.contracts import Limits, ToolCall, VerificationCommand
from rivumi.policy import SafePathPolicy
from rivumi.tools import ToolExecutor


def make_executor(
    workspace: Path,
    *,
    allowed_paths: tuple[str, ...] = ("src/**",),
    limits: Limits | dict[str, int] | None = None,
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


def test_apply_patch_rejects_stale_target_content(tiny_bug_repo: Path) -> None:
    executor = make_executor(tiny_bug_repo)
    target = tiny_bug_repo / "src" / "tiny_python_bug" / "calculator.py"
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
    target.write_text(
        target.read_text().replace("return left - right", "return left * right"),
        encoding="utf-8",
    )

    observation = executor.execute(ToolCall(name="apply_patch", arguments={"patch": patch}))

    assert observation.ok is False
    assert observation.error is not None
    assert "git apply --check failed" in observation.error
    assert target.read_text().endswith("return left * right\n")


def test_replace_text_makes_exact_reviewable_edit_and_preserves_mode(
    tiny_bug_repo: Path,
) -> None:
    target = tiny_bug_repo / "src" / "tiny_python_bug" / "calculator.py"
    target.chmod(0o754)
    executor = make_executor(tiny_bug_repo)
    executor.read_file("src/tiny_python_bug/calculator.py")

    observation = executor.execute(
        ToolCall(
            name="replace_text",
            arguments={
                "path": "src/tiny_python_bug/calculator.py",
                "old_text": "return left - right",
                "new_text": "return left + right",
            },
        )
    )
    review = executor.reviewable_patch()

    assert observation.ok is True
    assert target.read_text().endswith("return left + right\n")
    assert target.stat().st_mode & 0o777 == 0o754
    assert review.changed_paths == ("src/tiny_python_bug/calculator.py",)
    assert "-    return left - right" in review.content
    assert "+    return left + right" in review.content


@pytest.mark.parametrize(
    ("old_text", "observed"),
    [
        ("text that is absent", 0),
        ("int", 4),
    ],
)
def test_replace_text_rejects_missing_or_ambiguous_match_without_writing(
    tiny_bug_repo: Path, old_text: str, observed: int
) -> None:
    target = tiny_bug_repo / "src" / "tiny_python_bug" / "calculator.py"
    before = target.read_bytes()
    executor = make_executor(tiny_bug_repo)
    executor.read_file("src/tiny_python_bug/calculator.py")

    observation = executor.execute(
        ToolCall(
            name="replace_text",
            arguments={
                "path": "src/tiny_python_bug/calculator.py",
                "old_text": old_text,
                "new_text": "replacement",
            },
        )
    )

    assert observation.ok is False
    assert observation.error is not None
    assert f"observed {observed}" in observation.error
    assert target.read_bytes() == before


def test_replace_text_refuses_bulk_replacement(tiny_bug_repo: Path) -> None:
    target = tiny_bug_repo / "src" / "tiny_python_bug" / "repeated.txt"
    target.write_text("old\nold\n")
    executor = make_executor(tiny_bug_repo)
    executor.read_file("src/tiny_python_bug/repeated.txt")

    observation = executor.execute(
        ToolCall(
            name="replace_text",
            arguments={
                "path": "src/tiny_python_bug/repeated.txt",
                "old_text": "old",
                "new_text": "new",
            },
        )
    )

    assert observation.ok is False
    assert observation.error is not None
    assert "observed 2" in observation.error
    assert target.read_text() == "old\nold\n"


def test_replace_text_refuses_untracked_existing_file(tiny_bug_repo: Path) -> None:
    target = tiny_bug_repo / "src" / "tiny_python_bug" / "untracked.py"
    target.write_text("old\n")
    executor = make_executor(tiny_bug_repo)
    executor.read_file("src/tiny_python_bug/untracked.py")

    observation = executor.execute(
        ToolCall(
            name="replace_text",
            arguments={
                "path": "src/tiny_python_bug/untracked.py",
                "old_text": "old",
                "new_text": "new",
            },
        )
    )

    assert observation.ok is False
    assert observation.error is not None and "Git-tracked" in observation.error
    assert target.read_text() == "old\n"
    assert executor.reviewable_patch().content == ""


def test_replace_text_rejects_path_escape_and_binary_file(tiny_bug_repo: Path) -> None:
    executor = make_executor(tiny_bug_repo)
    escaped = executor.execute(
        ToolCall(
            name="replace_text",
            arguments={"path": "../outside", "old_text": "x", "new_text": "y"},
        )
    )
    binary = tiny_bug_repo / "src" / "tiny_python_bug" / "binary.dat"
    binary.write_bytes(b"before\x00after")
    executor.read_file("src/tiny_python_bug/binary.dat")
    binary_result = executor.execute(
        ToolCall(
            name="replace_text",
            arguments={
                "path": "src/tiny_python_bug/binary.dat",
                "old_text": "before",
                "new_text": "changed",
            },
        )
    )
    target = tiny_bug_repo / "src" / "tiny_python_bug" / "calculator.py"
    before = target.read_bytes()
    executor.read_file("src/tiny_python_bug/calculator.py")
    binary_output = executor.execute(
        ToolCall(
            name="replace_text",
            arguments={
                "path": "src/tiny_python_bug/calculator.py",
                "old_text": "left - right",
                "new_text": "left + right\x00",
            },
        )
    )

    assert escaped.ok is False
    assert binary_result.ok is False
    assert binary_output.ok is False
    assert binary.read_bytes() == b"before\x00after"
    assert target.read_bytes() == before


def test_replace_text_rolls_back_when_cumulative_patch_exceeds_limit(
    tiny_bug_repo: Path,
) -> None:
    target = tiny_bug_repo / "src" / "tiny_python_bug" / "calculator.py"
    before = target.read_bytes()
    executor = make_executor(tiny_bug_repo, limits=Limits(max_patch_bytes=120))
    executor.read_file("src/tiny_python_bug/calculator.py")

    observation = executor.execute(
        ToolCall(
            name="replace_text",
            arguments={
                "path": "src/tiny_python_bug/calculator.py",
                "old_text": "return left - right",
                "new_text": "return left + right",
            },
        )
    )

    assert observation.ok is False
    assert observation.error is not None
    assert "refused and rolled back" in observation.error
    assert target.read_bytes() == before
    assert executor.reviewable_patch().content == ""


@pytest.mark.parametrize(
    ("limit_name", "limit_value", "error_fragment"),
    [
        ("max_changed_files", 1, "changed files"),
        ("max_patch_lines", 8, "lines"),
    ],
)
def test_replace_text_enforces_cumulative_structural_limits(
    tiny_bug_repo: Path,
    limit_name: str,
    limit_value: int,
    error_fragment: str,
) -> None:
    first = tiny_bug_repo / "src" / "tiny_python_bug" / "first.py"
    second = tiny_bug_repo / "src" / "tiny_python_bug" / "second.py"
    first.write_text("first = False\n")
    second.write_text("second = False\n")
    subprocess.run(["git", "add", str(first), str(second)], cwd=tiny_bug_repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "fixture: cumulative exact edits"],
        cwd=tiny_bug_repo,
        check=True,
    )
    executor = make_executor(
        tiny_bug_repo,
        limits={limit_name: limit_value, "max_patch_bytes": 10_000},
    )
    executor.read_file("src/tiny_python_bug/first.py")
    executor.read_file("src/tiny_python_bug/second.py")

    first_result = executor.execute(
        ToolCall(
            name="replace_text",
            arguments={
                "path": "src/tiny_python_bug/first.py",
                "old_text": "False",
                "new_text": "True",
            },
        )
    )
    second_result = executor.execute(
        ToolCall(
            name="replace_text",
            arguments={
                "path": "src/tiny_python_bug/second.py",
                "old_text": "False",
                "new_text": "True",
            },
        )
    )

    assert first_result.ok is True
    assert second_result.ok is False
    assert second_result.error is not None and error_fragment in second_result.error
    assert first.read_text() == "first = True\n"
    assert second.read_text() == "second = False\n"
    assert executor.reviewable_patch().changed_paths == (
        "src/tiny_python_bug/first.py",
    )


def test_replace_text_requires_a_current_read(tiny_bug_repo: Path) -> None:
    target = tiny_bug_repo / "src" / "tiny_python_bug" / "calculator.py"
    executor = make_executor(tiny_bug_repo)

    unread = executor.execute(
        ToolCall(
            name="replace_text",
            arguments={
                "path": "src/tiny_python_bug/calculator.py",
                "old_text": "return left - right",
                "new_text": "return left + right",
            },
        )
    )
    executor.read_file("src/tiny_python_bug/calculator.py")
    target.write_text(target.read_text().replace("Return the sum", "Return a sum"))
    stale = executor.execute(
        ToolCall(
            name="replace_text",
            arguments={
                "path": "src/tiny_python_bug/calculator.py",
                "old_text": "return left - right",
                "new_text": "return left + right",
            },
        )
    )

    assert unread.ok is False
    assert unread.error is not None and "read_file" in unread.error
    assert stale.ok is False
    assert stale.error is not None and "changed after read_file" in stale.error


def test_replace_text_rolls_back_trailing_whitespace(tiny_bug_repo: Path) -> None:
    target = tiny_bug_repo / "src" / "tiny_python_bug" / "calculator.py"
    before = target.read_bytes()
    executor = make_executor(tiny_bug_repo)
    executor.read_file("src/tiny_python_bug/calculator.py")

    observation = executor.execute(
        ToolCall(
            name="replace_text",
            arguments={
                "path": "src/tiny_python_bug/calculator.py",
                "old_text": "return left - right",
                "new_text": "return left + right   ",
            },
        )
    )

    assert observation.ok is False
    assert observation.error is not None
    assert "whitespace errors" in observation.error
    assert target.read_bytes() == before


def test_replace_text_reads_large_files_with_a_hard_bound(tiny_bug_repo: Path) -> None:
    target = tiny_bug_repo / "src" / "tiny_python_bug" / "large.txt"
    target.write_bytes(b"a" * 9)
    executor = make_executor(
        tiny_bug_repo,
        limits={"max_read_bytes": 8, "max_patch_bytes": 1_000},
    )
    executor.read_file("src/tiny_python_bug/large.txt")

    observation = executor.execute(
        ToolCall(
            name="replace_text",
            arguments={
                "path": "src/tiny_python_bug/large.txt",
                "old_text": "a",
                "new_text": "b",
            },
        )
    )

    assert observation.ok is False
    assert observation.error is not None and "exceeds 8" in observation.error
    assert target.read_bytes() == b"a" * 9


def test_replace_text_restores_original_after_post_replace_fsync_failure(
    tiny_bug_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tiny_bug_repo / "src" / "tiny_python_bug" / "calculator.py"
    before = target.read_bytes()
    executor = make_executor(tiny_bug_repo)
    executor.read_file("src/tiny_python_bug/calculator.py")
    real_fsync = os.fsync
    calls = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls in {2, 4}:
            raise OSError("injected directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    observation = executor.execute(
        ToolCall(
            name="replace_text",
            arguments={
                "path": "src/tiny_python_bug/calculator.py",
                "old_text": "return left - right",
                "new_text": "return left + right",
            },
        )
    )

    assert observation.ok is False
    assert observation.error is not None and "fsync failure" in observation.error
    assert target.read_bytes() == before
    assert not list(target.parent.glob(".*.rivumi-replace-*"))


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
