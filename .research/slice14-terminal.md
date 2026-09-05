# Slice 1.4 terminal feature extraction

Status: complete within assigned Slice 1.4 scope; focused gates green. No stage or commit.

Scope: terminal feature widgets only. App, event projection, and conversation binding
remain in tui.py for the main agent's Slice 1.5.

Owners: approvals.py, composer.py, scroll.py, transcript.py, tool_widgets.py,
selectors.py, status_widgets.py, onboarding.py, clipboard.py, links.py.
Pure leaf status/types/events retain their existing owners and import behavior.

Approval policy receives typed request/permission callbacks and explicit session grants.
Tool groups receive a typed verbose callback. Legacy policy and formatter contracts
are adapted only in tui.py. Feature modules never import compatibility facades.
Onboarding screen and all worker groups moved together with their lifecycle guards.
Transcript/tool class CSS moved with their widgets; App keeps composition/layout CSS.
Clipboard/link compatibility facades preserve the old imports and dependency patch paths.
Clipboard/link tests moved to tests/terminal; cross-feature TUI tests remain in place.

## Validation evidence

- Pre-extraction characterization: `uv run pytest -q tests/test_tui.py
  tests/test_tui_clipboard.py tests/test_tui_links.py tests/terminal` exited 0;
  171 tests passed. Log: `.research/slice14-baseline.log`.
- Post-extraction existing behavior: `uv run pytest -q tests/test_tui.py
  tests/terminal tests/test_tui_pty.py` exited 0; 175 tests passed. This run
  preceded creation of `test_feature_widgets.py`. Log: `.research/slice14-focused.log`.
- New feature contracts and lazy imports: `uv run pytest
  tests/terminal/test_feature_widgets.py tests/test_lazy_imports.py -o addopts='' -q`
  exited 0; 33 passed in 4.97s. Log: `.research/slice14-contracts.log`.
- Import architecture and startup: `uv run pytest tests/test_modularization_boundaries.py
  tests/test_startup_cache.py tests/test_startup_trace.py -o addopts='' -q`
  exited 0; 28 passed in 2.40s. Log: `.research/slice14-boundaries.log`.
- `uv run ruff check src/looplane/tui.py src/looplane/tui_clipboard.py
  src/looplane/tui_links.py src/looplane/terminal tests/terminal tests/test_tui.py
  tests/test_tui_pty.py`: all checks passed after the final test correction.

Existing tests retain the cross-component focus, composer Enter/newlines/arrows,
number choices, Escape, selection copy, resize/draft preservation, onboarding worker
cancellation, model fetch, and real PTY coverage. New tests exercise canonical import
isolation and facade object identity, standalone modal focus/Enter/arrows/numbers/Escape,
actual modal Unmount delivery, dynamic permission callback/session grants, and tool
group completion/manual expansion without an App private-field dependency.

The first new modal test incorrectly expected `is_mounted` to reset after dismissal.
All keyboard decisions already passed; the test now waits for the actual Unmount
event. No production lifecycle behavior was altered to satisfy that assertion.

## Compatibility and integration notes

- `tui.py` reexports the canonical widget classes. Its `TextualApprovalPolicy` is a
  narrow legacy constructor adapter; the implementation in `terminal/approvals.py`
  accepts typed approval and permission-mode callbacks plus the explicit grant set.
- `tui.RuntimeMetrics` is a narrow compatibility subclass injecting a formatter
  callback, preserving `monkeypatch.setattr(tui, 'format_token_count', ...)` even
  after construction. Canonical metrics have no imports back to `tui.py`.
- Tool groups own their action collection and user-toggle state. App supplies
  `is_verbose=lambda: self._tool_verbose` at composition time; the feature does not
  inspect App private fields.
- Clipboard dependency monkeypatches under `tui_clipboard.shutil` and
  `tui_clipboard.subprocess` retain identity with canonical module dependencies.
  App selection-copy patch targets remain in `tui.py` for Slice 1.5 to preserve.
- Onboarding remains one 668-line feature owner, including credential mount,
  verification, model fetch, controls, and dismissal behavior. No partial worker
  extraction or provider behavior changes were introduced.
- Existing `terminal/status.py`, `terminal/types.py`, and `terminal/events.py`
  remain the canonical leaf owners. Stateful status widgets are deliberately in
  `status_widgets.py` so the pure status import still does not load Textual App.
- `tui.py` is now 3,630 lines (previously 5,282). The remaining App, event sinks,
  projection handlers, and conversation lifecycle are intentionally Slice 1.5 scope.

## Remaining work owned by main

No known Slice 1.4 regressions remain after focused validation. Main should run the
repository-wide Ruff/pytest gates, startup regression script, package build/archive
checks, update the canonical plan, and integrate scoped commits. None of those
repository-wide completion claims are implied by this report. No CLI, commands,
console, SDK, runtime wire, sandbox, or provider implementation files were edited.
No web requests, staging, or commits were performed.
