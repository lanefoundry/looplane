import os
import shutil
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
    sandbox_checks: bool = False,
    sandbox_profile: str | None = None,
    sandbox_backend: str | None = None,
    sandbox_read_roots: tuple[Path, ...] = (),
) -> ToolExecutor:
    return ToolExecutor(
        workspace=workspace,
        policy=SafePathPolicy(workspace, allowed_paths=allowed_paths),
        verification_commands=verification_commands
        or (VerificationCommand(name="tests", argv=("pytest", "-q"), timeout_seconds=30),),
        limits=limits,
        sandbox_checks=sandbox_checks,
        sandbox_profile=sandbox_profile,
        sandbox_backend=sandbox_backend,
        sandbox_read_roots=sandbox_read_roots,
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


def test_read_only_tool_definitions_mark_concurrency_safe(tiny_bug_repo: Path) -> None:
    executor = make_executor(tiny_bug_repo)
    definitions = {definition.name: definition for definition in executor.definitions}

    for name in ("list_files", "read_file", "search_text", "git_diff"):
        assert definitions[name].read_only is True
        assert definitions[name].concurrency_safe is True
    assert definitions["tool_program"].read_only is True
    assert definitions["tool_program"].concurrency_safe is False
    assert definitions["tool_transaction"].read_only is False
    assert definitions["tool_transaction"].concurrency_safe is False
    program_ops = definitions["tool_program"].input_schema["properties"]["steps"]["items"][
        "properties"
    ]["op"]["enum"]
    transaction_ops = definitions["tool_transaction"].input_schema["properties"]["steps"]["items"][
        "properties"
    ]["op"]["enum"]
    assert "repeat" in program_ops
    assert "if_contains" in program_ops
    assert "repeat" in transaction_ops
    assert "if_contains" in transaction_ops
    for name in ("replace_text", "apply_patch", "run_check"):
        assert definitions[name].read_only is False
        assert definitions[name].concurrency_safe is False


def test_tool_program_executes_bounded_read_only_steps(tiny_bug_repo: Path) -> None:
    executor = make_executor(tiny_bug_repo, allowed_paths=("src/**",))

    observation = executor.execute(
        ToolCall(
            name="tool_program",
            arguments={
                "steps": [
                    {"op": "list_files", "args": {"path": "src"}},
                    {
                        "op": "read_file",
                        "args": {"path": "src/tiny_python_bug/calculator.py"},
                    },
                    {"op": "search_text", "args": {"query": "return", "path": "src"}},
                ]
            },
        )
    )

    assert observation.ok is True
    assert observation.content.startswith("[tool-program-v1]")
    assert "## step 1: list_files" in observation.content
    assert "src/tiny_python_bug/calculator.py" in observation.content
    assert "## step 3: search_text" in observation.content


def test_tool_program_rejects_modify_steps(tiny_bug_repo: Path) -> None:
    executor = make_executor(tiny_bug_repo, allowed_paths=("src/**",))

    observation = executor.execute(
        ToolCall(
            name="tool_program",
            arguments={
                "steps": [
                    {
                        "op": "replace_text",
                        "args": {
                            "path": "src/tiny_python_bug/calculator.py",
                            "old_text": "left - right",
                            "new_text": "left + right",
                        },
                    }
                ]
            },
        )
    )

    assert observation.ok is False
    assert "unsupported tool program op" in (observation.error or "")


def test_tool_program_supports_bounded_repeat_and_branch(tiny_bug_repo: Path) -> None:
    executor = make_executor(tiny_bug_repo, allowed_paths=("src/**",))

    observation = executor.execute(
        ToolCall(
            name="tool_program",
            arguments={
                "steps": [
                    {
                        "op": "repeat",
                        "count": 2,
                        "steps": [
                            {
                                "op": "search_text",
                                "args": {"query": "return", "path": "src"},
                            }
                        ],
                    },
                    {
                        "op": "if_contains",
                        "contains": "calculator.py",
                        "then_steps": [
                            {
                                "op": "read_file",
                                "args": {"path": "src/tiny_python_bug/calculator.py"},
                            }
                        ],
                        "else_steps": [{"op": "git_diff"}],
                    },
                ]
            },
        )
    )

    assert observation.ok is True
    assert observation.content.count("## step") == 3
    assert "matched: true" in observation.content
    assert "return left - right" in observation.content


def test_tool_program_rejects_expanded_loop_over_step_limit(tiny_bug_repo: Path) -> None:
    executor = make_executor(
        tiny_bug_repo,
        allowed_paths=("src/**",),
        limits={"max_tool_program_steps": 2},
    )

    observation = executor.execute(
        ToolCall(
            name="tool_program",
            arguments={
                "steps": [
                        {
                            "op": "repeat",
                            "count": 2,
                            "steps": [{"op": "git_diff"}, {"op": "git_diff"}],
                        }
                ]
            },
        )
    )

    assert observation.ok is False
    assert "exceeds 2 steps" in (observation.error or "")


def test_tool_transaction_applies_edit_and_check_as_one_unit(tiny_bug_repo: Path) -> None:
    target = tiny_bug_repo / "src" / "tiny_python_bug" / "calculator.py"
    command = VerificationCommand(
        name="ok",
        argv=(sys.executable, "-c", "raise SystemExit(0)"),
        timeout_seconds=5,
    )
    executor = make_executor(tiny_bug_repo, verification_commands=(command,))

    observation = executor.execute(
        ToolCall(
            name="tool_transaction",
            arguments={
                "steps": [
                    {
                        "op": "read_file",
                        "args": {"path": "src/tiny_python_bug/calculator.py"},
                    },
                    {
                        "op": "replace_text",
                        "args": {
                            "path": "src/tiny_python_bug/calculator.py",
                            "old_text": "return left - right",
                            "new_text": "return left + right",
                        },
                    },
                    {"op": "run_check", "args": {"name": "ok"}},
                ]
            },
        )
    )

    assert observation.ok is True
    assert observation.content.startswith("[tool-transaction-v1]")
    assert "## step 3: run_check" in observation.content
    assert target.read_text().endswith("return left + right\n")


def test_tool_transaction_supports_branch_and_rolls_back_taken_edit(
    tiny_bug_repo: Path,
) -> None:
    target = tiny_bug_repo / "src" / "tiny_python_bug" / "calculator.py"
    before = target.read_bytes()
    command = VerificationCommand(
        name="fail",
        argv=(sys.executable, "-c", "raise SystemExit(7)"),
        timeout_seconds=5,
    )
    executor = make_executor(tiny_bug_repo, verification_commands=(command,))

    observation = executor.execute(
        ToolCall(
            name="tool_transaction",
            arguments={
                "steps": [
                    {
                        "op": "read_file",
                        "args": {"path": "src/tiny_python_bug/calculator.py"},
                    },
                    {
                        "op": "if_contains",
                        "contains": "return left - right",
                        "then_steps": [
                            {
                                "op": "replace_text",
                                "args": {
                                    "path": "src/tiny_python_bug/calculator.py",
                                    "old_text": "return left - right",
                                    "new_text": "return left + right",
                                },
                            },
                            {"op": "run_check", "args": {"name": "fail"}},
                        ],
                        "else_steps": [{"op": "git_diff"}],
                    },
                ]
            },
        )
    )

    assert observation.ok is False
    assert "rolled back touched paths" in (observation.error or "")
    assert target.read_bytes() == before
    assert executor.git_diff() == ""


def test_tool_transaction_rolls_back_edit_when_check_fails(tiny_bug_repo: Path) -> None:
    target = tiny_bug_repo / "src" / "tiny_python_bug" / "calculator.py"
    before = target.read_bytes()
    command = VerificationCommand(
        name="fail",
        argv=(sys.executable, "-c", "raise SystemExit(7)"),
        timeout_seconds=5,
    )
    executor = make_executor(tiny_bug_repo, verification_commands=(command,))

    observation = executor.execute(
        ToolCall(
            name="tool_transaction",
            arguments={
                "steps": [
                    {
                        "op": "read_file",
                        "args": {"path": "src/tiny_python_bug/calculator.py"},
                    },
                    {
                        "op": "replace_text",
                        "args": {
                            "path": "src/tiny_python_bug/calculator.py",
                            "old_text": "return left - right",
                            "new_text": "return left + right",
                        },
                    },
                    {"op": "run_check", "args": {"name": "fail"}},
                ]
            },
        )
    )

    assert observation.ok is False
    assert "rolled back touched paths" in (observation.error or "")
    assert target.read_bytes() == before
    assert executor.git_diff() == ""


def test_tool_transaction_rolls_back_new_file_when_check_fails(tiny_bug_repo: Path) -> None:
    target = tiny_bug_repo / "src" / "tiny_python_bug" / "generated.py"
    command = VerificationCommand(
        name="fail",
        argv=(sys.executable, "-c", "raise SystemExit(7)"),
        timeout_seconds=5,
    )
    executor = make_executor(tiny_bug_repo, verification_commands=(command,))
    patch = """\
diff --git a/src/tiny_python_bug/generated.py b/src/tiny_python_bug/generated.py
new file mode 100644
--- /dev/null
+++ b/src/tiny_python_bug/generated.py
@@ -0,0 +1 @@
+VALUE = 1
"""

    observation = executor.execute(
        ToolCall(
            name="tool_transaction",
            arguments={
                "steps": [
                    {"op": "apply_patch", "args": {"patch": patch}},
                    {"op": "run_check", "args": {"name": "fail"}},
                ]
            },
        )
    )

    assert observation.ok is False
    assert "rolled back touched paths" in (observation.error or "")
    assert not target.exists()
    assert executor.git_diff() == ""


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_search_text_uses_rg_and_respects_gitignore(tiny_bug_repo: Path) -> None:
    ignored_dir = tiny_bug_repo / "src" / "ignored"
    ignored_dir.mkdir(parents=True)
    (tiny_bug_repo / ".gitignore").write_text("src/ignored/\n", encoding="utf-8")
    (ignored_dir / "hidden.py").write_text("needle\n", encoding="utf-8")
    visible = tiny_bug_repo / "src" / "tiny_python_bug" / "visible.py"
    visible.write_text("needle\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", str(visible)], cwd=tiny_bug_repo, check=True)
    subprocess.run(["git", "commit", "-m", "search fixture"], cwd=tiny_bug_repo, check=True)
    executor = make_executor(tiny_bug_repo, allowed_paths=("src/**",))

    observation = executor.execute(ToolCall(name="search_text", arguments={"query": "needle"}))

    assert observation.ok is True
    assert "visible.py:1:needle" in observation.content
    assert "hidden.py" not in observation.content


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


def test_sandboxed_verification_fails_closed_when_platform_support_is_missing(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rivumi.runtime as runtime

    marker = tmp_path / "must-not-exist"
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: None)
    command = VerificationCommand(
        name="sandboxed",
        argv=(sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ran')"),
        timeout_seconds=5,
    )
    executor = make_executor(
        tiny_bug_repo,
        verification_commands=(command,),
        sandbox_checks=True,
    )

    outcome = executor.run_check("sandboxed")

    assert outcome.ok is False
    assert outcome.exit_code == 126
    assert "sandbox is unavailable" in outcome.output
    assert not marker.exists()


def test_sandboxed_verification_passes_profile_and_read_roots(
    tiny_bug_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rivumi.tools as tools

    captured: dict[str, object] = {}

    def fake_resolve_command_sandbox(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(tools, "resolve_command_sandbox", fake_resolve_command_sandbox)
    extra_root = tmp_path / "toolchain"
    command = VerificationCommand(
        name="python",
        argv=(sys.executable, "-c", "print('ok')"),
        timeout_seconds=5,
    )
    executor = make_executor(
        tiny_bug_repo,
        verification_commands=(command,),
        sandbox_checks=True,
        sandbox_profile="verification",
        sandbox_backend="landlock",
        sandbox_read_roots=(extra_root,),
    )

    outcome = executor.run_check("python")

    assert outcome.ok is True
    assert captured["profile"] == "verification"
    assert captured["backend"] == "landlock"
    assert captured["cwd"] == tiny_bug_repo.resolve(strict=True)
    assert captured["extra_read_roots"] == (extra_root,)


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
