# Runtime-first TUI onboarding

## Outcome

Make daily `looplane` onboarding distinguish execution runtime, authentication source, and model.
Local Claude/Codex subscriptions must remain delegated external backends; API/local providers
remain the looplane AgentRunner path.

## Work

- [x] Define persisted non-secret runtime/model configuration without breaking existing config.
- [x] Replace provider-first blocking modal with runtime-first choices and Automatic model support.
- [x] Add main-screen model/runtime controls and actionable readiness states.
- [x] Route Claude Code and Codex subscription choices through ExternalCodingRunner with existing
      clone, approval, check, cancellation, and source-integrity boundaries.
- [x] Preserve headless, `exec`, `run`, `--plain`, and existing config compatibility.
- [x] Add unit/TUI/CLI/integration regression coverage.
- [x] Run full release gates and independent review.

Pending milestone closure: user article review and the complete M10 commit.
