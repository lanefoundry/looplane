"""Keep a disposable workspace anchored at its pinned base commit.

The agent can run arbitrary shell commands, so it can move ``HEAD`` (``git commit``,
``git checkout``, ``git reset``) or stage changes (``git add``). Patch collection,
verification fingerprints and session resume all assume that ``HEAD`` is detached at
``base_sha`` and that the index holds the base tree plus intent-to-add entries for new
files. Blocking every Git-mutating command shape is not enforceable, so the harness
restores that invariant after the fact instead. The work tree is never touched, so no
file change is lost: committed or staged edits simply become uncommitted edits again.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from looplane.execution.types import CommandResult

GitRunner = Callable[[Sequence[str]], CommandResult]

_INTENT_TO_ADD_CHUNK = 200


class BaseAnchorError(RuntimeError):
    """Raised when the workspace cannot be re-anchored at its base commit."""


@dataclass(frozen=True)
class BaseAnchorRestore:
    """What was undone while restoring the base anchor."""

    base_sha: str
    previous_head: str
    previous_ref: str | None
    index_restored: bool

    @property
    def head_moved(self) -> bool:
        return self.previous_head != self.base_sha or self.previous_ref is not None

    def describe(self) -> str:
        actions: list[str] = []
        if self.head_moved:
            where = f" ({self.previous_ref})" if self.previous_ref else ""
            actions.append(
                f"HEAD moved to {self.previous_head[:12]}{where}; it was restored to the base "
                f"commit {self.base_sha[:12]}"
            )
        if self.index_restored:
            actions.append("the Git index was reset to the base tree")
        return (
            "looplane: " + " and ".join(actions) + ". Work-tree files were not changed, so "
            "every edit is kept as an uncommitted change. Commits, branches and staging in "
            "this disposable workspace are never exported; the final result is the patch "
            "against the base commit."
        )


def restore_base_anchor(git: GitRunner, base_sha: str, workspace: Path) -> BaseAnchorRestore | None:
    """Re-anchor ``workspace`` at ``base_sha``; return ``None`` when nothing drifted."""

    base = base_sha.lower()
    state = git(("rev-parse", "HEAD", "--symbolic-full-name", "HEAD"))
    if not state.ok:
        raise BaseAnchorError(f"could not read workspace HEAD: {state.stderr.strip()}")
    lines = state.stdout.split()
    head = lines[0].lower() if lines else ""
    ref = lines[1] if len(lines) > 1 and lines[1] != "HEAD" else None

    if head != base and not git(("cat-file", "-e", f"{base}^{{commit}}")).ok:
        raise BaseAnchorError("base commit is missing from the workspace repository")

    staged = git(("diff", "--cached", "--quiet", base, "--"))
    if staged.timed_out or staged.returncode not in {0, 1}:
        raise BaseAnchorError(f"could not compare the index with base: {staged.stderr.strip()}")
    index_drifted = staged.returncode == 1

    if head == base and ref is None and not index_drifted:
        return None

    # Remember index paths that are not part of the base tree (new files registered with
    # intent-to-add, or added by the agent's own commit) so they stay visible in diffs.
    indexed = _nul_paths(git, ("ls-files", "-z"), "list index paths")
    in_base = _nul_paths(git, ("ls-tree", "-r", "-z", "--name-only", base), "list base paths")

    moved = git(("update-ref", "--no-deref", "-m", "looplane: restore base anchor", "HEAD", base))
    if not moved.ok:
        raise BaseAnchorError(f"could not restore HEAD: {moved.stderr.strip()}")
    reset = git(("read-tree", base))
    if not reset.ok:
        raise BaseAnchorError(f"could not reset the index: {reset.stderr.strip()}")

    new_paths = sorted(path for path in indexed - in_base if os.path.lexists(workspace / path))
    for start in range(0, len(new_paths), _INTENT_TO_ADD_CHUNK):
        chunk = new_paths[start : start + _INTENT_TO_ADD_CHUNK]
        intent = git(("add", "--force", "--intent-to-add", "--", *chunk))
        if not intent.ok:
            raise BaseAnchorError(
                f"could not re-register new files after restore: {intent.stderr.strip()}"
            )

    return BaseAnchorRestore(
        base_sha=base,
        previous_head=head,
        previous_ref=ref,
        index_restored=index_drifted,
    )


def _nul_paths(git: GitRunner, argv: Sequence[str], purpose: str) -> set[str]:
    result = git(argv)
    if not result.ok or result.stdout_truncated:
        raise BaseAnchorError(f"could not {purpose}: {result.stderr.strip()}")
    return {path for path in result.stdout.split("\x00") if path}
