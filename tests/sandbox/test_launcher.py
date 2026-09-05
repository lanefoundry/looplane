"""Platform selection, normalized policy, and fail-closed launch contracts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from looplane.execution.local_process import run_local_process
from looplane.sandbox import landlock_run, launcher
from looplane.sandbox.linux import _linux_bwrap_argv
from looplane.sandbox.macos import _macos_sandbox_profile
from looplane.sandbox.policy import (
    CommandSandbox,
    _normalize_sandbox_roots,
    resolve_command_sandbox,
)


@pytest.mark.parametrize(
    "platform,backend,bwrap,landlock,expected",
    [
        ("darwin", "auto", None, False, "macOS sandbox-exec is unavailable"),
        ("linux", "auto", None, False, "Linux command sandbox is unavailable on this kernel"),
        ("linux", "bubblewrap", None, True, "Linux bubblewrap sandbox is unavailable"),
        ("linux", "landlock", "/bin/bwrap", False, "Linux landlock sandbox is unavailable"),
        ("win32", "auto", None, False, "OS command sandbox is unavailable on this platform"),
    ],
)
def test_unavailable_sandbox_never_starts_process(
    tmp_path,
    monkeypatch,
    platform,
    backend,
    bwrap,
    landlock,
    expected,
):
    marker = tmp_path / "forbidden"
    monkeypatch.setattr(launcher.sys, "platform", platform)
    monkeypatch.setattr(launcher.shutil, "which", lambda _: bwrap)
    monkeypatch.setattr(launcher, "landlock_available", lambda: landlock)
    result = run_local_process(
        (sys.executable, "-c", f"open({str(marker)!r},'w').close()"),
        cwd=tmp_path,
        timeout_seconds=2,
        max_output_chars=1000,
        sandbox=CommandSandbox(mode="workspace-write", backend=backend),
    )
    assert result.returncode == 126 and result.stderr == expected
    assert not marker.exists()


def test_macos_profile_escapes_paths_and_keeps_default_deny(tmp_path, monkeypatch):
    cwd = tmp_path / 'quote"slash\\'
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "/usr/bin/sandbox-exec")
    argv = launcher.sandboxed_command_argv(
        ("check", "literal argument"),
        cwd=cwd,
        sandbox=CommandSandbox(mode="workspace-write"),
    )
    assert argv[:2] == ("/usr/bin/sandbox-exec", "-p")
    assert "(deny default)" in argv[2] and "(allow network" not in argv[2]
    assert '(allow file-read-data (literal "/"))' in argv[2]
    assert '(subpath "/")' not in argv[2]
    assert 'quote\\"slash\\\\' in argv[2]
    assert argv[-2:] == ("check", "literal argument")


def test_linux_auto_prefers_bwrap_and_falls_back_to_canonical_landlock(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "/bin/bwrap")

    def unexpected_probe():
        raise AssertionError("bwrap must not probe Landlock")

    monkeypatch.setattr(launcher, "landlock_available", unexpected_probe)
    policy = CommandSandbox(mode="workspace-write", read_roots=(tmp_path,))
    assert (
        launcher.sandboxed_command_argv(("check",), cwd=tmp_path, sandbox=policy)[0] == "/bin/bwrap"
    )
    monkeypatch.setattr(launcher.shutil, "which", lambda _: None)
    monkeypatch.setattr(launcher, "landlock_available", lambda: True)
    argv = launcher.sandboxed_command_argv(("check",), cwd=tmp_path, sandbox=policy)
    assert Path(argv[1]) == Path(landlock_run.__file__)
    assert json.loads(argv[3])["read_roots"] == [str(tmp_path)]


def test_bwrap_write_roots_are_not_remounted_readonly(tmp_path):
    args = _linux_bwrap_argv(
        ("check",),
        tmp_path,
        executable="bwrap",
        read_roots=(tmp_path, tmp_path / "child"),
        writable_roots=(tmp_path,),
    )
    assert args.count("--bind") == 1 and "--ro-bind" not in args
    assert "--unshare-all" in args


@pytest.mark.skipif(sys.platform != "darwin", reason="Real macOS sandbox-exec contract")
def test_macos_real_sandbox_allows_workspace_and_denies_outside_write(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    forbidden = tmp_path / "outside"
    script = (
        "from pathlib import Path\n"
        "Path('allowed').write_text('ok')\n"
        "try:\n"
        f" Path({str(forbidden)!r}).write_text('forbidden')\n"
        "except PermissionError:\n"
        " print('denied')\n"
        "else:\n"
        " raise AssertionError('sandbox allowed outside write')\n"
    )
    result = run_local_process(
        (sys.executable, "-c", script),
        cwd=workspace,
        timeout_seconds=5,
        max_output_chars=2000,
        sandbox=resolve_command_sandbox(cwd=workspace, task_home=tmp_path / "task-home"),
    )
    if launcher.shutil.which("sandbox-exec") is None:
        assert result.returncode == 126 and not (workspace / "allowed").exists()
        pytest.skip("sandbox-exec unavailable; fail-closed checked, enforcement unmeasured")
    assert result.ok, result.stderr
    assert result.stdout == "denied\n"
    assert (workspace / "allowed").read_text() == "ok"
    assert not forbidden.exists()


def test_invalid_roots_and_macos_backend_rejected(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        _normalize_sandbox_roots((Path("nul\x00path"),), label="read")
    with pytest.raises(ValueError):
        _macos_sandbox_profile(tmp_path, read_roots=(), writable_roots=(Path("nul\x00"),))
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    with pytest.raises(ValueError, match="backend on macOS"):
        launcher.sandboxed_command_argv(
            ("check",),
            cwd=tmp_path,
            sandbox=CommandSandbox(mode="workspace-write", backend="landlock"),
        )


def test_landlock_entry_rejects_invalid_policy_before_exec(monkeypatch, capsys):
    monkeypatch.setattr(landlock_run.sys, "platform", "linux")
    monkeypatch.setattr(landlock_run.os, "execvp", lambda *_: pytest.fail("must not exec"))
    assert landlock_run.main(["--policy-json", "[]", "--", "check"]) == 126
    assert "policy must be an object" in capsys.readouterr().err


@pytest.mark.parametrize("stage", ["_landlock_abi", "_create_ruleset", "_install_seccomp_filter"])
def test_landlock_setup_failure_never_executes_command(tmp_path, monkeypatch, stage):
    monkeypatch.setattr(landlock_run.sys, "platform", "linux")
    for name in (
        "_landlock_abi",
        "_create_ruleset",
        "_prctl_no_new_privs",
        "_install_seccomp_filter",
    ):
        monkeypatch.setattr(landlock_run, name, lambda: 123)
    monkeypatch.setattr(landlock_run, "_add_path_rule", lambda *_: None)
    monkeypatch.setattr(landlock_run, "_add_device_file_rule", lambda *_: None)
    monkeypatch.setattr(landlock_run, "_syscall", lambda *_: 0)
    monkeypatch.setattr(landlock_run.os, "close", lambda _: None)
    monkeypatch.setattr(landlock_run.os, "execvp", lambda *_: pytest.fail("must not exec"))

    def fail():
        raise OSError("fixture denied")

    monkeypatch.setattr(landlock_run, stage, fail)
    policy = json.dumps({"cwd": str(tmp_path), "read_roots": [], "writable_roots": []})
    assert landlock_run.main(["--policy-json", policy, "--", "check"]) == 126
