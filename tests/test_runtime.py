import json
import sys
import time
from pathlib import Path

import pytest
from conftest import run_git

from looplane import landlock_run
from looplane.runtime import (
    CommandSandbox,
    LocalGitWorkspace,
    WorkspacePreparationError,
    python_runtime_read_roots,
    resolve_command_sandbox,
    run_bounded_command,
    sandboxed_command_argv,
)


def test_bounded_command_delivers_complete_stdout_lines_before_exit(tmp_path: Path) -> None:
    delivered: list[tuple[str, bool, float]] = []

    result = run_bounded_command(
        (
            sys.executable,
            "-c",
            "import time; print('ready', flush=True); time.sleep(0.4); print('done')",
        ),
        cwd=tmp_path,
        timeout_seconds=2,
        max_output_chars=1_000,
        stdout_line_callback=lambda line, truncated: delivered.append(
            (line, truncated, time.monotonic())
        ),
        max_stdout_line_bytes=64,
    )

    assert result.ok
    assert [(line, truncated) for line, truncated, _ in delivered] == [
        ("ready", False),
        ("done", False),
    ]
    assert delivered[1][2] - delivered[0][2] >= 0.3


def test_bounded_command_fails_closed_when_required_sandbox_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import looplane.runtime as runtime

    marker = tmp_path / "must-not-exist"
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: None)
    monkeypatch.setattr(runtime, "landlock_available", lambda: False)

    result = run_bounded_command(
        (sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ran')"),
        cwd=tmp_path,
        timeout_seconds=2,
        max_output_chars=1_000,
        sandbox=CommandSandbox(mode="workspace-write"),
    )

    assert result.ok is False
    assert result.returncode == 126
    assert "sandbox is unavailable" in result.stderr
    assert not marker.exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Landlock is Linux-specific")
def test_landlock_sandbox_allows_dev_null(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    task_home = tmp_path / "task-home"
    workspace.mkdir()
    task_home.mkdir()
    script = (
        "import os; "
        "open(os.devnull, encoding='utf-8').close(); "
        "open(os.devnull, 'w', encoding='utf-8').close()"
    )

    result = run_bounded_command(
        (sys.executable, "-c", script),
        cwd=workspace,
        timeout_seconds=5,
        max_output_chars=1_000,
        sandbox=resolve_command_sandbox(
            profile="verification",
            backend="landlock",
            cwd=workspace,
            task_home=task_home,
        ),
    )

    assert result.ok, result.stderr


def test_resolve_command_sandbox_adds_named_profile_and_read_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    task_home = tmp_path / ".task-home"
    extra = tmp_path / "toolchain"

    sandbox = resolve_command_sandbox(
        profile=None,
        cwd=workspace,
        task_home=task_home,
        extra_read_roots=(extra, extra),
    )

    assert sandbox.mode == "workspace-write"
    assert sandbox.profile == "verification"
    assert sandbox.backend == "auto"
    assert sandbox.read_roots == (
        workspace.resolve(strict=False),
        task_home.resolve(strict=False),
        extra.resolve(strict=False),
        *python_runtime_read_roots(),
    )
    assert sandbox.writable_roots == (task_home.resolve(strict=False),)


def test_resolve_command_sandbox_accepts_landlock_backend(tmp_path: Path) -> None:
    sandbox = resolve_command_sandbox(
        profile="verification",
        backend="landlock",
        cwd=tmp_path / "repo",
        task_home=tmp_path / ".task-home",
    )

    assert sandbox.backend == "landlock"


def test_python_runtime_read_roots_include_prefixes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "prefix", "/opt/looplane/.venv")
    monkeypatch.setattr(sys, "base_prefix", "/opt/python")

    assert python_runtime_read_roots() == (
        Path("/opt/looplane/.venv"),
        Path("/opt/python"),
    )


def test_python_runtime_read_roots_reject_broad_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "prefix", str(Path.home().parent))
    monkeypatch.setattr(sys, "base_prefix", str(Path.home()))

    assert python_runtime_read_roots() == ()


def test_sandboxed_command_rejects_unknown_profile_before_execution(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported command sandbox profile"):
        sandboxed_command_argv(
            (sys.executable, "-c", "print('must not run')"),
            cwd=tmp_path,
            sandbox=CommandSandbox(mode="workspace-write", profile="networked"),
        )


def test_sandboxed_command_wraps_linux_with_bubblewrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import looplane.runtime as runtime

    workspace = tmp_path / "workspace"
    task_home = tmp_path / ".task-home"
    extra = tmp_path / "toolchain"
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.shutil, "which", lambda name: f"/usr/bin/{name}")

    argv = sandboxed_command_argv(
        ("pytest", "-q"),
        cwd=workspace,
        sandbox=CommandSandbox(
            mode="workspace-write",
            read_roots=(workspace, task_home, extra),
            writable_roots=(task_home,),
        ),
    )

    assert isinstance(argv, tuple)
    assert argv[:4] == ("/usr/bin/bwrap", "--die-with-parent", "--unshare-all", "--new-session")
    assert "--bind" in argv
    assert str(workspace.resolve(strict=False)) in argv
    assert "--ro-bind" in argv
    assert str(extra.resolve(strict=False)) in argv
    assert argv[-5:] == ("--chdir", str(workspace), "--", "pytest", "-q")


def test_sandboxed_command_wraps_linux_with_landlock_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not landlock_run.landlock_available():
        pytest.skip("Landlock not supported by the running kernel")

    import looplane.runtime as runtime

    workspace = tmp_path / "workspace"
    task_home = tmp_path / ".task-home"
    monkeypatch.setattr(runtime.sys, "platform", "linux")

    argv = sandboxed_command_argv(
        ("pytest", "-q"),
        cwd=workspace,
        sandbox=CommandSandbox(
            mode="workspace-write",
            backend="landlock",
            read_roots=(workspace, task_home),
            writable_roots=(task_home,),
        ),
    )

    assert isinstance(argv, tuple)
    assert argv[0] == sys.executable
    assert argv[1].endswith("landlock_run.py")
    assert argv[2] == "--policy-json"
    policy = json.loads(argv[3])
    assert policy == {
        "cwd": str(workspace),
        "read_roots": [str(workspace), str(task_home)],
        "writable_roots": [str(task_home)],
    }
    assert argv[-3:] == ("--", "pytest", "-q")


def test_sandboxed_command_can_require_bubblewrap_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import looplane.runtime as runtime

    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: None)

    error = sandboxed_command_argv(
        ("pytest", "-q"),
        cwd=tmp_path,
        sandbox=CommandSandbox(mode="workspace-write", backend="bubblewrap"),
    )

    assert error == "Linux bubblewrap sandbox is unavailable"


def test_sandboxed_command_rejects_unknown_backend(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported command sandbox backend"):
        sandboxed_command_argv(
            ("pytest", "-q"),
            cwd=tmp_path,
            sandbox=CommandSandbox(mode="workspace-write", backend="ptrace"),
        )


def test_landlock_backend_seccomp_profiles_cover_high_risk_syscalls() -> None:
    x86_arch, x86_denied = landlock_run._seccomp_profile_for_machine("x86_64")
    arm_arch, arm_denied = landlock_run._seccomp_profile_for_machine("aarch64")

    assert x86_arch == landlock_run._AUDIT_ARCH_X86_64
    assert arm_arch == landlock_run._AUDIT_ARCH_AARCH64
    assert {101, 165, 321, 435}.issubset(x86_denied)
    assert {40, 117, 280, 435}.issubset(arm_denied)


def test_landlock_backend_seccomp_rejects_unknown_architecture() -> None:
    with pytest.raises(ValueError, match="unsupported seccomp architecture"):
        landlock_run._seccomp_profile_for_machine("mips")


def test_landlock_backend_seccomp_filter_denies_configured_syscalls() -> None:
    filters = landlock_run._seccomp_filter(
        landlock_run._AUDIT_ARCH_X86_64,
        (165,),
    )

    assert filters[0].k == landlock_run._SECCOMP_ARCH_OFFSET
    assert filters[1].k == landlock_run._AUDIT_ARCH_X86_64
    assert filters[4].k == 165
    assert filters[5].k == landlock_run._SECCOMP_RET_ERRNO | 1
    assert filters[-1].k == landlock_run._SECCOMP_RET_ALLOW


def test_bounded_command_bounds_each_callback_line_without_losing_capture(
    tmp_path: Path,
) -> None:
    delivered: list[tuple[str, bool]] = []

    result = run_bounded_command(
        (sys.executable, "-c", "print('x' * 1000)"),
        cwd=tmp_path,
        timeout_seconds=2,
        max_output_chars=2_000,
        stdout_line_callback=lambda line, truncated: delivered.append((line, truncated)),
        max_stdout_line_bytes=32,
    )

    assert result.ok
    assert delivered == [("x" * 32, True)]
    assert result.stdout == "x" * 1000 + "\n"


def test_disposable_workspace_keeps_source_unchanged_and_produces_patch(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    source_sha = run_git(tiny_bug_repo, "rev-parse", "HEAD")
    source_status = run_git(tiny_bug_repo, "status", "--porcelain=v1")
    source_file = tiny_bug_repo / "src" / "tiny_python_bug" / "calculator.py"
    source_contents = source_file.read_bytes()
    runtime = LocalGitWorkspace(tiny_bug_repo, tmp_path / "run", source_sha)

    workspace = runtime.prepare()
    workspace_file = workspace / "src" / "tiny_python_bug" / "calculator.py"
    workspace_file.write_text(source_file.read_text().replace("left - right", "left + right"))
    patch = run_git(workspace, "diff", "--binary", "--no-ext-diff")

    assert workspace == runtime.workspace_path
    assert workspace.resolve() != tiny_bug_repo.resolve()
    assert "-    return left - right" in patch
    assert "+    return left + right" in patch
    assert run_git(tiny_bug_repo, "rev-parse", "HEAD") == source_sha
    assert run_git(tiny_bug_repo, "status", "--porcelain=v1") == source_status == ""
    assert source_file.read_bytes() == source_contents


def test_disposable_workspace_is_pinned_to_commit_not_dirty_source(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    source_sha = run_git(tiny_bug_repo, "rev-parse", "HEAD")
    source_file = tiny_bug_repo / "src" / "tiny_python_bug" / "calculator.py"
    committed_contents = source_file.read_text()
    dirty_contents = committed_contents.replace("left - right", "left * right")
    source_file.write_text(dirty_contents)

    workspace = LocalGitWorkspace(tiny_bug_repo, tmp_path / "run", source_sha).prepare()

    assert (workspace / "src" / "tiny_python_bug" / "calculator.py").read_text() == (
        committed_contents
    )
    assert source_file.read_text() == dirty_contents
    assert run_git(workspace, "rev-parse", "HEAD") == source_sha


def test_workspace_preparation_respects_explicit_timeout(
    tiny_bug_repo: Path, tmp_path: Path
) -> None:
    source_sha = run_git(tiny_bug_repo, "rev-parse", "HEAD")
    runtime = LocalGitWorkspace(tiny_bug_repo, tmp_path / "run", source_sha)

    with pytest.raises(WorkspacePreparationError, match="preparation exceeded"):
        runtime.prepare(timeout_seconds=1e-12)



def test_sandboxed_command_argv_reports_unavailable_when_kernel_lacks_landlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the running kernel has Landlock disabled in CONFIG_LSM, auto
    backend must surface an ``unavailable`` string instead of wrapping the
    command anyway — the wrapper would otherwise apply no-new-privs and a
    filesystem rule set that excludes the venv, causing the child to crash
    with PermissionError on its own pyvenv.cfg."""
    import looplane.runtime as runtime

    sandbox = CommandSandbox(mode="workspace-write")

    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: None)
    monkeypatch.setattr(landlock_run, "_landlock_abi", lambda: (_ for _ in ()).throw(OSError(38, "Function not implemented")))

    result = sandboxed_command_argv(
        (sys.executable, "-c", "pass"),
        cwd=tmp_path,
        sandbox=sandbox,
    )

    assert isinstance(result, str)
    assert "unavailable" in result


def test_landlock_backend_reports_unavailable_when_kernel_lacks_landlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit ``landlock`` backend must also surface the kernel probe
    failure rather than attempting to wrap with a missing syscall."""
    import looplane.runtime as runtime

    sandbox = CommandSandbox(mode="workspace-write", backend="landlock")

    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(landlock_run, "_landlock_abi", lambda: (_ for _ in ()).throw(OSError(38, "Function not implemented")))

    result = sandboxed_command_argv(
        (sys.executable, "-c", "pass"),
        cwd=tmp_path,
        sandbox=sandbox,
    )

    assert isinstance(result, str)
    assert "landlock" in result
    assert "unavailable" in result


def test_bubblewrap_backend_unaffected_by_landlock_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``bubblewrap`` backend must keep its own ``bwrap`` lookup independent
    of Landlock availability, and fall through to its existing
    ``unavailable`` string when bwrap is missing."""
    import looplane.runtime as runtime

    sandbox = CommandSandbox(mode="workspace-write", backend="bubblewrap")

    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: None)

    result = sandboxed_command_argv(
        (sys.executable, "-c", "pass"),
        cwd=tmp_path,
        sandbox=sandbox,
    )


