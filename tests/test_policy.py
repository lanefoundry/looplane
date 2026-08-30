from pathlib import Path

import pytest

from looplane.policy import PathPolicyError, SafePathPolicy


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src" / "module.py").write_text("VALUE = 1\n")
    return root


def test_resolves_allowed_relative_path(workspace: Path) -> None:
    policy = SafePathPolicy(workspace, allowed_paths=("src/**",))

    assert policy.resolve("src/module.py") == workspace / "src" / "module.py"


@pytest.mark.parametrize("path", ["../secret.txt", "src/../../secret.txt"])
def test_rejects_parent_traversal(workspace: Path, path: str) -> None:
    policy = SafePathPolicy(workspace, allowed_paths=("src/**",))

    with pytest.raises(PathPolicyError, match="(?i)(outside|traversal|relative|path)"):
        policy.resolve(path)


def test_rejects_absolute_path_even_when_it_points_inside_workspace(workspace: Path) -> None:
    policy = SafePathPolicy(workspace, allowed_paths=("src/**",))

    with pytest.raises(PathPolicyError, match="(?i)(absolute|relative|path)"):
        policy.resolve(workspace / "src" / "module.py")


def test_rejects_symlink_escape(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    (workspace / "src" / "escape.txt").symlink_to(outside)
    policy = SafePathPolicy(workspace, allowed_paths=("src/**",))

    with pytest.raises(PathPolicyError, match="(?i)(outside|symlink|workspace|path)"):
        policy.resolve("src/escape.txt")


def test_rejects_path_outside_allowed_patterns(workspace: Path) -> None:
    (workspace / "tests").mkdir()
    policy = SafePathPolicy(workspace, allowed_paths=("src/**",))

    with pytest.raises(PathPolicyError, match="(?i)(allow|path)"):
        policy.resolve("tests/test_module.py")


def test_single_star_glob_does_not_cross_path_segments(workspace: Path) -> None:
    (workspace / "src" / "package").mkdir()
    (workspace / "src" / "package" / "nested.py").write_text("VALUE = 2\n")
    policy = SafePathPolicy(workspace, allowed_paths=("src/*.py",))

    assert policy.resolve("src/module.py") == workspace / "src" / "module.py"
    with pytest.raises(PathPolicyError, match="(?i)(allow|path)"):
        policy.resolve("src/package/nested.py")


def test_double_star_glob_explicitly_crosses_path_segments(workspace: Path) -> None:
    nested = workspace / "src" / "package" / "nested.py"
    nested.parent.mkdir()
    nested.write_text("VALUE = 2\n")
    policy = SafePathPolicy(workspace, allowed_paths=("src/**",))

    assert policy.resolve("src/package/nested.py") == nested
