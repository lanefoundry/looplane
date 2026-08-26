# Warp UI investigation

Status: implemented and verified

- [x] Fetch the current official Codex configuration reference.
- [x] Inspect repository and local Codex configuration for TUI settings.
- [x] Compare Codex alternate-screen rendering with Claude Code inline rendering in Warp.
- [x] Confirm the least invasive configuration change and its trade-offs.

Evidence:

- Official Codex config reference documents `tui.alternate_screen = "auto" | "always" | "never"`.
- Default is `auto`; the documented automatic exception is Zellij, not Warp.
- Local Codex is 0.149.0 and exposes `--no-alt-screen` for an inline, scrollback-preserving run.
- Warp identifies itself as `TERM_PROGRAM=WarpTerminal`; no local Codex TUI override was found.
- PCA M9 deliberately adopted a Textual full-screen application; Textual owns alternate-screen behavior.
- PCA M11 changed transcript semantics but explicitly retained full-screen presentation.
- `#transcript` consumes `1fr`, while `#messages` is `height: auto` with no bottom alignment, so sparse content lays out from the top.

Conclusion:

The mismatch is the combination of an intentional full-screen shell and a missing bottom-anchor rule,
not a Codex/Claude runtime discrepancy. A small full-screen fix can bottom-align sparse transcript
content; true Claude-style inline scrollback requires a separate inline renderer or running the
official Codex CLI with `--no-alt-screen`.

Implementation gate requested by user:

- [x] Add a geometry regression test for sparse transcript bottom anchoring.
- [x] Add a deterministic screenshot command to the development workflow.
- [x] Render and inspect the screenshot at wide and narrow terminal sizes.
- [x] Run focused and full verification.

Verification evidence:

- `uv run pytest tests/test_tui.py -q`: passed.
- `uv run pytest -q`: passed.
- `uv run ruff check .`: passed.
- `git diff --check`: passed.
- Wide screenshot: `.artifacts/tui/wide.png` (120x36 terminal cells), visually inspected.
- Narrow screenshot: `.artifacts/tui/narrow.png` (60x22 terminal cells), visually inspected.

Follow-up runtime findings:

- [x] Root-cause Codex command `cwd` being misclassified as a changed file path.
- [x] Restrict changed-path normalization to `FILE_CHANGE` events, matching the Claude adapter.
- [x] Add a regression for a command whose `cwd` is the disposable workspace root.
- [x] Verify the focused and complete suites after the runtime fix.
- [x] Map Claude-style loading states to waiting, streaming, tool, approval, and terminal events.
- [x] Add a native Textual loading indicator that pauses while hidden.
- [x] Add loading transition coverage and an active-state screenshot workflow.
- [x] Render and inspect the loading screenshot, then run complete verification.

Follow-up verification evidence:

- `uv run pytest tests/test_codex_conversation.py tests/test_codex_app_server.py -q`: 20 passed.
- `uv run pytest -q`: 333 passed.
- `uv run ruff check .`: passed.
- `git diff --check`: passed.
- Thinking screenshot: `.artifacts/tui/loading-wide.png`, visually inspected.
- Tool-running screenshot: `.artifacts/tui/loading-narrow.png`, visually inspected.

IME placeholder follow-up:

- [x] Confirm Textual 8.2.8 exposes no IME composition/preedit state.
- [x] Remove the focused composer placeholder so Warp-owned preedit has nothing to overlap.
- [x] Retain the native Input, block cursor, and existing shortcut hint.
- [x] Regenerate screenshots and run complete verification after the composer change.

IME verification evidence:

- `uv run pytest tests/test_tui.py -q`: 30 passed.
- `uv run pytest -q`: 333 passed.
- `uv run ruff check .`: passed.
- `git diff --check`: passed.
- Focused wide composer: `.artifacts/tui/ime-placeholder-wide.png`, visually inspected.
- Focused narrow composer: `.artifacts/tui/ime-placeholder-narrow.png`, visually inspected.
- macOS/Warp preedit cannot be synthesized by Textual Pilot; live IME confirmation remains manual.

IME cursor follow-up:

- [x] Confirm the white cell is Textual's virtual `input--cursor`, not the removed placeholder.
- [x] Replace the reverse block with a theme-colored underline cursor that does not cover preedit.
- [x] Verify component styles, regenerate screenshots, and run complete verification.

Cursor verification evidence:

- `tests/test_tui.py` asserts an empty focused placeholder, input-colored cursor background, and
  underline style.
- Inspected `.artifacts/tui/ime-cursor-{wide,narrow}.png`.
- macOS/Warp live preedit remains the one manual-only boundary because Textual Pilot receives only
  committed terminal input.
## Claude Code 2.1.238 loading audit

- Exact local binary: `/Users/xiaoxu/.local/share/claude/versions/2.1.238`.
- Active local version on 2026-08-22 is already `2.1.239`; the preserved 2.1.238 binary remains directly auditable.
- Extract minified spinner/state functions and compare their predicates with the older readable 2.1.88 source before reporting exact behavior.
- Confirmed from the 2.1.238 bundle:
  - spinner state is per-agent in a centralized store; default mode is `responding`;
  - supported stream modes are `tool-input`, `tool-use`, `requesting`, `responding`, and `thinking`;
  - the spinner renders only while the currently viewed agent is loading, is suppressed by permission/prompt waits, and is normally hidden when a streaming preview is visible;
  - the animated row refreshes at 50 ms in `requesting` mode and 100 ms otherwise, with animation disabled for reduced motion;
  - tool/thinking duration, token count, stalled-state animation, retry and compaction states are integrated into the same row;
  - tool rows independently render `Waiting for permission…` while approval is pending.

## PCA loading parity implementation

- [x] Replace the first-delta loading cutoff with a displayable-preview boundary.
- [x] Keep spinner states explicit across requesting, thinking, tool use, permission, and completion.
- [x] Pause hidden looping animation and honor Textual reduced-motion capability where available.
- [x] Add focused state-machine tests, including partial-line streaming behavior.
- [x] Capture and inspect wide and narrow screenshots for active loading states.
- [x] Run focused tests, full test suite, Ruff, and `git diff --check`.

Verification evidence:

- `uv run pytest tests/test_tui.py -q`: 31 passed.
- `uv run pytest -q`: all 334 collected tests passed.
- `uv run ruff check .`: passed.
- `git diff --check`: passed.
- Inspected wide and narrow PNGs for thinking, streaming, tool, and permission states under `.artifacts/tui/loading-238-*`.

## Claude-style approval continuation

- [x] Correct the source interpretation: current Claude Code appends tool permission after messages
  inside the transcript `ScrollBox`; it is not a generic pinned-bottom layout slot.
- [x] Replace PCA's four horizontal colored buttons with a compact vertical numbered choice list.
- [x] Show only available decisions; support arrows/Enter, direct number keys, and Escape.
- [x] Use a `›` focus pointer and reserve warning color for the authorization boundary/focus.
- [x] Bound the literal preview and replace empty-preview filler with actionable fail-safe context.
- [x] Default an empty preview to Deny when Deny is available.
- [x] Add wide-to-narrow geometry, literal content, decision, safe-default, and cancellation tests.
- [x] Render and inspect `.artifacts/tui/approval-{wide,narrow}.png`.

Approval verification evidence:

- `uv run pytest tests/test_tui.py -q`: 33 passed.
- `uv run pytest -q`: all 337 collected tests passed.
- Approval timing-sensitive test passed 10 consecutive focused runs.
- `uv run ruff check .`: passed.
- `git diff --check`: passed.
