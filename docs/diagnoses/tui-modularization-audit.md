# TUI modularization audit

Date: 2026-09-05  
Scope: `src/looplane/tui.py`, its tests, and local coding-agent references under
`/Users/xiaoxu/Projects/coding-agent-reference`.

## Decision

`tui.py` is a working product surface, but it is no longer a well-bounded module. A
clipboard or hyperlink fix should not add more policy to it. This change therefore
extracts selection/native-copy behavior to `tui_clipboard.py` and safe link handling to
`tui_links.py`. The remaining decomposition should be incremental and protected by
the existing lifecycle tests; it is not safe to move all TUI code as part of one input
bug fix.

## Current inventory

The audit found 5,121 lines in `src/looplane/tui.py` and 4,475 lines in
`tests/test_tui.py` before this decomposition. Its AST contained 31 top-level
classes, 5 top-level functions, and 232 class methods. The largest ownership clusters
are:

| Owner | Size | Responsibilities currently combined |
|---|---:|---|
| `looplaneApp` | about 3,186 lines, 118 methods, 76 initialized attributes | layout, key routing, commands, runtime configuration, conversation lifecycle, event projection, approvals, interruption, transcript export |
| `OnboardingModal` | 638 lines, 31 methods | provider/runtime selection, credentials, model fetching, persistence, modal rendering |
| Approval widgets and policy | about 400 lines | grant policy, modal and inline rendering, focus/key behavior, preview scrolling |
| Transcript/tool widgets | about 300 lines | composer events, scrolling, messages, timeline entries, tool grouping and rendering |

The main risk is not file length by itself. State transitions owned by `looplaneApp`
cross UI widgets, runner resources, conversation storage, and event projection; the
class also performs roughly 157 `query_one` and 48 `query` calls. A large mechanical
split into mixins would distribute the same coupling across files without creating
real module boundaries.

## What the local references do

The reference implementations consistently isolate terminal capabilities and major
surfaces even when their root application remains large:

- Codex has dedicated `clipboard_copy.rs`, `clipboard_paste.rs`, approval-overlay,
  composer, input, session-lifecycle, event-dispatch, and transcript-export modules.
- OpenCode separates `clipboard.ts`, selection utilities, keybinding configuration,
  permission context, prompt context, and route-specific components.
- Pi separates regular-screen and alternate-screen renderers, terminal primitives,
  keybindings, components, and coding-agent clipboard helpers.
- The reconstructed Claude Code snapshot separates Ink selection/terminal I/O,
  hyperlink utilities, command packages, prompt input, and permission components.

The useful pattern is capability ownership: clipboard code decides clipboard routing,
link code decides link safety, approval code owns approval state, and the root app
coordinates them. Language or framework choice does not remove that responsibility.

## Target boundaries

| Boundary | Intended ownership | Migration status |
|---|---|---|
| `tui_clipboard.py` | selected-text priority and native clipboard command routing | extracted in this change |
| `tui_links.py` | HTTP(S) validation, repository-file containment, Markdown click handling | extracted in this change |
| `tui_status.py` | loading, runtime status, usage formatting, and metrics widgets | next low-risk extraction |
| `tui_types.py` / `tui_events.py` | request/configuration types plus event messages and narrow host protocols | extract before feature widgets |
| `tui_approvals.py` | policy plus modal/inline approval widgets and key behavior | move as one feature cluster |
| `tui_onboarding.py` | setup state, provider/model loading, credential UI | extract after approval |
| `tui_transcript.py` | composer, transcript scroll, message/timeline/tool widgets | extract after leaf services |
| `tui_projection.py` | conversion of runtime events into transcript/tool state | introduce as a service rather than an App mixin |
| `tui_commands.py` | command-menu choices and command dispatch independent of rendering | separate pure resolution from App effects |
| `tui_session_controller.py` | conversation lease, queue, compaction, resume, and resource lifecycle | final high-risk extraction |
| `tui.py` | compatibility imports and root composition | shrink last to preserve callers |

## Migration order and guardrails

1. Keep `looplane.tui` imports stable while extracting leaf modules. Re-export moved
   public names until downstream callers migrate.
2. Split `tests/test_tui.py` alongside production boundaries. Moving implementation
   without its focused tests would leave another monolith.
3. Extract pure policy and platform adapters before widgets. Pass narrow protocols or
   callbacks into services so new modules do not import `looplaneApp`.
4. Extract approval and onboarding widgets next. Their Textual decorators and focus
   behavior require targeted Pilot tests before and after each move.
5. Separate event projection and conversation orchestration only after their state
   inputs/outputs are explicit. Avoid inheritance-only mixin splits.
6. Preserve full-suite and real-PTY evidence boundaries. Textual Pilot verifies app
   state and messages; it does not prove every terminal supports OSC 52, native file
   opening, or mouse escape-sequence behavior.
7. Make clipboard results honest: a confirmed native command may say “Copied”; an
   unacknowledged OSC 52 handoff should say “Copy requested via terminal.” SSH/tmux
   routing belongs in the clipboard service rather than the App key handler.

## Acceptance criteria

- The root App no longer contains clipboard, link, approval-policy, or platform
  command details.
- Each extracted behavior has deterministic unit tests plus the necessary Textual
  integration test.
- `Ctrl+C` copies a non-empty composer or transcript selection first; without a
  selection, approval cancellation, run interruption, draft clearing, and confirmed
  exit retain their existing semantics.
- Clicked links open only complete HTTP(S) URLs or existing files whose resolved path
  remains inside the active repository. A `:line[:column]` suffix is accepted but the
  operating-system opener is not guaranteed to navigate to that line.
- The full lint and test gates remain green after every extraction slice.

## Out of scope for this fix

- Replacing Textual or reviving the pre-M11 Ask/Agent product split.
- Building an editor-specific URL protocol for exact line navigation.
- Rewriting the conversation controller while changing clipboard and link behavior.
- Claiming native clipboard or file opening works in every SSH, tmux, terminal, and
  desktop combination without a real environment matrix.

Clickable file references must be Markdown links, for example
`[README](README.md)` or `[implementation](src/looplane/tui.py:5015)`. Plain path
text remains selectable text; automatically linkifying every path-like token would
change transcript rendering and needs a separate parser contract.
