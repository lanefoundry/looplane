# TUI vs Claude Code gap analysis

## Goal

Explain, from current repository evidence, why looplane's TUI feels substantially different from Claude Code.

## Tasks

- [x] Map the current TUI rendering and interaction architecture.
- [x] Map the Claude/Codex conversation runtime and event flow used by the TUI.
- [x] Compare the implementation with the project's stated Claude Code reference/design.
- [x] Separate deliberate MVP scope from defects or missing integration.
- [x] Summarize the highest-leverage next fixes with file and line evidence.

## Status

Complete. This file records analysis only; no product code changes were made.

## Findings

1. M11 implemented a safe, correlated transcript and long-lived provider session, not Claude Code
   product parity. Its final review was scoped to protocol, lifecycle, approval, and workspace
   invariants.
2. The Textual shell is structurally different: permanent topbar/status/footer, a bottom-aligned
   sparse transcript, a single-line composer, and a modal approval sheet. These choices explain the
   most visible blank-space and chrome differences.
3. Transcript rendering remains generic and plain text. `MessageBlock` and `ToolActionBlock` have
   no Markdown, specialized command/edit/read renderer, grouping/collapse, rich diff view, or
   thinking/background-task presentation.
4. The composer is disabled during a turn. looplane therefore lacks Claude Code's multiline editing,
   queued follow-ups, command recall, and background-work interaction.
5. The Claude bridge deliberately exposes only Read/Glob/Grep/Bash/Edit/Write and disables Agent,
   Task, web, MCP, settings, and SDK session persistence. It ignores system/tool-progress/auth
   frames. This is a major capability and metadata reduction, not just a visual difference.
6. looplane runs in an audited disposable committed-HEAD clone and persists only user/assistant turn
   semantics. This safety design intentionally differs from Claude Code's current-worktree and
   native-session behavior.
7. Newline-gated streaming and whole-block `Static` replacement are implementation compromises,
   not required security boundaries.
8. Slash commands are a small looplane-owned hard-coded dispatcher. Every leading-slash input is
   intercepted before the runtime sees it, and only `/model`, `/runtime`, `/new`, `/resume`,
   `/clear`, `/history`, `/conversations`, `/status`, and `/help` are recognized. There is no
   discovery/autocomplete surface, and Claude Code commands such as compact/context/permissions,
   plan/review, and rewind are neither implemented nor forwarded.
