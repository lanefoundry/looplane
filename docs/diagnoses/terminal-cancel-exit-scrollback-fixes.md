# Terminal cancel, exit, rewind, and scrollback fixes

## Outcome

Match Claude Code's terminal lifecycle without conflating four different operations:

1. interrupting an active turn;
2. rewinding a conversation;
3. exiting looplane;
4. preserving finalized transcript output in terminal scrollback.

Conversation persistence on disk and visible terminal scrollback are separate guarantees and must be
tested separately.

## Current defects

### 1. Second Escape can exit looplane

`looplaneApp.action_handle_escape()` interrupts while `_agent_running` is true, but its idle fallback
immediately calls `exit()`. If the first Escape finishes cancellation quickly, the second Escape lands
on the idle fallback and closes the coding agent.

This behavior is currently encoded in TUI tests, so both implementation and tests must change.

### 2. Empty double Escape does not open rewind

looplane has prompt-history text recall, but no idle 800 ms double-Escape detector and no durable
conversation rewind/fork workflow. Prompt history must not be presented as conversation rewind.

### 3. Exit restores an empty primary terminal buffer

The CLI calls `tui_app.run()` with Textual's default `inline=False`. Textual enters DEC alternate
screen (`1049h`) and leaves it on exit (`1049l`), so everything drawn by looplane disappears from the
terminal's main-buffer scrollback.

Conversation events may still exist under `~/.local/state/looplane/conversations/`; that does not make
the transcript visible after exit.

## Required interaction contract

### Escape

- Approval or selector open: Escape dismisses/cancels that foreground interaction first.
- Active turn: one Escape cooperatively interrupts the current response/tool operation.
- Completing an interrupt must never arm or trigger application exit.
- Idle, empty composer: one Escape does nothing visible; a second idle Escape within 800 ms opens
  rewind when rewindable prompts exist.
- The Escape used to cancel an active turn must not count as the first press of the later idle
  double-Escape sequence.
- Idle Escape must never close looplane.

### Ctrl+C and Ctrl+D

- Active turn: Ctrl+C interrupts the turn.
- Idle with draft text: first Ctrl+C clears the draft and keeps looplane open.
- Idle with an empty composer: first Ctrl+C shows `Press Ctrl-C again to exit`; the second press within
  800 ms exits.
- Ctrl+D follows the same double-press exit confirmation when the composer is empty.
- `/exit` remains the explicit immediate exit command when no approval decision is pending.

### Rewind

- `/rewind` and idle-empty double Escape open the same keyboard-first selector.
- Selecting a submitted prompt forks the conversation at the point immediately before that prompt.
- The selected prompt is restored into the composer and is not automatically submitted.
- Conversation-only rewind keeps the disposable workspace unchanged.
- Code rewind must not be exposed until real per-prompt workspace checkpoints exist.

## Scrollback preservation

### Preferred first implementation

Keep the full-screen alternate-screen UI while looplane is running. After `looplaneApp.run()` returns,
render an app-owned, bounded semantic transcript into the primary terminal buffer.

The export must include finalized:

- user prompts;
- assistant messages;
- tool/action titles and terminal states;
- bounded tool details or diffs;
- high-level timeline notices needed to understand the outcome;
- conversation ID and a copyable `/resume` command.

It must exclude:

- transient spinners and progress animation frames;
- empty viewport space;
- composer chrome, menus, selectors, and temporary permission prompts;
- secrets, raw credentials, hidden provider metadata, and unbounded tool output.

The semantic transcript must come from an explicit app-owned accumulator/reducer. The currently
visible Textual compositor is not a complete history because earlier rows may have scrolled out of the
viewport.

### Short-term fallback that is not sufficient by itself

`tui_app.run(inline=True, inline_no_clear=True)` can retain the final visible compositor on supported
POSIX terminals, but it is not the final solution:

- it retains only the visible viewport, not guaranteed complete semantic history;
- it may freeze empty space and TUI chrome into scrollback;
- Textual's inline driver selection is platform-dependent.

### Full Claude-style live scrollback parity

If finalized rows must enter terminal scrollback during the session, replace the full-screen renderer
with normal-buffer append-only transcript output plus a small redrawable composer/selector region.
Treat this as a larger renderer architecture change, not a driver flag.

## Implementation sequence

- [ ] Replace the current Escape idle-exit fallback with an explicit input-state machine.
- [ ] Add independent 800 ms detectors for rewind and confirmed exit; reset them across active-turn
      cancellation and foreground dialogs.
- [ ] Add `/rewind` conversation forking and prompt restoration before enabling double-Escape rewind.
- [ ] Introduce a provider-neutral semantic transcript export model populated by the same reducer that
      drives the TUI.
- [ ] Print the bounded final transcript after Textual returns to the primary terminal buffer.
- [ ] Add live normal-buffer rendering only if final-export parity is insufficient.
- [ ] Update README language that currently promises full-screen/alternate-screen behavior.

## Acceptance tests

### Input-state tests

- [ ] Active Escape requests cancellation exactly once and never exits.
- [ ] A second Escape immediately after cancellation does not exit.
- [ ] Idle single Escape does not exit or mutate conversation/draft state.
- [ ] Idle empty double Escape opens rewind only within the configured window.
- [ ] Escape from selector/approval dismisses only that foreground interaction.
- [ ] Idle Ctrl+C and Ctrl+D require a confirmed second press to exit.
- [ ] Ctrl+C with a draft clears it first; `/exit` remains explicit.

### Rewind tests

- [ ] Rewind selects persisted prompts by `turn_id`, including duplicate prompt text.
- [ ] The new branch excludes the selected turn and everything after it.
- [ ] The parent conversation remains byte-for-byte unchanged.
- [ ] The selected multiline prompt returns to the composer without automatic submission.
- [ ] The provider session is recreated from the branch prefix and ignores late events from the old
      generation.

### Terminal integration tests

- [ ] PTY test proves a normal exit returns control to the primary terminal.
- [ ] Post-exit output contains finalized user and assistant text plus a resume command.
- [ ] Long conversations export bounded semantic history even when early rows were outside the final
      viewport.
- [ ] Output does not contain alternate-screen-only empty space, spinner frames, composer chrome, or
      secrets.
- [ ] Cancelled, failed, and successful exits all leave a useful bounded transcript.

## Evidence

- looplane launch: `src/looplane/cli.py` constructs `looplaneApp` and calls `tui_app.run()` with default
  driver settings.
- Escape behavior: `src/looplane/tui.py` currently exits from the idle branch of
  `action_handle_escape()`.
- Durable storage: `src/looplane/conversation.py` stores conversation artifacts under the XDG state
  directory by default.
- Existing headless Textual tests cannot observe terminal escape sequences; a real PTY test is
  required.

## Status

Implemented (2026-08-23):

- Input state machine in `looplaneApp`: Escape never exits; idle Escape arms an
  800 ms double-press rewind detector; Ctrl+C/Ctrl+D/Ctrl+Q require a confirmed
  second press when idle and clear a draft first. Detectors reset across
  active-turn cancellation, foreground dialogs, and turn boundaries.
- `/rewind` added to the slash registry. `ConversationStore.fork_before_turn()`
  forks by `turn_id` into a new conversation (parent untouched byte-for-byte);
  the TUI opens a keyboard-first selector via `/rewind` or idle double-Escape,
  restores the prompt into the composer without submitting, and recreates the
  provider session from the branch prefix (`_native_session_has_context=False`).
- `looplane/transcript_export.py` adds the app-owned semantic transcript reducer.
  It is fed by `_write_turn`, `_write_timeline`, runtime tool/turn-completion
  handlers, external message events, and cancelled-turn notices, resets with
  `_reset_transcript`, snapshots at `exit()` (`final_transcript_text`), and the
  CLI prints it after Textual returns so history survives in scrollback.
- PTY integration tests (`tests/test_tui_pty.py`) prove alternate-screen exit,
  primary-buffer transcript output, resume command presence, and bounded export;
  README interactive section updated for the new key contract.
