# TUI parity implementation plan

## Outcome

Make looplane's conversation surface feel like a complete coding-agent REPL: discoverable slash
commands, a multiline composer usable during work, semantic tool/diff rendering, collapsible noise,
and predictable scroll-follow behavior. Preserve approval and isolated-workspace safeguards.

## Design contract

- Subject: a developer operating one repository through one continuous agent conversation.
- Single job: keep the current reasoning, actions, approvals, and next input understandable.
- Layout: executable transcript first; quiet runtime identity; one persistent multiline composer.
- Signature: each tool is one stable action node whose state and bounded result update in place.
- Motion: only the active action/loading marker animates; reduced/no-animation modes remain valid.
- Safety: slash commands never bypass approval, workspace audit, runtime fencing, or bounded replay.

## Workstreams

- [x] Command registry and `/` discovery, including compact/context/permissions/model/new/help.
- [x] Multiline composer, history, queued follow-up, and run-time input availability.
- [x] Markdown assistant rendering and specialized read/search/command/edit/diff details.
- [x] Read/search collapse and keyboard expansion.
- [x] Bottom-follow only while pinned; show a new-items affordance when scrolled up.
- [x] Focus, resize, permission, plain-text safety, and narrow/wide regression tests.
- [x] Deterministic screenshots and full lint/test/build verification.

## Explicit non-goals

- Do not enable MCP, web tools, subagents, plugins, or unsafe current-worktree writes merely to
  imitate Claude Code. Those capability changes require a separate security design.
- Do not forward unknown slash commands blindly into a provider prompt.

## Status

Complete. Full tests, lint, lock validation, diff hygiene, screenshots, and package build pass.
