import json
import sys
from pathlib import Path

import pytest

from looplane import landlock_run
from looplane.runtime import CommandSandbox, run_bounded_command, sandboxed_command_argv


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
    monkeypatch.setattr(
        landlock_run,
        "_landlock_abi",
        lambda: (_ for _ in ()).throw(OSError(38, "Function not implemented")),
    )

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
    monkeypatch.setattr(
        landlock_run,
        "_landlock_abi",
        lambda: (_ for _ in ()).throw(OSError(38, "Function not implemented")),
    )

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

    assert isinstance(result, str)
    assert "unavailable" in result
