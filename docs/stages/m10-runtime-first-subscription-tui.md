# M10: Runtime-first subscription TUI

> Status: implementation and independent review complete; article review and commit pending.

## Scope

Let bare interactive `rivumi` use an installed Claude Code or Codex CLI login without confusing an
external agent runtime with Rivumi's native model providers. A usable default must not require a raw
model ID, and model/approval choices must remain changeable and narrowly scoped.

## References studied

| Reference | Boundary used |
| --- | --- |
| Claude Code model configuration | account default plus `sonnet`, `opus`, `haiku`, and `best` aliases |
| Codex model documentation | Automatic default plus current Sol, Terra, and Luna choices |
| Pi / Oh My Pi model UX | login/provider and model picker are separate; Ctrl+L changes model |
| OpenCode provider/model UX | connect runtime first, then switch among available models |
| Rivumi M5 external runner | official CLI owns login/loop; Rivumi owns clone, patch audit, and verification |

Official pages were retrieved through the local Groundlane MCP fetch path because the configured
`stealth_fetch` transport was unavailable. No credential value was read or forwarded.

## Design

The TUI persists `runtime` and an optional non-secret `runtime_model` separately from Rivumi's
provider/model defaults. Claude Code and Codex start with `Automatic`, represented as no CLI model
argument. Ctrl+L opens the same runtime/model control later. Ollama still uses bounded loopback
discovery; unknown custom API endpoints remain explicit.

External choices route only to `ExternalCodingRunner`. The installed official child owns its login
and agent loop. Rivumi asks once before granting modification of a disposable clone, validates the
resulting tracked patch and allowed paths, runs exact final checks, and proves the source repository
is unchanged.

The composer separates `Ask` from `Agent` instead of inferring intent. External runtimes start in
Ask: each question runs read-only outside the repository, skips Git/clone/approval/verification,
and receives a bounded process-local transcript for follow-ups. Agent remains the explicit coding
path with every existing safety gate. Switching runtime, model, or mode clears the Ask transcript;
no vendor session identifier or conversation is persisted.

`Allow for session` is process-local and keyed by backend identity. Granting
`external_agent:codex-cli` does not permit Claude Code and does not permit Rivumi's own `replace_text`
or `apply_patch`. Approval returns an immediate activity message, yields to the UI, then checks Stop
before any source snapshot or clone preparation.

## Deliberate limitations

- Installed executable discovery does not claim the user is authenticated.
- External CLI output is normalized and bounded, but is not yet token-streamed live.
- Ask continuity is process-local bounded replay, not vendor session persistence or durable resume.
- Agent remains one task per isolated run.
- API provider model catalogs are not guessed for arbitrary custom endpoints.

## Verification

```text
uv run pytest -q
252 tests passed

uv run ruff check .
All checks passed!

uv lock --check
resolved lock is current

uv build
sdist and wheel built successfully

scripts/install-dev-cli (run before the Rivumi rename)
global editable pca synchronized with uv.lock; dependency check and help smoke passed

git diff --check
clean
```

Independent review first found an over-broad MODIFY session grant and a cancellation gap between
approval and workspace preparation. Both were reproduced, narrowed, and regression-tested. A
second review traced Ask before `TaskContract` creation, confirmed Claude no-tools and Codex
read-only empty-cwd routing, rechecked every Agent gate, ran 87 relevant tests plus the full 252,
and returned GO.

## Artifacts

- Research: `.research/onboarding-runtime-model-ux.md`
- Plan: `.work/runtime-onboarding-plan.md`
- Ask/Agent plan: `.work/ask-agent-mode-plan.md`
- Draft article:
  `quidproquo/src/content/posts/ai/2026-08-22-python-coding-agent-runtime-first-subscription-tui.md`
  (historical pre-rename filename)

## Commit

Pending user review and formatted commit confirmation.
