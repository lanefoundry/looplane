# M11 Claude Code TUI source reference

Date: 2026-08-22  
Scope: read-only audit of the checked-out `claude-code-source`; this is an implementation reference for PCA, not a claim that PCA should clone every Claude Code feature.

## Conclusion

Claude Code's REPL is not a conventional role-labelled chat and it does not have a separate, primary "activity log". Its main surface is a semantic transcript:

- a user prompt is a full-width contrasting row, without a literal `You` heading;
- assistant prose is unboxed Markdown introduced by a small dot, without a literal `Assistant` heading;
- tool invocation, progress, result, error, and diff stay adjacent in the transcript and are linked by `tool_use_id`;
- repetitive reads/searches and compatible tool calls are grouped or collapsed before rendering;
- a permission request is the active interaction at the transcript tail, while prior context remains visible and scrollable;
- the composer is always the session continuation point. Mode, tasks, permission state, remote state, and shortcut hints are compressed into its footer instead of occupying a permanent top status panel.

The highest-value PCA change is therefore not another visual restyle. It is to replace `MessageBlock(role, content)` plus generic `TimelineEntry("Activity", line)` with a typed transcript projection. Without that semantic projection, Claude-like grouping, inline tool results, diffs, and permission placement cannot be implemented reliably.

## Source-derived behavior

### 1. Message projection is a reducer, not a direct event dump

`Messages.tsx` performs this sequence before a row is mounted:

1. Normalize and remove empty messages.
2. Preserve full history in fullscreen; in the native-scrollback path, omit pre-compact messages already printed above the viewport.
3. Remove progress pseudo-messages and attachments that intentionally render `null`.
4. Reorder tool use/results into UI order and filter synthetic user records.
5. Optionally filter Brief-mode duplicate prose.
6. Apply tool-owned grouping.
7. Collapse read/search groups, teammate shutdowns, hook summaries, and background-bash notifications.
8. Build lookup maps over the normalized source so tool use, result, error, and progress remain correlated.
9. Render a virtualized list in fullscreen; otherwise use a stable UUID-anchored safety window. Streaming prose is a distinct final row until it becomes a finalized assistant message.

Evidence: `src/components/Messages.tsx:379-467`, `src/components/Messages.tsx:475-543`, `src/components/Messages.tsx:559-624`, `src/components/Messages.tsx:677-720`.

The `Message.tsx` dispatcher is typed by content, not merely by author: assistant text, thinking, redacted thinking, advisor content, and tool use have separate renderers; user text, image, and tool result have separate renderers; system, attachment, compact boundary, grouped tools, and collapsed read/search have separate paths. `MessageRow.tsx` derives the display message for a group, resolves progress via lookup, and only freezes a row once streaming and all sibling tool activity are resolved. Transcript-only metadata (time/model) is right-aligned above assistant text rather than repeated in every normal prompt row. Evidence: `src/components/Message.tsx:70-330`, `src/components/MessageRow.tsx:100-260`, `src/components/MessageModel.tsx:12-37`, `src/components/MessageTimestamp.tsx:12-49`.

Remote SDK events are also projected deliberately. Assistant and optional historical user messages become normal REPL messages; tool-result-shaped user records can be converted for local grouping; successful terminal `result` events are ignored as noise; auth, tool-use-summary, and rate-limit events are handled elsewhere or ignored; status/tool progress become informational system records. Live prompt echoes are ignored because the REPL has already inserted the user row. Evidence: `src/remote/sdkMessageAdapter.ts:145-275`.

Implication for PCA: a backend's `completed` result must not become a second assistant answer when a streamed/final message already exists. PCA already guards this per generation, but each external `message` is still mounted as an independent assistant block and the coarse `activity` event has no correlation identity.

### 2. User/assistant hierarchy is asymmetric

`UserPromptMessage` caps pathological prompts at 10,000 displayed characters, retaining a 2,500-character head and tail, and puts the prompt on `userMessageBackground`. The tail retention is intentional for piped input where the actual question is last. It has no `You` label. Evidence: `src/components/messages/UserPromptMessage.tsx:20-78`.

Normal assistant prose is an unboxed Markdown row with a two-cell dot gutter. Special strings are projected to compact error/status forms; API errors are capped unless verbose. It has no `Assistant` label. Evidence: `src/components/messages/AssistantTextMessage.tsx:47-260`.

Therefore the PCA P0 hierarchy should be:

```text
[contrasting full-width user prompt]
● assistant markdown
● Tool name (compact input summary)
  ⎿ progress/result/error
● next assistant markdown
```

Do not add chat bubbles, author avatars, or permanent `You` / `Assistant` headings as a supposed Claude Code match.

### 3. Tool grouping, collapse, and expansion

`AssistantToolUseMessage` resolves a tool definition, validates its input, asks the tool for a user-facing name/summary/tag, and derives queued/in-progress/waiting-for-permission/resolved/error state. A normal tool is one compact semantic row: loader/dot, bold name, optional `(summary)`, optional tag. Its subordinate progress/result uses the `⎿` response gutter. Transparent wrapper tools disappear when queued/resolved and only expose useful active progress. Evidence: `src/components/messages/AssistantToolUseMessage.tsx:35-260`, `src/components/MessageResponse.tsx:14-55`.

`GroupedToolUseContent` joins each use with its result by `tool_use_id`, supplies per-item resolved/error/in-progress/progress state, and delegates the actual grouped presentation to the tool. Animation is enabled only while at least one member is active. Evidence: `src/components/messages/GroupedToolUseContent.tsx:13-56`.

Collapsed read/search and truncated tool results are explicitly expandable. The expansion key is `tool_use_id` when possible, so invocation and result expand together, with UUID only as fallback. Evidence: `src/components/Messages.tsx:559-594`, `src/components/Messages.tsx:723-726`.

PCA currently turns every core projected event into `TimelineEntry("Activity", line)` in Agent mode (`src/coding_agent/tui.py:1265-1280`) and reduces external activity to `Working · <item_type>` (`src/coding_agent/tui.py:1283-1307`). That loses use/result identity, repeats generic headings, and cannot collapse consecutive related work.

### 4. Diff is attached to the edit

For an accepted edit result, `FileEditTool/UI.tsx` renders a file-aware updated message with a structured patch. For a rejected edit, it still renders an edit preview: new files show content; existing files reconstruct a bounded-context patch, falling back to the tool inputs when the file context cannot be read. Evidence: `src/tools/FileEditTool/UI.tsx:57-153`, `src/tools/FileEditTool/UI.tsx:155-280`.

The file permission request embeds that diff/content between the request title and choices. It can delegate to an IDE diff, shows symlink/outside-workspace warnings, then offers accept-once, session-scoped acceptance, and reject. Evidence: `src/components/permissions/FilePermissionDialog/FilePermissionDialog.tsx:140-202`, `src/components/permissions/FilePermissionDialog/permissionOptions.tsx:47-176`.

PCA instead waits for the terminal `RunResult`, reads one patch artifact, and appends a standalone `TimelineEntry("Diff", preview)` (`src/coding_agent/tui.py:1003-1028`). That is useful as a run summary but is not an inline edit projection and cannot show the proposed diff at approval time unless `ApprovalRequest.preview` already contains it.

### 5. Permission placement and interaction

Claude Code chooses exactly one focused input dialog by priority. The current source appends `toolPermissionOverlay` after `Messages` inside the same `ScrollBox`; it is not passed through `FullscreenLayout`'s generic bottom slot. Other interactive requests may occupy that bottom slot. The normal prompt is withheld while a focused dialog is active. Permission appearance/dismissal explicitly re-pins the scroll so the blocking request cannot be hidden. If the user is actively typing, interrupt dialogs may be temporarily suppressed and the composer shows `Waiting for permission…`. Evidence: `src/screens/REPL.tsx:2011-2105`, `src/screens/REPL.tsx:4518-4593`, `src/components/FullscreenLayout.tsx:31-38`, `src/components/FullscreenLayout.tsx:359-362`, `src/components/PromptInput/PromptInput.tsx:2243-2275`.

The shared permission shell is only a top rounded rule plus title/content, not a centered full-screen card. Choices are keyboard-first; Escape cancels and Tab can amend accept/reject with instructions. Bash embeds the full command plus sandbox/classifier/destructive context; file edits embed the diff. Evidence: `src/components/permissions/PermissionDialog.tsx:17-65`, `src/components/permissions/PermissionPrompt.tsx:45-320`, `src/components/permissions/BashPermissionRequest/BashPermissionRequest.tsx:320-480`.

PCA now retains `ModalScreen`'s blocking semantics but renders a content-height, bottom-adjacent terminal choice surface: one top rule, a bounded literal preview, and only the available decisions in a vertical numbered `OptionList`. Its decision semantics remain correct, including process-lifetime external-runtime session grants. The remaining structural difference is that Claude owns the permission as the last item in the transcript scroll flow, while PCA overlays that boundary above the composer.

### 6. Spinner and background tasks

Claude Code does not fill idle space with log text. A single spinner/verb row appears near the end of the transcript when the query, external loading, teammates, or queued task notifications are active. It is hidden when streaming text itself is feedback, when only Sleep is active, or while an approval owns attention. Evidence: `src/screens/REPL.tsx:1654-1689`, `src/screens/REPL.tsx:4584-4589`.

Bash progress is tool-local (`Running…` or shell output/elapsed/line/byte/task metadata). The Bash UI offers a contextual `ctrl+b` background hint; running foreground commands can move to background. Background tasks then surface as a compact footer pill/status and an optional management dialog, not a permanent verbose pane. Evidence: `src/tools/BashTool/UI.tsx:29-152`, `src/components/PromptInput/PromptInputFooterLeftSide.tsx:277-408`.

PCA has a two-row global status plus a hidden seven-row detail log. The hidden detail layer is a reasonable diagnostic escape hatch, but current Agent mode duplicates virtually all activity into the primary transcript. The primary surface should instead contain only semantic tool rows, current progress, meaningful errors, checks, and final diff/summary.

### 7. Composer and session continuity

The composer is a multiline bottom input with a light top/bottom boundary, an inline mode indicator, and a stable one-row footer. The input owns history navigation, queued-command recall/edit, paste IDs that survive resume, modal-aware focus, and inline model/mode pickers. The footer shows non-default permission mode, background tasks/teammates, remote state, and context-sensitive shortcuts, truncating secondary content on narrow terminals. Evidence: `src/components/PromptInput/PromptInput.tsx:188-380`, `src/components/PromptInput/PromptInput.tsx:907-1105`, `src/components/PromptInput/PromptInput.tsx:2140-2300`, `src/components/PromptInput/PromptInputFooter.tsx:112-151`, `src/components/PromptInput/PromptInputFooterLeftSide.tsx:317-479`.

The REPL keeps the message array across turns, restores read/bash state from resumed messages, keeps the draft while message actions are open, and uses a conversation ID only to invalidate rendering keys after real resets/rewinds. It also exposes message rewind/fork/summarize separately from code rewind. Evidence: `src/screens/REPL.tsx:1960-2000`, `src/screens/REPL.tsx:4492-4519`, `src/components/MessageSelector.tsx:46-256`, `src/components/MessageSelector.tsx:314-410`.

PCA now preserves a visible multi-turn Ask transcript and durable `/resume`, which is directionally correct. Remaining differences are material:

- `Input` is single-line while Claude's prompt is multiline and independently scrollable.
- PCA disables the input for the entire run (`src/coding_agent/tui.py:1249-1263`), so it cannot queue a follow-up while work continues.
- changing Ask/Agent releases the conversation and clears history/transcript (`src/coding_agent/tui.py:698-708`); changing runtime/model does the same. This is a hard session break, whereas Claude treats modes/model as session controls.
- PCA's composer is six rows with three permanent buttons plus the app Footer; Claude's normal path is the input boundary plus a stable compact status/hint line.

## PCA structural delta

| Concern | Current PCA | Claude-derived target |
|---|---|---|
| Primary data model | role block or generic timeline string | typed transcript item with stable ID and parent/correlation ID |
| User prompt | `You`/`Task` heading + body | contrasting full-width prompt row, no role heading |
| Assistant | `Assistant`/`Agent` heading + body | dot gutter + Markdown, no box/role heading |
| Tool activity | every core event becomes `Activity`; external becomes `Working` | one tool row with state; subordinate progress/result/error |
| Repetition | no grouping | group compatible consecutive tools; collapse read/search/noise |
| Diff | terminal run-level patch preview | proposed diff inside approval; applied diff attached to edit; optional run aggregate |
| Permission | compact bottom-adjacent modal with vertical numbered choices | transcript-tail overlay in the shared scroll flow |
| Progress | global status text plus duplicated timeline | one current spinner/verb plus tool-local progress |
| Details | hidden `RichLog` | keep for diagnostics only; do not mirror every event into transcript |
| Composer | single-line + Select + buttons + Footer | multiline input, compact mode/model/status footer, keyboard-first |
| Continuation | Ask persists; mode/config reset | explicit `/new`/rewind resets; ordinary turns and safe control changes preserve transcript |

## P0 implementation plan

### P0.1 Introduce a typed transcript projection

Add an internal immutable item union (names illustrative):

```python
UserPromptItem(id, text)
AssistantTextItem(id, text, streaming=False)
ToolUseItem(id, name, summary, state, parent_id=None)
ToolResultItem(id, tool_use_id, text, error=False, truncated=False)
DiffItem(id, tool_use_id, path, patch, state)
NoticeItem(id, level, text)
CheckItem(id, name, ok, exit_code)
```

Create one reducer owned by `PcaApp` that consumes `RunEvent`, `ExternalAgentEvent`, and final `RunResult`. The reducer must update an existing item by stable ID instead of appending a new row for every phase. Keep `RichLog` as raw diagnostics behind `Details`.

This requires extending `ExternalAgentEvent.data` (or adding a richer event type) with stable `item_id`, phase, semantic kind, and optional parent/tool ID. The current Codex adapter only exposes `item_type` and phase; Claude backend messages similarly need normalized identities. Until a backend provides IDs, conservatively show one compact opaque activity row rather than claiming tool/result grouping.

### P0.2 Replace the transcript widgets, without adding chat chrome

- Replace `MessageBlock` with `UserPromptBlock` and `AssistantTextBlock`.
- User row: background, one-cell horizontal padding, no `You`/`Task` label, display cap with head+tail retention.
- Assistant row: two-cell dot gutter and Markdown-capable body, no `Assistant`/`Agent` label.
- Add `ToolUseBlock` with nested response gutter and state-specific loader/marker.
- Add `ToolGroupBlock`/collapse counter only after stable correlation exists.
- Stop calling `_write_timeline("Activity", line)` for every event.
- Continue auto-follow only while already at the bottom; if the user scrolls upward, preserve their position and show a `N new` affordance.

### P0.3 Keep approval adjacent to the pending action

Keep the existing policy and decisions unchanged. The presentation now provides one active compact approval surface:

- title/effect and exact target;
- command or diff preview in a vertically bounded scroll region;
- vertical options at every width, arrow/Enter and number-key selection, and a visible `›` focus pointer;
- `Allow once`, narrowly scoped `Allow for session`, `Deny`, `Cancel run`;
- Escape remains cancel, and no approval choice is activated by an unrelated global binding;
- restore focus to the unchanged draft after dismissal.

This closes the oversized horizontal-button UI shown in the live Warp capture. A later structural change may move the same renderer into the transcript scroll flow; that should not reopen the approval policy or backend-scoped session-grant boundary.

The policy invariant remains: session grants are only keyed for `external_agent:<backend>` and never persisted; core edit/execute approvals must still prompt.

### P0.4 Compact the composer, preserve real continuity

- Replace single-line `Input` with a multiline TextArea-style composer with a bounded viewport.
- Put mode and runtime/model identity into one compact footer line; open pickers with Ctrl+M/Ctrl+L rather than keeping large permanent buttons.
- Keep one visible send/stop action only if mouse discoverability is required; keyboard remains primary.
- Do not clear the transcript just because Ask/Agent changes. If the security/runtime contract requires a new context, append a visible boundary notice and start a new backend context while retaining UI history.
- Preserve draft, cursor/focus, and scroll on resize, permission close, details toggle, and runtime/model picker cancel.

### P0.5 Inline meaningful run products

- At modification approval, render `ApprovalRequest.preview` as a diff when it is patch-shaped; otherwise render safe plain text.
- At completion, attach verification rows and patch summary to the corresponding Agent turn. Keep the aggregate patch artifact accessible in Details.
- Never interpret backend text as Textual/Rich markup.

## Pilot acceptance tests

Tests should assert structure and state, not ANSI colors.

1. **Hierarchy, no role headers**: submit Ask, emit one assistant answer, assert one `UserPromptBlock` and one `AssistantTextBlock`; assert visible transcript contains the prompt/answer but no standalone `You`, `Assistant`, `Task`, or `Agent` heading.
2. **No generic activity flood**: emit requested/started/progress/completed for one core tool ID; assert one `ToolUseBlock` changes state in place and there are not four `TimelineEntry("Activity")` rows.
3. **Tool-result correlation**: emit two interleaved tool IDs and their results; assert each result is under its matching tool, not merely arrival order.
4. **Collapse**: emit consecutive compatible read/search uses; assert a single collapsed group and count; expand via keyboard and assert members appear; collapse again without losing focus.
5. **External fallback**: emit external activity without an item ID; assert one opaque `Working` row is updated/throttled, not incorrectly joined to a later unrelated item.
6. **Stream-to-final continuity**: stream assistant chunks, then final result summary; assert one assistant row contains the completed text and no duplicate summary.
7. **Inline diff approval**: request MODIFY with a patch preview; assert the bottom approval panel contains target + diff + choices while transcript remains mounted and scrollable.
8. **Approval focus/draft**: type a multiline draft, open approval, choose Allow once, and assert the draft, cursor/focus target, transcript items, and scroll position survive.
9. **Session grant scope**: Allow for session on external backend A; next bounded task on A does not prompt; backend B and a core `replace_text` request still prompt. This preserves the existing security regression coverage.
10. **Narrow approval**: at 60x22, assert options stack vertically, preview is scrollable, no control is clipped, and focused choice has an accessible label.
11. **Wide transcript**: at 160x36, assert transcript uses available width without a fixed chat column and the composer footer stays one row.
12. **Resize continuity**: enter a multiline draft, scroll above bottom, resize wide→narrow→wide; assert draft/cursor/focus and selected/expanded tool state survive and auto-follow is still disabled.
13. **Scroll follow**: at bottom, append progress and answer and assert viewport follows; scroll upward, append two items, assert position is unchanged and `2 new` appears; activate it and assert bottom is reached.
14. **Permission/status precedence**: while a tool spinner is active, open approval; assert the approval owns focus and the global spinner does not compete; dismiss and assert current activity resumes.
15. **Background task**: background a running command; assert foreground progress becomes a compact task indicator and composer remains usable; opening task management does not clear the transcript or draft.
16. **Mode boundary without visual erasure**: switch Ask→Agent while idle; assert existing transcript remains, a semantic boundary notice is appended if a new backend context is required, and Ask history is not silently presented to Agent as repository context.
17. **Explicit reset**: `/new` and `/clear` reset the intended backend/durable state and visible transcript; ordinary second/third turns do not.
18. **Resume**: resume a stored Ask conversation; assert user/assistant rows project in original order, no duplicate live prompt echo appears, and the next answer appends to the same visible session.
19. **Keyboard**: Tab/Shift+Tab navigation, Enter submission/selection, Escape cancel, Ctrl+C stop, Ctrl+M mode, and Ctrl+L runtime/model remain reachable with no hidden focused widget.
20. **Plain-text safety**: backend text containing Rich/Textual markup-like sequences renders literally in assistant, tool result, diff fallback, status, and permission preview.

## Security and scope invariants

- This work is presentation/projection only. Do not bypass `TextualApprovalPolicy`, external Agent clean-repository checks, isolated workspace preparation, patch acceptance, or verification.
- Ask remains read-only and must not gain repository context because the transcript persists visually.
- `Allow for session` remains process-local and backend-scoped; no config persistence.
- A visual collapse must never discard source events needed for audit/details or durable conversation completion.
- Unknown/unsupported external events are rendered conservatively or logged; never infer an edit, success, or approval from a provider label or item name.

## Files reviewed

- `src/screens/REPL.tsx`
- `src/components/Messages.tsx`
- `src/components/Message.tsx`, `MessageRow.tsx`, `MessageModel.tsx`, `MessageResponse.tsx`, `MessageSelector.tsx`, `MessageTimestamp.tsx`
- `src/components/VirtualMessageList.tsx`
- `src/components/messages/UserPromptMessage.tsx`, `AssistantTextMessage.tsx`, `AssistantToolUseMessage.tsx`, `GroupedToolUseContent.tsx`
- `src/components/PromptInput/PromptInput.tsx`, `PromptInputFooter.tsx`, `PromptInputFooterLeftSide.tsx`, `PromptInputFooterSuggestions.tsx`
- permission dispatch/shell/prompt plus Bash and file-edit permission paths under `src/components/permissions/`
- `src/tools/FileEditTool/UI.tsx`, `src/tools/BashTool/UI.tsx`
- `src/remote/sdkMessageAdapter.ts`
- PCA comparison: `src/coding_agent/tui.py`, `tests/test_tui.py`
