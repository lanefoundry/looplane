# Native conversation source-invariant fix

- [x] Trace the observed cleanup failure to native ConversationWorkspace.
- [x] Verify target-file conflict checks already exist at edit/apply boundaries.
- [x] Remove whole-source snapshot capture and terminal equality checks.
- [x] Make Claude/Codex cleanup independent of source worktree drift.
- [x] Replace invariant tests with concurrent-source-drift regression tests.
- [x] Add stale-target patch regression coverage.
- [x] Run focused tests, full tests, lint, and diff checks.

Verification:

- Full pytest suite: passed (343 tests).
- Focused regression suite: passed (39 tests).
- `ruff check .`: passed.
- Targeted `ruff format --check`: passed.
- `uv lock --check`: passed.
- `git diff --check`: passed.
- Repository-wide format check remains noisy from unrelated pre-existing files;
  no unrelated formatting was applied.

ExternalCodingRunner is intentionally out of scope: it is a separate one-shot
acceptance boundary and reports a different terminal reason.
