# Approval context repair

## Goal

Replace the opaque `No preview supplied` approval state with enough bounded,
non-markup context for a user to make an informed decision.

## Plan

- [completed] Trace approval producers and identify why the screenshot has an empty preview.
- [completed] Add a safe fallback summary from structured request data.
- [completed] Update TUI and approval-policy tests without disturbing unrelated worktree changes.
- [completed] Run focused tests and record results here.

## Constraints

- Preserve existing user changes in the shared dirty worktree.
- Never invent a diff when the runtime did not provide one.
- Keep untrusted command/tool text rendered with markup disabled.

## Result

- Codex file-change approvals reuse validated tool-start context: action, workspace,
  file count, and paths.
- Empty third-party previews render an explicit action/effect fallback and focus Deny.
- `uv run ruff check` passed for all four touched source/test files.
- 62 focused approval, controller, Codex adapter, and TUI tests passed.
- Ruff format passed for the new adapter/tests. The existing dirty `tui.py` still has
  two unrelated pre-existing formatting differences at lines 1633 and 1685; they
  were intentionally left untouched.
