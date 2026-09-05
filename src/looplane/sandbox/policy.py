from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandSandbox:
    """OS-level command sandbox request for local verification commands."""

    mode: str
    profile: str = "verification"
    backend: str = "auto"
    read_roots: tuple[Path, ...] = ()
    writable_roots: tuple[Path, ...] = ()


def python_runtime_read_roots() -> tuple[Path, ...]:
    """Return narrow trusted roots required by this Python sandbox wrapper."""

    roots: list[Path] = []
    home = Path.home().resolve(strict=False)
    for value in (sys.prefix, sys.base_prefix):
        root = Path(value).resolve(strict=False)
        if root == Path(root.anchor) or home == root or home.is_relative_to(root):
            continue
        roots.append(root)
    return tuple(dict.fromkeys(roots))


def _normalize_sandbox_roots(roots: Sequence[Path], *, label: str) -> tuple[Path, ...]:
    normalized: list[Path] = []
    for root in roots:
        resolved = Path(root).expanduser().resolve(strict=False)
        if "\x00" in str(resolved):
            raise ValueError(f"sandbox {label} roots must be NUL-free")
        normalized.append(resolved)
    return tuple(dict.fromkeys(normalized))


def resolve_command_sandbox(
    *,
    profile: str | None = "verification",
    backend: str | None = "auto",
    cwd: Path,
    task_home: Path,
    extra_read_roots: Sequence[Path] = (),
    _runtime_read_roots: Callable[[], tuple[Path, ...]] | None = None,
) -> CommandSandbox:
    """Build a normalized verification sandbox request."""

    profile_name = profile or "verification"
    if profile_name != "verification":
        raise ValueError(f"unsupported command sandbox profile: {profile_name}")
    backend_name = backend or "auto"
    if backend_name not in {"auto", "bubblewrap", "landlock"}:
        raise ValueError(f"unsupported command sandbox backend: {backend_name}")
    task_home = task_home.expanduser().resolve(strict=False)
    read_roots = _normalize_sandbox_roots(
        (cwd, task_home, *extra_read_roots, *(_runtime_read_roots or python_runtime_read_roots)()),
        label="read",
    )
    return CommandSandbox(
        mode="workspace-write",
        profile=profile_name,
        backend=backend_name,
        read_roots=read_roots,
        writable_roots=(task_home,),
    )
