# Loading copy refinement

- [completed] Audit loading-state rendering, runtime events, and existing tests.
- [completed] Define concise provider-neutral copy and animation behavior inspired by Claude Code and Codex.
- [completed] Implement the smallest coherent TUI change without touching unrelated worktree edits.
- [completed] Add focused tests and render representative screenshots.
- [completed] Run targeted verification and record the outcome here.

## Outcome

- Loading copy now uses a six-step muted-to-bold glimmer synchronized with the fixed-width otter frames.
- Runtime phases now report `Thinking…`, `Responding…`, concrete tool activity, verification, and permission waits consistently.
- Elapsed time appears as a dim suffix after 16 seconds; reduced-motion mode disables both refresh loops.
- Generic external activity no longer immediately overwrites its concrete action label.
- Deterministic screenshot rendering freezes both the otter and text frame.

## Verification

- `uv run pytest tests/test_tui.py -q`: 37 passed.
- `uv run pytest -q`: passed (complete suite).
- `uv run ruff check src/rivumi/tui.py tests/test_tui.py scripts/render_tui_screenshot.py`: passed.
- `git diff --check`: passed.
- Visually inspected all six thinking frames plus wide responding and narrow tool screenshots under `.artifacts/tui/loading-copy-*`.
