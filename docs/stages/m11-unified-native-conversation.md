# M11: Unified native conversation

> Status: implementation and independent review complete; article review and commit pending.

## Scope

Replace the visible `Ask | Agent` split and one-shot `codex exec` / `claude -p` illusion with one
continuous coding conversation. Reading, answering, editing, and command execution now share one
transcript. Side effects pause at their actual tool boundary for approval.

## Why M10 was not enough

M10 made the subscription runtimes reachable, but it still launched a new child for every prompt
and rendered normalized protocol records inside a generic activity canvas. That produced three
user-visible failures:

- the previous answer was replayed as hidden prompt text instead of being owned by a live runtime
  session;
- terminal `system`, `message`, and `result` records could appear as duplicated prose;
- a casual question and a coding request used separate modes even though Claude Code and Codex
  treat them as turns in one agent session.

The source-level Claude Code comparison confirmed that its main surface is a semantic transcript,
not a role-labelled chat or a permanent activity log. User prompts are contrasting rows, assistant
text is unboxed, each tool is one correlated row updated in place, diffs belong to edits, and the
active permission surface is docked at the bottom.

## Architecture

```text
Textual transcript
      |
      v
ConversationController ---- strict ConversationRuntimeEvent union
      |                                  |
      +-- Codex app-server JSON-RPC -----+
      |      one child, one thread, many turns
      |
      +-- Claude Agent SDK sidecar ------+
             one query session, many turns

Both runtimes -> ConversationWorkspace
                 committed-HEAD disposable clone
                 isolated Git control + bounded patch audit
```

`ConversationController` owns a long-lived provider-neutral runtime session. Each turn streams
typed text, tool, approval, and terminal events. Vendor thread/session/tool identifiers remain
inside the adapter. Rivumi-generated turn/action/approval identifiers are the only correlation values
that reach the renderer and durable conversation store.

Codex uses the installed `codex app-server`, initializes one thread in `workspace-write`, and
routes file-change/command approval requests through Rivumi. Its local MCP configuration is reduced
to Groundlane only; Rivumi explicitly disables every other configured MCP server and forwards only
Groundlane's configured bearer-token environment variable. Claude uses the installed official Agent
SDK through a pinned Node sidecar with `settingSources: []`, no MCP, no Agent/Task delegation, and
PreToolUse/canUseTool approval correlation. Unknown tools and protocol frames fail closed.

## Workspace and patch boundary

The native session never edits the user's worktree directly. `ConversationWorkspace` clones exact
committed `HEAD`, removes origin, isolates Git metadata, disables hooks/fsmonitor, and retains one
workspace across conversation turns. Dirty source files are deliberately excluded and surfaced as
a warning boundary. Once created, that clone is independent of concurrent source-worktree changes.

At every terminal event Rivumi re-audits the bounded patch and allowed paths. Codex file-change events
must exactly account for the actual patch. Claude's SDK does not provide a reliable complete diff,
so Rivumi recomputes it from the isolated Git workspace and attaches that audited diff to the typed
edit row. Cleanup removes only the disposable workspace and cannot retroactively fail a completed
turn because an unrelated source file changed.

## Transcript projection

- user prompt: full-width contrasting row, no literal `You` heading;
- assistant: dot gutter and one streaming text block, no `Assistant` heading;
- tool: one stable row updated from started to waiting/completed/failed;
- tool output and diff: subordinate detail on that tool, not a new generic activity message;
- permission: bottom-docked interaction while the transcript remains visible;
- diagnostics: retained behind `Details`, not mirrored into the primary transcript;
- conversation: ordinary turns share the same native child/session and the same disposable
  workspace; `/new` clears the conversation. A Claude/Codex runtime or model change keeps the
  Rivumi-owned transcript, records a context boundary, starts a new native session, and replays the
  bounded completed-turn history once.

There is no longer a user-facing Ask/Agent decision. A message remains ordinary conversation until
the model requests a side effect; the actual Edit, Write, Bash, or Codex approval request is the
security boundary.

## Persistence and resume

Rivumi's separate `ConversationStore` remains canonical across application restarts. It stores only a
strict user/assistant turn schema in 0600 files under a 0700 state directory. Vendor session IDs,
credentials, raw stderr, and opaque provider metadata are not persisted. A live process uses the
native session directly; after restart, Rivumi opens a fresh native session and supplies bounded,
completed-turn semantic replay once.

## Verification

- `uv run pytest -q`: 352 passed.
- `uv run ruff check .`: passed.
- `uv lock --check`: passed.
- `git diff --check`: passed.
- `uv build`: source distribution and wheel built successfully.
- Real installed Codex app-server smoke returned `PCA_SMOKE_OK` through the live long-lived
  adapter; this is a historical pre-rename marker, and no credential value was read or printed.
- Before the Rivumi rename, the former editable `pca` command was refreshed through
  `scripts/install-dev-cli`; its isolated dependency environment passed `uv pip check` plus
  `pca --help`.

Independent review evidence and protocol/lifecycle repros are recorded in
`.research/m11-release-review.md`.

## Artifacts

- Claude Code source audit: `.research/m11-claude-code-tui-reference.md`
- Design: `.work/m11-claude-tui-design.md`
- Plan: `.work/m11-conversation-tui-plan.md`
- Draft article:
  `quidproquo/src/content/posts/ai/2026-08-22-coding-agent-native-conversation-tui.md`

## Commit

Pending user review and formatted commit confirmation.
