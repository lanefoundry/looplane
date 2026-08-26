# Claude-style inline option controls

## Outcome

Replace modal/form-like option configuration for model/runtime and text-only permission status with
keyboard-first inline transcript selectors matching Claude Code's interaction pattern.

## Tasks

- [x] Audit current Rivumi option-control entry points and Claude Code reference components.
- [x] Define a reusable inline selector contract and narrow-terminal layout.
- [x] Implement model/runtime and permissions selectors without unrelated TUI chrome.
- [x] Add focused keyboard, cancellation, selection, and viewport tests.
- [x] Run focused tests, formatting, linting, and inspect the final diff.

## Constraints

- Preserve unrelated dirty-worktree changes.
- Keep selectors in the conversation flow; do not open a full-screen modal.
- Visible keyboard focus, deterministic Escape cancellation, no automatic model prompt submission.
- Model/runtime changes must retain the existing native-conversation switching behavior.

## Status

Complete.

## Result

- Bare `/model`, `/runtime`, and `/permissions` now open a transcript-native selector.
- Exact slash commands execute on Enter even while the composer palette is visible; argument
  completion remains available after a space.
- The active choice is marked, arrow keys move, Enter commits, and Escape or Ctrl+C cancels without
  mutation.
- The empty composer is hidden while the selector owns focus and restored afterward.
- Ctrl+L opens the inline model picker when the runtime is configured; first-run/custom API model
  entry retains the onboarding form because it requires free text and provider configuration.
- A deterministic 100x28 visual artifact was reviewed at
  `.artifacts/tui/inline-model-selector.svg.png`.

## Verification

- `uv run pytest -q`: passed.
- `uv run pytest -q tests/test_tui.py`: 54 passed.
- `uv run ruff check src/rivumi/tui.py tests/test_tui.py`: passed.
- `uv run ruff format src/rivumi/tui.py tests/test_tui.py`: no changes after formatting.
- Repository-wide `ruff format --check .` remains red on 24 unrelated pre-existing files; the two
  touched product/test files pass their focused format check.
