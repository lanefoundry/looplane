# Claude Code source-invariant research

## Objective

Verify from the locally installed Claude Code bundle/source why standalone Claude
Code does not exhibit PCA's whole-repository `source filesystem changed` cleanup
failure.

## Questions

- [complete] Does Claude Code snapshot/hash the whole repository for a session?
- [complete] How does it detect stale files or concurrent edits before writing?
- [complete] Are ignored/untracked/cache files part of any terminal integrity check?
- [complete] What sandbox/worktree boundary differs from PCA's wrapper?
- [complete] Record source excerpts, confidence, and limitations in a research note.

## Sources

- Primary: locally installed Claude Code executable/package bundle and SDK package.
- Web: Groundlane only if mounted; otherwise no web fallback under project rules.

## Result

See `docs/research/2026-08-22-claude-code-file-conflict-architecture.md`.
