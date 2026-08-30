# looplane documentation index

All project documents live under `docs/`. Root-level `progress.md` is a stub pointing here.

- [progress.md](progress.md) — rolling development progress, milestone status, acceptance criteria, and security invariants.
- [agent-diff-report.md](agent-diff-report.md) — agent-capability gap report against Claude Code's architecture.
- [startup-performance-playbook.md](startup-performance-playbook.md) — measured startup-performance playbook and budgets.
- [sdk.md](sdk.md) — stable Python SDK facade, WebSocket attach, replay/fork API, role lanes, and policy boundaries.
- [opencode-zen-protocol-mismatch.md](opencode-zen-protocol-mismatch.md) — diagnosis of the OpenCode Zen protocol mismatch.

## plans/

Implementation plans migrated from the former `.work/` and `.agent-work/` scratch areas. One document per initiative: milestone plans (`m11-conversation-tui-plan.md`, `m12-startup-performance-plan.md`, `m13-external-coding-cli-adapters-plan.md`), runtime work (`runtime-onboarding-plan.md`, `runtime-parity-plan.md`, `tui-parity-implementation-plan.md`), feature/UX initiatives (`ask-agent-mode-plan.md`, `loading-copy-plan.md`, `approval-context-plan.md`, `patchotter-spinner-plan.md`), naming/branding plans (`clean-brand-name-plan.md`, `short-euphonic-name-plan.md`, `looplane-project-rename-plan.md`, `ottie-otti-clearance-plan.md`), and audits/designs (`capability-current-state-audit-plan.md`, `m11-claude-tui-design.md`, `claude-code-ui-research-plan.md`, `uv-tool-sync-plan.md`). 18 files total.

## diagnoses/

Post-hoc diagnoses, reports, fix notes, and investigations from `.work/` and `.agent-work/`.

- Milestone reports and scheduling: `m13-stage-report.md`, `milestone-reschedule.md`, `summary.md`.
- Runtime/TUI diagnosis: `nim-500-diagnosis.md`, `tui-live-smoke-report.md`, `model-switch-conversation-diagnosis.md`, `run-fail-diagnosis.md`, `screenshot-failure-analysis.md`, `textual-ime-placeholder-investigation.md`, `warp-ui-investigation.md`.
- Fix notes: `codex-subagent-activity-fix.md`, `composer-bottom-fix.md`, `contextual-command-menu-fix.md`, `groundlane-codex-child-env-fix.md`, `runtime-reported-model-fix.md`, `source-invariant-fix.md`, `terminal-cancel-exit-scrollback-fixes.md`, `terminal-failure-repair.md`, `inline-selector-controls.md`.
- Investigations: `approval-scope-diagnosis.md`, `conversational-turn-redesign.md`, `claude-code-source-invariant-research.md`, `claude-rewind-behavior-audit.md`, `cloudflare-looplane-resource-migration.md`, `otter-animation-cadence.md`, `source-filesystem-changed-investigation.md`, `tui-vs-claude-code-gap-analysis.md`.
- `agent-diff/` — six-part capability diff detail (`a1-a8.md`, `a9-a16.md`, `a17-a23.md`, `b1-b5.md`, `b6-b10.md`, `c1-c6.md`) supporting [../agent-diff-report.md](../agent-diff-report.md).

27 files plus the `agent-diff/` subdirectory.

## research/

Research notes migrated from `.research/`: release reviews per milestone (`m2-release-review.md` … `m11-release-review.md`), live evidence records (`m3-live-eval-evidence.md`, `m4-live-evidence.md`, `m5-live-evidence.md`, `m6-live-evidence.md` — raw bundles remain in `.research/evidence/`), provider/backend design studies (`m5-claude-coding-backend-design.md`, `codex-oauth-implementation.md`, `provider-bridge-comparison.md`, `subscription-bridges.md`, `ccswitch-architecture.md`), CLI/TUI reference audits (`interactive-cli-audit.md`, `coding-cli-landscape.md`, `m11-claude-code-tui-reference.md`), and project-naming screens with date-prefixed files (`2026-08-22-*.md`). 64 files total.

## stages/

Per-milestone stage documents with implementation evidence and release criteria: `m1-local-harness.md` through `m11-unified-native-conversation.md`. See [stages/README.md](stages/README.md).

## product/

Product-facing documents; see [product/onboarding](product/onboarding).
