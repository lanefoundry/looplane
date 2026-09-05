import sys
from pathlib import Path

import pytest

from looplane.execution.local_process import run_local_process as run_bounded_command
from looplane.sandbox import landlock_run
from looplane.sandbox.launcher import sandboxed_command_argv
from looplane.sandbox.policy import (
    CommandSandbox,
    python_runtime_read_roots,
    resolve_command_sandbox,
)


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
