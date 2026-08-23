# M11 UI direction: one agent conversation

## Subject and job

PCA is a terminal coding agent for a developer working inside one repository. The
screen's single job is to keep one continuous conversation understandable while
the runtime thinks, reads, edits, runs commands, asks permission, and verifies.

## Source-derived interaction model

Reference: the local `claude-code-source`, especially `REPL.tsx`, `Messages.tsx`,
`MessageRow.tsx`, `AssistantToolUseMessage.tsx`, per-tool `UI.tsx`,
`PermissionRequest.tsx`, and `PromptInput.tsx`.

- The source of truth is a typed message stream, not a text log.
- User prompts, assistant text, tool use, tool result, progress, and permission
  are distinct records.
- Tool use and result are joined by action/tool-use ID.
- Read/search work is grouped and collapsible; edits own a diff renderer;
  commands own an output renderer.
- Permission is attached to the pending tool. It is not a separate application
  mode.
- Success terminal frames do not become duplicate assistant messages.
- One prompt input persists throughout the session; mode is a permission policy,
  not an Ask/Agent conversation switch.

## Visual system

- Canvas: terminal-native near-black inherited from Textual theme.
- Text: default terminal foreground; muted metadata uses theme-muted.
- Accent: one warm amber for pending/active work and permission boundaries.
- Success/error use semantic theme colors and always include words/symbols.
- Typography: terminal monospace only; hierarchy comes from weight, indentation,
  glyphs, and whitespace rather than panels.

## Layout

```text
PCA · runtime/model                                      repository

> user prompt

● Read src/foo.py
  └ 84 lines
● Search "AgentRunner"
  └ 6 matches
● Update src/foo.py
  ┌ diff
  │ - old
  │ + new
  └
  Permission required: edit src/foo.py

assistant response...

────────────────────────────────────────────────────────────────
> message composer
  model · permission policy · context                     shortcuts
```

There is no centered card, no giant framed activity panel, and no visible
Ask/Agent selector. Full screen is intentional; the conversation column uses the
terminal width. Details are grouped under the action that produced them.

## Signature

The signature is the executable transcript: every side effect becomes a durable
inline action node that changes state in place from queued to active to approved
or denied to completed, with its own bounded result renderer.

## Self-critique

The prior design used generic chat roles plus a secondary activity drawer. That
could fit any chatbot and hid the defining coding-agent behavior. The revision
spends its complexity on typed action nodes and keeps all surrounding chrome
quiet. It follows Claude Code's information architecture without copying its
React/Ink implementation or pretending vendor protocol frames are UI models.
