from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from looplane.sandbox.policy import _normalize_sandbox_roots


def _sandbox_path_literal(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def _macos_sandbox_profile(
    cwd: Path,
    *,
    read_roots: Sequence[Path],
    writable_roots: Sequence[Path],
) -> str:
    write_roots = []
    for root in (cwd, *writable_roots):
        resolved = Path(root).resolve(strict=False)
        if "\x00" in str(resolved):
            raise ValueError("sandbox writable roots must be NUL-free")
        write_roots.append(resolved)
    read_roots = (
        *_normalize_sandbox_roots((cwd, *read_roots, *writable_roots), label="read"),
        Path("/System"),
        Path("/Library"),
        Path("/usr"),
        Path("/bin"),
        Path("/sbin"),
        Path("/private/var/folders"),
        Path("/private/tmp"),
        Path("/tmp"),
    )
    read_rules = "\n".join(
        f'  (subpath "{_sandbox_path_literal(root)}")' for root in dict.fromkeys(read_roots)
    )
    writable_rules = "\n".join(
        f'  (subpath "{_sandbox_path_literal(root)}")' for root in dict.fromkeys(write_roots)
    )
    return f"""
(version 1)
(deny default)
(allow process*)
(allow sysctl-read)
(allow file-read-metadata)
; Allow dyld to open the root directory itself, without recursive read access.
(allow file-read-data (literal "/"))
(allow file-read*
{read_rules}
)
(allow file-write*
{writable_rules}
)
""".strip()
