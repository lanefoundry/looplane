from __future__ import annotations

import subprocess
import sys

import pytest

from looplane.contracts import VerificationCommand
from looplane.publishing import PublishError, publish_changes


def git(repo, *args):
    return subprocess.run(
        ("git", *args), cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.com")
    (source / "file").write_text("base\n")
    git(source, "add", "file")
    git(source, "commit", "-m", "base")
    base = git(source, "rev-parse", "HEAD")
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(bare))
    git(source, "remote", "add", "origin", str(bare))
    work = tmp_path / "work"
    git(tmp_path, "clone", str(source), str(work))
    git(work, "config", "user.name", "Test")
    git(work, "config", "user.email", "test@example.com")
    (work / "file").write_text("changed\n")
    git(work, "add", "file")
    git(work, "commit", "-m", "change")
    return source, work, bare, base


def publish(repos, **kwargs):
    source, work, _, base = repos
    return publish_changes(
        source_repo=source,
        workspace=work,
        base_sha=base,
        branch=kwargs.pop("branch", "publication"),
        verification=kwargs.pop(
            "verification",
            (VerificationCommand(name="check", argv=(sys.executable, "-c", "pass")),),
        ),
        **kwargs,
    )


def test_publish_preserves_source(repos):
    source, work, bare, base = repos
    result = publish(repos)
    assert result.head == git(work, "rev-parse", "HEAD")
    assert git(bare, "rev-parse", "refs/heads/publication") == result.head
    assert git(source, "rev-parse", "HEAD") == base
    assert git(source, "status", "--porcelain") == ""
    assert "publication" not in git(source, "branch", "--list")


def test_failed_check_never_pushes(repos):
    checks = (VerificationCommand(name="bad", argv=(sys.executable, "-c", "exit(1)")),)
    with pytest.raises(PublishError, match="verification"):
        publish(repos, verification=checks)
    assert not (repos[2] / "refs/heads/publication").exists()


def test_missing_remote(repos):
    git(repos[0], "remote", "remove", "origin")
    with pytest.raises(PublishError, match="remote lookup"):
        publish(repos)


def test_dirty_workspace(repos):
    (repos[1] / "file").write_text("dirty")
    with pytest.raises(PublishError, match="clean"):
        publish(repos)


@pytest.mark.parametrize("branch", ["-bad", "bad..name", "HEAD", "refs/../bad"])
def test_invalid_branch(repos, branch):
    with pytest.raises(PublishError):
        publish(repos, branch=branch)


def test_non_descendant(repos):
    git(repos[1], "checkout", "--orphan", "other")
    git(repos[1], "commit", "-m", "unrelated")
    with pytest.raises(PublishError, match="ancestry"):
        publish(repos)


def test_rejected_push(repos):
    publish(repos)
    git(repos[1], "reset", "--hard", repos[3])
    (repos[1] / "file").write_text("different\n")
    git(repos[1], "add", "file")
    git(repos[1], "commit", "-m", "divergent")
    with pytest.raises(PublishError, match="remote push"):
        publish(repos)


def test_patch_publication(repos):
    source, _, bare, base = repos
    (source / "file").write_text("patch\n")
    patch = git(source, "diff", "--binary", "HEAD") + "\n"
    result = publish(repos, patch=patch)
    assert git(bare, "show", f"{result.head}:file") == "patch"
    assert git(source, "rev-parse", "HEAD") == base
    assert (source / "file").read_text() == "patch\n"


def test_multiple_push_urls(repos):
    git(repos[0], "remote", "set-url", "--add", "--push", "origin", str(repos[2]))
    git(repos[0], "remote", "set-url", "--add", "--push", "origin", "https://example.com/secret")
    with pytest.raises(PublishError, match="unambiguous"):
        publish(repos)


def test_requires_checks(repos):
    with pytest.raises(PublishError, match="requires verification"):
        publish(repos, verification=())


def test_verification_has_no_host_secrets(repos, monkeypatch):
    monkeypatch.setenv("HOST_SECRET_TOKEN", "never-pass-this")
    command = VerificationCommand(
        name="environment",
        argv=(
            sys.executable,
            "-c",
            "import os; assert 'HOST_SECRET_TOKEN' not in os.environ; "
            "assert 'CODING_AGENT_TASK_HOME' in os.environ",
        ),
    )
    publish(repos, verification=(command,))


def test_verification_cannot_modify_git_configuration(repos):
    command = VerificationCommand(
        name="mutate-config",
        argv=(
            sys.executable,
            "-c",
            "from pathlib import Path; p=Path('.git/config'); "
            "p.write_text(p.read_text()+'\\n[alias]\\n x = status\\n')",
        ),
    )
    with pytest.raises(PublishError, match="Git configuration"):
        publish(repos, verification=(command,))


def test_verbose_verification_allowed(repos):
    command = VerificationCommand(
        name="verbose",
        argv=(sys.executable, "-c", "print('x'*30000)"),
    )
    publish(repos, verification=(command,))


def test_publish_source_default_branch_name(repos):
    branch = git(repos[0], "branch", "--show-current")
    result = publish(repos, branch=branch)
    assert git(repos[2], "rev-parse", f"refs/heads/{branch}") == result.head
    assert git(repos[0], "rev-parse", "HEAD") == repos[3]


def test_publication_sandbox_settings_apply_only_to_checks(repos, monkeypatch, tmp_path):
    import looplane.publishing as publishing

    original_run = publishing.run_local_process
    original_resolve = publishing.resolve_command_sandbox
    requests = []
    executions = []
    extra = tmp_path / "toolchain"
    extra.mkdir()

    def resolve(**kwargs):
        requests.append(kwargs)
        assert kwargs["task_home"].is_dir()
        return original_resolve(**kwargs)

    def run(argv, **kwargs):
        executions.append((argv, dict(kwargs)))
        # Inspect policy without depending on this host's OS sandbox availability.
        kwargs.pop("sandbox")
        return original_run(argv, **kwargs)

    monkeypatch.setattr(publishing, "resolve_command_sandbox", resolve)
    monkeypatch.setattr(publishing, "run_local_process", run)
    publish(
        repos,
        sandbox_checks=True,
        sandbox_profile="verification",
        sandbox_backend="landlock",
        sandbox_read_roots=(extra,),
    )
    assert len(requests) == 1
    request = requests[0]
    assert request["profile"] == "verification"
    assert request["backend"] == "landlock"
    assert request["extra_read_roots"] == (extra,)
    for argv, kwargs in executions:
        sandbox = kwargs["sandbox"]
        if argv[0] == "git":
            assert sandbox is None
        else:
            assert sandbox is not None
            assert sandbox.backend == "landlock"
            assert sandbox.profile == "verification"
            assert request["cwd"] == kwargs["cwd"]
            assert extra in sandbox.read_roots
            assert sandbox.writable_roots == (request["task_home"].resolve(),)
            assert kwargs["env"]["CODING_AGENT_TASK_HOME"] == str(request["task_home"])


def test_unavailable_publication_sandbox_never_pushes(repos, monkeypatch, tmp_path):
    import looplane.execution.local_process as local_process

    marker = tmp_path / "must-not-run"
    checks = (
        VerificationCommand(
            name="sandbox-required",
            argv=(sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ran')"),
        ),
    )
    original_launcher = local_process.sandboxed_command_argv

    def launcher(argv, *, cwd, sandbox):
        if sandbox is not None:
            return "required sandbox is unavailable"
        return original_launcher(argv, cwd=cwd, sandbox=sandbox)

    monkeypatch.setattr(local_process, "sandboxed_command_argv", launcher)
    with pytest.raises(PublishError, match="verification"):
        publish(repos, verification=checks, sandbox_checks=True)
    assert not marker.exists()
    assert not (repos[2] / "refs/heads/publication").exists()


def test_invalid_publication_sandbox_configuration_never_pushes(repos):
    with pytest.raises(PublishError, match="sandbox configuration"):
        publish(repos, sandbox_checks=True, sandbox_backend="unsupported")
    assert not (repos[2] / "refs/heads/publication").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS sandbox-exec")
def test_real_publication_sandbox_blocks_host_reads(repos):
    import shutil
    import tempfile
    from pathlib import Path

    if shutil.which("sandbox-exec") is None:
        pytest.skip("sandbox-exec unavailable")
    # macOS policy permits system temp reads; keep the probe in private home space.
    with tempfile.TemporaryDirectory(prefix="looplane-private-probe-", dir=Path.home()) as root:
        private_file = Path(root) / "private-host-file"
        private_file.write_text("host secret")
        command = VerificationCommand(
            name="host-isolation",
            argv=(
                sys.executable,
                "-c",
                "from pathlib import Path\n"
                f"p = Path({str(private_file)!r})\n"
                "try:\n p.read_text()\nexcept PermissionError:\n pass\n"
                "else:\n raise AssertionError('host file was readable')\n",
            ),
        )
        publish(repos, verification=(command,), sandbox_checks=True)
