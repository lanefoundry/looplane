# M4 — Claude subscription integration boundary

Date checked: 2026-08-21
Scope: whether a third-party coding-agent product may use Claude Pro/Max OAuth as the model transport for its own agent loop. This is a product-policy and technical architecture assessment, not legal advice.

## Decision

**Do not implement Claude Pro/Max OAuth as a `ModelProvider` for PCA without prior written approval from Anthropic.** Pi and OMP demonstrate that this is technically possible, but Anthropic's current Agent SDK documentation explicitly says third-party developers may not offer `claude.ai` login or Claude subscription rate limits in their products unless previously approved. OpenCode has removed its bundled subscription plugins for the same stated reason.

For PCA, use this order:

1. **Native Anthropic API adapter — default.** Keep PCA's own loop, tools, guards, checkpoints, and evaluations; send model requests through an Anthropic API key governed by the Commercial Terms.
2. **Approved commercial proxy — deployment option.** A proxy is safe only when it terminates an authorized Anthropic API, Bedrock, Vertex AI, Foundry, or explicitly approved reseller credential. It does not make consumer OAuth reusable.
3. **`ExternalAgentBackend` — separate, local-only integration.** Optionally delegate a whole run to the installed official Claude Code CLI/Agent SDK without extracting credentials. This is not PCA's own loop and must not be marketed or hosted as a way to re-export Pro/Max rate limits. Obtain Anthropic approval before distributing it as a product integration.

```text
PCA own loop ── ModelProvider ── Anthropic Messages API
                         └────── approved API proxy ── Anthropic API

PCA dispatcher ── ExternalAgentBackend ── official Claude Code CLI/SDK
                                           owns loop, tools, auth, session
```

`claude setup-token` is not a loophole. It is an official Claude Code credential mechanism; PCA should neither read it nor reinterpret it as a raw inference token.

## What the official sources establish

| Source (checked 2026-08-21) | Relevant boundary |
| --- | --- |
| [Agent SDK overview](https://code.claude.com/docs/en/agent-sdk) | The Agent SDK provides Claude Code's agent loop, tools, and context management. It distinguishes this from a client SDK, where the developer owns the loop. Most importantly, it says third parties may not offer `claude.ai` login or subscription rate limits in products without prior approval, including products built with the Agent SDK, and directs developers to API-key authentication. |
| [Anthropic Commercial Terms](https://www.anthropic.com/legal/commercial-terms) (effective 2025-06-17) | Commercial services may power products for customers/end users, subject to the incorporated policies. Consumer offerings such as `claude.ai` are separately governed. The terms restrict unapproved resale and building a competing/duplicative service. |
| [Anthropic Usage Policy](https://www.anthropic.com/legal/aup) (effective 2025-09-15) | Applies to direct users and authorized passthrough/reseller use; prohibits bypassing platform restrictions and requires disclosure for external-facing agents/chatbots. |

Only `stealth_fetch` was used for web-page retrieval. The Agent SDK page was fetched from the official Anthropic documentation redirect; the other two sources are official Anthropic legal pages. The decisive restriction is in the Agent SDK documentation, so unavailable secondary pages were not substituted with unofficial summaries.

### Local official CLI/SDK evidence

The locally installed Claude Code is `2.1.237`:

- `claude -p` runs non-interactively and supports JSON/stream-JSON input and output, resume/session options, permission modes, and tool controls.
- `claude setup-token` creates a long-lived authentication token and explicitly requires a Claude subscription.
- `claude --bare` says authentication is limited to `ANTHROPIC_API_KEY` or `apiKeyHelper`; it does not read OAuth/keychain credentials. Bedrock, Vertex, and Foundry use their own credentials.
- The installed `@anthropic-ai/claude-agent-sdk` `0.1.77` launches the Claude Code executable with stream-JSON. Its API exposes process/session/permission controls. This is a full external agent runtime, not a raw completion transport.

Technical implication: headless Claude Code is a good shape for a delegated backend, while `--bare` plus an API key is the clean automation path. Neither the presence of `setup-token` nor the ability to invoke `claude -p` grants a third-party product permission to expose subscription capacity.

## Current ecosystem comparison

These source snapshots show capability and project choices; they do not grant authorization.

| Project | Snapshot | Current behavior | Interpretation for PCA |
| --- | --- | --- | --- |
| Pi | `5cd93f688aaab89dbb6dfa4aca535f21796ae185` (2026-08-20) | `packages/ai/src/auth/oauth/anthropic.ts` implements Claude Pro/Max OAuth directly, including authorization, refresh, and the `user:inference` scope; it marks this as subscription auth. | Proves technical feasibility only. Copying this flow into a third-party product would cross Anthropic's published boundary unless approved. |
| OpenCode | `5e75e5e9901f0d178f425bfb47f1bd46cbe78a59` (`1.18.19`) | Current provider docs explicitly say Anthropic prohibits Pro/Max plugins and that bundled plugins were removed as of `1.3.0`; the core Anthropic provider is API/provider plumbing rather than bundled consumer OAuth. | Strongest ecosystem precedent for the safe product choice: support API credentials, not subscription login. Some nearby prose still mentions Pro/Max and appears stale; the explicit warning and source state are controlling evidence. |
| OMP | `72000acfeb902e21816252699482887f34d1a5a4` (`17.4.0`) | `packages/ai/src/registry/oauth/anthropic.ts` directly implements OAuth, refresh, organization bootstrap, and subscription inference, and its README advertises Pro/Max OAuth. | Also proves feasibility, not permission. Its use of Claude Code-specific client characteristics is exactly the coupling PCA should avoid. |

## Legal and technical answer

### Can a third-party own agent loop use Claude Pro/Max OAuth?

- **Technically: yes.** Pi and OMP contain working-looking implementations, and Claude Code itself has subscription authentication.
- **As a supported third-party product integration: no, not without prior Anthropic approval.** Anthropic's published Agent SDK policy covers both `claude.ai` login and re-use of subscription rate limits. A user supplying their own token does not remove the product-level restriction.
- **For purely personal local experiments: ambiguous, not a dependable architecture.** The published wording focuses on third-party products. That ambiguity should not be converted into a claim of permission, and it is unsuitable for a Cloudflare-hosted, multi-user, or distributed service.

Therefore PCA should not add `ANTHROPIC_OAUTH_TOKEN`, a `login with Claude` command, setup-token import, browser OAuth, keychain scraping, or emulation of the official Claude Code client. It should also never relay a consumer token through Cloudflare.

## Recommended PCA contracts

### 1. Keep Anthropic in the native model-provider layer

```python
class ModelProvider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class AnthropicApiProvider(ModelProvider):
    """Anthropic Messages API using a commercial API credential."""
```

Required guards:

- accept only API/provider credentials appropriate to the selected commercial endpoint;
- keep secrets server-side and redact them from events, checkpoints, exception strings, and telemetry;
- reject token types known to be consumer OAuth/setup-token material;
- retain PCA's existing tool approvals, path/size constraints, sandbox, usage accounting, and evaluation loop;
- label proxy endpoints explicitly and allowlist them rather than accepting an arbitrary credential-forwarding URL.

### 2. Treat an approved proxy as transport, not policy bypass

```text
PCA ModelProvider -> organization-controlled proxy -> authorized API endpoint
```

The proxy may centralize secrets, budgets, audit logs, provider routing, and Cloudflare ingress. It must use commercial API credentials or a provider relationship that expressly permits passthrough. A proxy carrying Pro/Max OAuth has the same underlying policy problem and adds credential-custody risk.

### 3. Separate full-agent delegation from model inference

```python
class ExternalAgentBackend(Protocol):
    async def run(
        self,
        task: ExternalTask,
        event_sink: EventSink,
        approval_bridge: ApprovalBridge,
    ) -> ExternalRunResult: ...
```

For a local Claude Code backend:

- spawn the user-installed official executable; do not bundle or impersonate it;
- pass the task through documented headless JSON/stream-JSON interfaces;
- let Claude Code own its authentication, model loop, tools, permissions, and sessions;
- do not read, export, persist, refresh, proxy, or display its credentials;
- normalize only task status, events, artifacts, cost/usage when reported, and terminal reason;
- make the backend visibly distinct from PCA's native loop; avoid nesting PCA tools inside Claude Code tools;
- default it off for hosted/cloud use and require written Anthropic approval before shipping a product feature that relies on subscription capacity.

This backend is useful for comparing harness behavior and providing a local operator workflow, but it cannot validate PCA's own loop. Native provider evaluations and external-backend evaluations must be reported separately.

## Implementation decision for M4

Recommended CLI/product surface:

```text
pca run --provider anthropic-api ...    # own loop, commercial API key
pca run --provider anthropic-proxy ...  # own loop, approved API proxy
pca backend claude-code ...             # optional delegated local agent
```

Do not add `pca auth login-claude` until Anthropic grants written approval covering the intended distribution and rate-limit use.

Acceptance tests for the boundary:

1. Native Anthropic adapter accepts an API key through the secret resolver and never serializes it.
2. Consumer OAuth/setup-token-shaped credentials are rejected by the native adapter with remediation to use an API key.
3. Proxy configuration requires an allowlisted endpoint and never changes the credential-policy decision.
4. External backend works without PCA reading auth files or environment-token values.
5. External-backend events are labelled `backend=claude-code`; results never claim they exercised PCA's native loop.
6. Cloud deployment excludes the subscription backend and exposes no consumer-login route.

## Bottom line

The architecture closest to the stated goal—**our own Python agent loop with a general model interface**—is the native Anthropic API adapter. An approved commercial proxy is the same architecture with centralized transport. `ExternalAgentBackend` is valuable as a sharply separated, local delegated-agent option, but it is neither a raw model adapter nor a safe general-purpose route to Pro/Max subscription capacity. Pi and OMP are useful implementation references for understanding the mechanics; OpenCode's removal is the better product-policy precedent.
