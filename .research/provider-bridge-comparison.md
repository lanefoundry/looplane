# Provider bridge comparison: OMP, OpenCode, and Pi

Date: 2026-08-21 (Asia/Taipei)

Scope: read-only architecture research for the Python coding agent. This report compares current upstream source snapshots and does not inspect or copy any credential value.

## Executive conclusion

The three projects do **not** primarily turn an installed coding-agent CLI into a model server.

- **Pi** is a client-side provider registry plus protocol adapters and credential/OAuth handlers. `baseUrl` means “send this adapter's request to another upstream,” not “Pi is now a proxy server.”
- **OpenCode** is also a client. Its ChatGPT integration is an in-process OAuth + `fetch` rewrite adapter that redirects AI SDK Responses calls to the Codex backend. Its custom `baseURL` support points the client at an existing proxy or local server.
- **OMP** has the same client-adapter core, **and additionally ships an optional real HTTP auth gateway**. The gateway accepts OpenAI Chat Completions, OpenAI Responses, Anthropic Messages, or OMP-native streaming; translates them into OMP's neutral context; resolves credentials through its broker; then dispatches through provider-specific adapters. This is the closest reference for the “反代理過去” idea.

In precise HTTP terminology, OMP calls that component a **forward proxy / protocol gateway**, not a reverse proxy. From our product user's point of view, “local reverse proxy” is understandable, but the implementation boundary should be called `model gateway` or `provider bridge`.

The Python agent should therefore have two separate layers:

1. a provider-neutral agent core calling explicit client transports; and
2. an optional HTTP gateway that re-exposes a bounded public wire protocol for other clients or remote workers.

The gateway must not become the agent loop, and “OpenAI-compatible” must not erase the differences between Chat Completions, Responses, Codex Responses, and Anthropic Messages.

## Source snapshots

| Project | Snapshot inspected | Notes |
|---|---|---|
| OMP (`can1357/oh-my-pi`) | `72000acfeb902e21816252699482887f34d1a5a4` (`main`, 2026-08-20) | Official GitHub repository source and docs |
| OpenCode (`anomalyco/opencode`) | `11e8110f9e6863369d361d31f601cecc8202c9c6` (`dev`, 2026-08-20) | Official GitHub repository source and docs |
| Pi (`badlogic/pi-mono`) | `5cd93f688aaab89dbb6dfa4aca535f21796ae185` (`main`, package `0.84.2`, 2026-08-20) | Official GitHub repository source and docs |
| Locally installed Pi | package `0.70.6` | Used only to record a version-specific Anthropic warning; it is older than upstream and is labelled as such below |

Changing authentication and product-policy behavior should be rechecked before implementation or release.

## Comparison matrix

| Dimension | OMP | OpenCode | Pi |
|---|---|---|---|
| Normal mode | In-process provider adapters | In-process provider/AI SDK adapters | In-process provider adapters |
| Explicit protocol discriminator | Yes: e.g. `openai-completions`, `openai-responses`, `openai-codex-responses`, `anthropic-messages`, `ollama-chat` | Mostly delegated to the selected AI SDK package/provider, with custom provider package such as `@ai-sdk/openai-compatible` | Yes: `openai-completions`, `openai-responses`, `openai-codex-responses`, `anthropic-messages`, etc. |
| Custom base URL | Yes | Yes | Yes |
| Runs a real model gateway | **Yes, optional `auth-gateway`** | No equivalent found in the provider integration examined | No equivalent found in the coding-agent/provider docs examined |
| ChatGPT/Codex subscription | Direct OAuth adapter plus Codex-specific transport | Direct OAuth adapter plus request rewrite | Direct OAuth adapter plus Codex-specific transport |
| Anthropic subscription | Direct OAuth implemented | Deliberately no longer bundled; docs say Anthropic prohibits third-party Pro/Max plugins | Current upstream implements direct OAuth; older installed release warns usage is extra-usage billing, not plan limits |
| Ollama | Built-in discovery/keyless local engine; also explicit protocol support | Custom OpenAI-compatible provider at loopback base URL | OpenAI-compatible Chat Completions at loopback base URL; compatibility flags often required |

## OMP: adapters plus a real gateway

### Provider model

OMP separates provider identity from wire protocol:

- `Provider` is the backend/account namespace and `Model` is chosen as `provider/model-id` ([`docs/providers.md` lines 1-7](https://github.com/can1357/oh-my-pi/blob/72000acfeb902e21816252699482887f34d1a5a4/docs/providers.md#L1-L7)).
- The catalog is built from bundled models, custom `models.yml`, runtime discovery, and extensions ([lines 9-25](https://github.com/can1357/oh-my-pi/blob/72000acfeb902e21816252699482887f34d1a5a4/docs/providers.md#L9-L25)).
- Its API union explicitly distinguishes `openai-completions`, `openai-responses`, `openai-codex-responses`, `anthropic-messages`, and `ollama-chat` ([`packages/catalog/src/types.ts` lines 8-23](https://github.com/can1357/oh-my-pi/blob/72000acfeb902e21816252699482887f34d1a5a4/packages/catalog/src/types.ts#L8-L23)).

This is a strong boundary: a model can have a provider identity, a base URL, and a transport protocol without conflating them.

### Custom base URL and Ollama

OMP's `models.yml` can point an `openai-completions` adapter at a gateway or local endpoint. It supports authenticated remote endpoints as well as a keyless loopback provider with `auth: none` ([`docs/providers.md` lines 302-349](https://github.com/can1357/oh-my-pi/blob/72000acfeb902e21816252699482887f34d1a5a4/docs/providers.md#L302-L349)).

Ollama is not treated as a magical generic provider. OMP has a built-in local-engine discovery path, defaults it to `http://127.0.0.1:11434`, and treats it as keyless unless configured otherwise ([lines 182-195](https://github.com/can1357/oh-my-pi/blob/72000acfeb902e21816252699482887f34d1a5a4/docs/providers.md#L182-L195)).

### Credential boundary

OMP keeps a deterministic credential precedence. A config key for a custom `baseUrl` beats stored OAuth so an upstream OAuth token is not accidentally sent to an unrelated proxy ([`docs/providers.md` lines 27-39](https://github.com/can1357/oh-my-pi/blob/72000acfeb902e21816252699482887f34d1a5a4/docs/providers.md#L27-L39)). This is a security boundary worth copying.

It also defines provider-scoped OAuth login and an auth broker mode instead of treating every secret as one global `api_key` ([lines 41-52](https://github.com/can1357/oh-my-pi/blob/72000acfeb902e21816252699482887f34d1a5a4/docs/providers.md#L41-L52)).

### The actual proxy/gateway

OMP is the exception in this comparison because it implements an optional HTTP gateway:

- The auth broker holds refresh tokens and performs refreshes.
- The auth gateway is documented as a forward proxy accepting OpenAI Chat Completions, Anthropic Messages, OpenAI Responses, and OMP-native streams; clients never receive provider access tokens ([`docs/auth-broker-gateway.md` lines 1-10](https://github.com/can1357/oh-my-pi/blob/72000acfeb902e21816252699482887f34d1a5a4/docs/auth-broker-gateway.md#L1-L10)).
- The architecture explicitly separates the canonical broker writer from the gateway and its clients ([lines 12-42](https://github.com/can1357/oh-my-pi/blob/72000acfeb902e21816252699482887f34d1a5a4/docs/auth-broker-gateway.md#L12-L42)).
- The gateway exposes `/v1/chat/completions`, `/v1/messages`, `/v1/responses`, and `/v1/pi/stream` ([lines 127-155](https://github.com/can1357/oh-my-pi/blob/72000acfeb902e21816252699482887f34d1a5a4/docs/auth-broker-gateway.md#L127-L155)).
- It intentionally has **no raw passthrough**. Every request goes through the provider adapter so OAuth request shaping and provider quirks remain centralized ([lines 155-159](https://github.com/can1357/oh-my-pi/blob/72000acfeb902e21816252699482887f34d1a5a4/docs/auth-broker-gateway.md#L155-L159)).

The server source describes the same pipeline as “foreign wire → OMP Context → provider stream → OMP events → foreign wire” ([`packages/ai/src/auth-gateway/server.ts` lines 1-19](https://github.com/can1357/oh-my-pi/blob/72000acfeb902e21816252699482887f34d1a5a4/packages/ai/src/auth-gateway/server.ts#L1-L19)). Its route table is explicit ([lines 73-82](https://github.com/can1357/oh-my-pi/blob/72000acfeb902e21816252699482887f34d1a5a4/packages/ai/src/auth-gateway/server.ts#L73-L82)).

This is the best direct reference for our optional Python bridge. The useful pattern is not OMP's total feature count; it is the boundary between wire parsers, neutral context/events, auth resolution, and provider dispatch.

## OpenCode: client adapter, not a server

### Custom base URL and Ollama

OpenCode says `baseURL` is for proxy services or custom endpoints ([`packages/web/src/content/docs/providers.mdx` lines 27-44](https://github.com/anomalyco/opencode/blob/11e8110f9e6863369d361d31f601cecc8202c9c6/packages/web/src/content/docs/providers.mdx#L27-L44)). That config changes where OpenCode sends requests; it does not expose a new listener.

Its Ollama example uses `@ai-sdk/openai-compatible` and `http://localhost:11434/v1` ([lines 1613-1647](https://github.com/anomalyco/opencode/blob/11e8110f9e6863369d361d31f601cecc8202c9c6/packages/web/src/content/docs/providers.mdx#L1613-L1647)). The same pattern is used for other loopback OpenAI-compatible servers, which shows that `provider package + baseURL + model IDs` is OpenCode's extensibility boundary.

### ChatGPT/Codex OAuth

OpenCode's Codex integration is a client-side OAuth and request adapter:

- It defines the OpenAI OAuth issuer and ChatGPT Codex Responses endpoint, uses PKCE, and extracts the ChatGPT account ID from token claims ([`packages/opencode/src/plugin/openai/codex.ts` lines 10-101](https://github.com/anomalyco/opencode/blob/11e8110f9e6863369d361d31f601cecc8202c9c6/packages/opencode/src/plugin/openai/codex.ts#L10-L101)).
- Its loader installs a custom `fetch`. It removes the placeholder authorization header, refreshes its own stored OAuth credential, injects bearer/account headers, and rewrites ordinary Responses/Chat Completions request URLs to the Codex endpoint ([lines 325-433](https://github.com/anomalyco/opencode/blob/11e8110f9e6863369d361d31f601cecc8202c9c6/packages/opencode/src/plugin/openai/codex.ts#L325-L433)).

That is not an HTTP reverse proxy. It is an in-process transport interceptor. It proves that ChatGPT/Codex is not merely “OpenAI-compatible base URL”: it needs separate OAuth, account identity, endpoint rewrite, model filtering, and Codex request semantics.

### Anthropic subscription policy boundary

OpenCode's current provider docs say third-party plugins that use Claude Pro/Max are explicitly prohibited by Anthropic and were removed from the bundle as of OpenCode 1.3.0. The same note lists ChatGPT Plus as supported with zero setup ([`providers.mdx` lines 332-369](https://github.com/anomalyco/opencode/blob/11e8110f9e6863369d361d31f601cecc8202c9c6/packages/web/src/content/docs/providers.mdx#L332-L369)).

This is a statement in OpenCode's official repository, not an independent legal opinion. Still, it is strong enough that our default Python implementation should **not** promise that a Claude Pro/Max subscription can power an arbitrary third-party harness.

## Pi: client provider registry and direct OAuth transports

### Provider and protocol boundary

Current Pi defines distinct API kinds including Chat Completions, Responses, Codex Responses, and Anthropic Messages ([`packages/ai/src/types.ts` lines 17-29](https://github.com/badlogic/pi-mono/blob/5cd93f688aaab89dbb6dfa4aca535f21796ae185/packages/ai/src/types.ts#L17-L29)). Its provider object separately owns identity, base URL, headers, auth semantics, models, and protocol dispatch ([`packages/ai/src/models.ts` lines 97-103](https://github.com/badlogic/pi-mono/blob/5cd93f688aaab89dbb6dfa4aca535f21796ae185/packages/ai/src/models.ts#L97-L103), [`models.ts` lines 739-779](https://github.com/badlogic/pi-mono/blob/5cd93f688aaab89dbb6dfa4aca535f21796ae185/packages/ai/src/models.ts#L739-L779)).

Pi's extension docs explicitly describe `baseUrl` as redirecting an existing provider through an already-running proxy. A custom provider selects a concrete API such as `openai-completions` ([`packages/coding-agent/docs/custom-provider.md` lines 31-119](https://github.com/badlogic/pi-mono/blob/5cd93f688aaab89dbb6dfa4aca535f21796ae185/packages/coding-agent/docs/custom-provider.md#L31-L119), [lines 121-186](https://github.com/badlogic/pi-mono/blob/5cd93f688aaab89dbb6dfa4aca535f21796ae185/packages/coding-agent/docs/custom-provider.md#L121-L186)). Again, this is client routing, not a server.

### Ollama

Pi documents Ollama as `openai-completions` at `http://localhost:11434/v1`. A dummy key is needed for Pi's availability/auth machinery even though Ollama ignores it. It also calls out common compatibility differences such as unsupported `developer` role and `reasoning_effort` ([`packages/coding-agent/docs/models.md` lines 17-63](https://github.com/badlogic/pi-mono/blob/5cd93f688aaab89dbb6dfa4aca535f21796ae185/packages/coding-agent/docs/models.md#L17-L63)).

This demonstrates why “OpenAI-compatible” must have capability/quirk flags rather than assuming exact OpenAI behavior.

### ChatGPT/Codex OAuth

Pi implements its own ChatGPT OAuth client; it does not consume the installed Codex CLI as a subprocess and does not read Codex CLI credentials. The current source defines PKCE browser/device flows against `auth.openai.com` ([`packages/ai/src/auth/oauth/openai-codex.ts` lines 1-45](https://github.com/badlogic/pi-mono/blob/5cd93f688aaab89dbb6dfa4aca535f21796ae185/packages/ai/src/auth/oauth/openai-codex.ts#L1-L45)).

The resulting provider is explicitly `openai-codex`, marks OAuth as a ChatGPT Plus/Pro subscription flow, uses `https://chatgpt.com/backend-api`, and dispatches through the special `openai-codex-responses` API ([`packages/ai/src/providers/openai-codex.ts` lines 1-21](https://github.com/badlogic/pi-mono/blob/5cd93f688aaab89dbb6dfa4aca535f21796ae185/packages/ai/src/providers/openai-codex.ts#L1-L21)). The transport has Codex-specific retries, request shaping, caching, streaming, and headers rather than being an alias for standard Responses ([`packages/ai/src/api/openai-codex-responses.ts` lines 41-99](https://github.com/badlogic/pi-mono/blob/5cd93f688aaab89dbb6dfa4aca535f21796ae185/packages/ai/src/api/openai-codex-responses.ts#L41-L99)).

### Anthropic subscription and version discrepancy

Current upstream Pi `0.84.2` implements a separate Anthropic OAuth login marked `Claude Pro/Max`/subscription ([`packages/ai/src/providers/anthropic.ts` lines 43-58](https://github.com/badlogic/pi-mono/blob/5cd93f688aaab89dbb6dfa4aca535f21796ae185/packages/ai/src/providers/anthropic.ts#L43-L58)); the OAuth source requests `user:inference` and Claude Code-related scopes ([`packages/ai/src/auth/oauth/anthropic.ts` lines 28-37](https://github.com/badlogic/pi-mono/blob/5cd93f688aaab89dbb6dfa4aca535f21796ae185/packages/ai/src/auth/oauth/anthropic.ts#L28-L37)).

However, the locally installed older Pi `0.70.6` displays this exact product warning in `dist/modes/interactive/interactive-mode.js:76-79`: subscription auth in a third-party harness draws from Anthropic extra usage billed per token, not Claude plan limits. The warning is not present in the current upstream source inspected, so it must be treated as **version-specific evidence**, not a current universal contract.

Taken with OpenCode's stronger prohibition note, the safe product decision is to support Anthropic API keys and approved Anthropic-compatible endpoints first, and keep direct subscription OAuth out of the default milestone unless current Anthropic terms explicitly authorize our use.

## Are these “reverse proxies”?

| Component | Opens an HTTP model endpoint? | Holds/refreshes provider credentials? | Translates wire protocols? | Classification |
|---|---:|---:|---:|---|
| Pi provider adapter | No | Yes, in client | Internally | Client adapter |
| OpenCode Codex plugin | No | Yes, in client | Rewrites AI SDK request to Codex | Client adapter/interceptor |
| OMP normal provider path | No | Yes, in client/broker | Internally | Client adapter |
| OMP auth gateway | Yes | Via broker-backed store | Yes | Real authenticated model gateway/forward proxy |
| Ollama | Yes | Normally no local auth | Implements an OpenAI-compatible endpoint | Model server/upstream |
| Cloudflare AI Gateway / LiteLLM / custom URL | Yes | Depends on gateway | Depends on product | External upstream gateway |

So “要反代理過去” should mean: build a bounded local model gateway like OMP's auth gateway, **not** launch `codex`/`claude` as the UX and **not** assume their CLIs expose a raw model API.

## Recommended protocol boundaries for the Python agent

### 1. Keep provider identity, protocol, endpoint, and auth separate

Suggested configuration shape:

```yaml
providers:
  local-ollama:
    protocol: openai_chat
    base_url: http://127.0.0.1:11434/v1
    auth: none
    model: qwen-coder
    capabilities:
      developer_role: false
      reasoning_effort: false

  chatgpt-codex:
    protocol: openai_codex_responses
    base_url: https://chatgpt.com/backend-api
    auth: oauth_openai_codex
    model: gpt-5.4

  anthropic-api:
    protocol: anthropic_messages
    base_url: https://api.anthropic.com
    auth: api_key
    model: claude-sonnet
```

Do not infer protocol solely from provider name or URL.

### 2. Use explicit transport interfaces

The internal model boundary can remain one `ModelProvider.stream(request) -> AsyncIterator[ModelEvent]`, but construction must select one of these concrete transports:

- `OpenAIChatTransport` — `/v1/chat/completions` and Ollama-compatible endpoints;
- `OpenAIResponsesTransport` — standard `/v1/responses`;
- `OpenAICodexResponsesTransport` — ChatGPT/Codex endpoint, OAuth account header, Codex-specific body/stream rules;
- `AnthropicMessagesTransport` — `/v1/messages` and Anthropic event schema.

Normalize only after decoding each native event. Preserve native metadata in an optional diagnostic field so retry, usage, and capability problems remain debuggable.

### 3. Separate credential providers from transports

Use a narrow credential contract such as:

```python
class CredentialProvider(Protocol):
    async def headers(self, audience: EndpointAudience) -> Mapping[str, str]: ...
    async def refresh(self) -> None: ...
```

Required controls:

- credentials are scoped to provider + endpoint audience;
- a custom `base_url` must never automatically inherit a first-party OAuth token;
- the agent owns its own OAuth grants if OAuth is implemented; never scrape `~/.codex`, Claude Code, Pi, OMP, or OpenCode credential stores;
- refresh is single-flight and writes atomically;
- logs/events redact authorization, cookies, account IDs, callback codes, and refresh tokens;
- remote HTTP requires HTTPS; plain HTTP is allowed only for explicit loopback/local-network development policy.

### 4. Make the gateway optional and protocol-bounded

The first gateway should expose only:

- `GET /healthz`;
- `GET /v1/models`;
- `POST /v1/chat/completions` with SSE streaming;
- later, `POST /v1/responses` if a real consumer needs it.

Internally:

```text
incoming OpenAI wire
  -> strict parser and request limits
  -> neutral ModelRequest
  -> selected explicit transport
  -> neutral ModelEvent stream
  -> OpenAI wire encoder
```

Do not add raw arbitrary-URL passthrough. It creates SSRF and credential exfiltration risk and bypasses provider-specific request shaping. Bind to loopback by default, require a separate gateway bearer for non-loopback access, and place TLS/network policy outside or in front of the service.

### 5. Do not put the agent loop behind the model adapter

Our own interactive CLI should continue to own:

- messages/context;
- tool calls;
- approvals;
- checkpoints/resume;
- sandbox/security;
- events and eval artifacts.

The provider bridge returns model events only. Invoking `codex exec` or `claude -p` would delegate a second agent loop with its own tools, approvals, context, and session semantics, making our harness impossible to reason about. Official CLIs can remain optional comparison/E2E backends, but not the generic `ModelProvider` implementation.

### 6. Treat Cloudflare deployment and subscriptions as different trust modes

- **Local CLI:** loopback Ollama, API keys, and (if explicitly authorized) locally stored OAuth are possible.
- **Cloudflare/container service:** prefer API keys, Workers AI, Cloudflare AI Gateway, or an operator-controlled gateway. Do not silently upload local subscription OAuth credentials into a cloud container.
- A local bridge can later expose a tightly authenticated endpoint to a remote worker, but then it becomes an always-on secret-bearing service with network, replay, audit, and rate-limit obligations.

## Implementation order implied by the references

1. Replace the generic “OpenAI-compatible only” assumption with explicit `openai_chat` and `openai_responses` protocol discriminators.
2. Allow `http://127.0.0.1`, `http://localhost`, and `http://[::1]` for local endpoints; require HTTPS elsewhere and reject URL credentials/query/fragment.
3. Make Ollama a configuration preset over `OpenAIChatTransport`, not a special agent core.
4. Add compatibility flags and fail visibly for unsupported tool/reasoning features.
5. Implement the optional loopback model gateway with one inbound protocol first.
6. Add a separately tested `openai_codex_responses` transport and **our own** OAuth store only after confirming current authorization/product terms.
7. Keep Anthropic subscription OAuth out of the default path; implement `anthropic_messages` with API key/approved proxy first.
8. Add E2E fixtures per protocol: streamed text, multiple tool calls, tool-result continuation, usage, retry/429, malformed SSE, disconnect/cancel, and credential redaction.

## Key design decision

Adopt OMP's **gateway boundary**, Pi/OMP's **explicit API discriminator**, and OpenCode's evidence that subscription auth can require a dedicated request interceptor. Do not copy their credential files or collapse everything into one OpenAI-compatible adapter.

This matches the QuidProQuo harness principle used for this analysis: the API is the base layer, while CLI/SDK/proxy are wrappers; reliability comes from deterministic protocol, auth, guards, state, and observability in the harness rather than from treating a model endpoint as interchangeable.
