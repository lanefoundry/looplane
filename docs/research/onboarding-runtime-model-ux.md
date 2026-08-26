# PCA onboarding: runtime, authentication, and model UX

Date: 2026-08-22

## Decision

PCA should not present `Anthropic API` as if it were a Claude subscription. The first choice is
the execution runtime, then its authentication state, then an optional model override.

Recommended first-run choices:

1. `PCA agent`
   - `Ollama (local)`
   - `Anthropic API (API key)`
   - `OpenAI-compatible API`
   - other native API providers
2. `Claude Code (local subscription, experimental)`
3. `Codex CLI (ChatGPT subscription, local)`

The external CLI choices remain `ExternalAgentBackend` runtimes. They must not be routed through
`ModelProvider` or borrow subscription tokens for PCA's own loop.

## Model behavior

- Subscription runtimes start with the official CLI's account/runtime default. Model selection is
  optional during onboarding and can be changed inside the session.
- API-backed PCA runs may enter the main screen without a model, but Run remains disabled until a
  model is selected. Use a catalog/picker where discovery exists; do not require a raw ID in the
  first-run modal.
- Ollama may preselect a bounded, locally discovered tool-capable model.
- A saved explicit choice or last-used model wins over a recommendation. Changing provider/runtime
  clears incompatible model and API URL state.

## Implemented behavior

- The modal separates the external runtime from PCA's API/local connection.
- `Anthropic API (API key billing)` is distinct from the installed Claude Code runtime.
- Claude Code and Codex omit the model argument for `Automatic`, then offer official aliases or
  current recommended models through `Ctrl+L`.
- Ollama selects the first bounded local discovery result; unknown provider endpoints may enter the
  main screen but cannot Run until an explicit model is supplied.
- External choices route only through `ExternalCodingRunner`; they retain disposable-clone,
  approval, cancellation, verification, patch, and source-integrity checks.
- `Allow for session` is process-scoped and shared across later bounded tasks, never persisted.

## Primary-source comparison

| Tool | Authentication | Model startup | In-session selection |
| --- | --- | --- | --- |
| Claude Code | First launch supports Claude.ai Pro/Max subscription or API/provider credentials | Uses the runtime default for the account unless overridden | `/model` opens the picker; a choice can be session-only or saved |
| Codex | Signs in with ChatGPT subscription or an API key | Uses a recommended model when config has no explicit model | Model picker/config provides overrides |
| Pi / Oh My Pi lineage | `/login` selects subscription/provider; API keys are separate | Maintains provider model catalogs | `/model` or Ctrl+L opens the selector |
| OpenCode | `/connect` configures provider credentials | CLI flag, config, last used, then internal priority | `/models` opens the picker |

The shared pattern is: authentication and model selection are separate; a model default or
last-used choice lets the user reach the session; the picker remains available after startup.

## PCA implementation boundary

The TUI should become a shell with a persistent runtime/model control instead of a blocking model
form:

1. Open the main screen immediately.
2. Show `Runtime`, `Connection`, and `Model` as separate state.
3. If connection is missing, offer the appropriate action (`Use installed Claude Code login`,
   `Use installed Codex login`, or show the API-key environment-variable name).
4. If an API PCA run has no model, show `Model required` and disable Run; open a picker from the
   header or command palette.
5. For external Claude/Codex runtimes, show `Recommended (managed by Claude Code/Codex)` and do not
   require a model override.
6. Preserve disposable clone, explicit external modification approval, exact checks, source
   integrity validation, cancellation, and local-only subscription boundaries.

## Regression checks

- Provider/runtime switching clears incompatible model and endpoint state.
- Subscription routes only to the matching external backend; API routes only to ModelProvider.
- No consumer token is read, copied, logged, persisted, or sent to Cloudflare.
- Missing API model disables Run without blocking navigation.
- Ollama discovery remains fixed-loopback, bounded, printable, and tool-capability aware.
- Headless `-p`, `exec`, `run`, and `--plain` behavior remains unchanged.

## Sources

- https://code.claude.com/docs/en/authentication
- https://code.claude.com/docs/en/model-config
- https://developers.openai.com/codex/auth
- https://developers.openai.com/codex/models
- https://opencode.ai/docs/providers/
- https://opencode.ai/docs/models/
- https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md
- https://quidproquo.cc/posts/ai/2026-08-21-python-coding-agent-subscription-cli-isolated-clone
- https://quidproquo.cc/posts/ai/2026-03-28-openclaw-model-providers
