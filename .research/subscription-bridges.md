# Claude Code / OpenAI Codex CLI subscription bridges

Research date: 2026-08-21 (Asia/Taipei)

> Superseded design note: this was the first bridge hypothesis. M2 did **not** adopt the
> `ExternalAgentBackend` or launch either official CLI. Follow-up source research in
> `.research/provider-bridge-comparison.md` established that OMP/OpenCode/Pi normally implement
> model transports behind their own loop, so the shipped design uses explicit provider protocols,
> an optional translation gateway, and an app-owned experimental Codex OAuth grant.

## Executive decision

`python-coding-agent` **must not read, copy, refresh, persist, or forward either CLI's OAuth/access tokens**. If subscription-backed local execution is added, the installed official CLI must remain the credential owner and be invoked through a documented machine interface.

The bridge also **must not implement the existing `ModelProvider.complete()` contract**. Claude Code and Codex expose a complete coding-agent loop (model, tools, approvals, sessions, and events), not a raw completion endpoint backed by a consumer subscription. Wrapping either one as a `ModelProvider` would create two nested agent/tool loops and would falsely imply that subscription OAuth is an API credential.

Recommended architecture:

1. Keep the current `ModelProvider` + bounded Python tool loop for API-key providers.
2. Add a separate, opt-in `ExternalAgentBackend` that receives one bounded task and streams normalized agent events.
3. For Codex MVP, use `codex exec --json`; add `codex app-server` only when bidirectional approvals or long-lived sessions are actually required.
4. For Claude, use the official Python `claude-agent-sdk` where its authentication/terms fit; otherwise use the installed `claude -p` stream-JSON subprocess for a local/private bridge. In both cases, the CLI owns authentication.
5. Treat Claude subscription use as **local/private and non-productized unless Anthropic approval is confirmed**. Anthropic's official Agent SDK overview says third-party developers may not offer claude.ai login or rate limits in their products without prior approval.

## Evidence and boundary

No credential file, keychain item, token value, or credential-bearing environment variable was read. Commands were limited to executable discovery/version/help, official installed package metadata/types, generated Codex protocol schemas in `/tmp`, repository contracts, and public official pages fetched through `stealth_fetch`.

Installed executables inspected:

| CLI | Resolved entry point | Version |
|---|---|---|
| OpenAI Codex | `/opt/homebrew/bin/codex` -> official npm package `@openai/codex` | `codex-cli 0.147.0` |
| Claude Code | `/Users/xiaoxu/.local/bin/claude` -> native Anthropic binary | `2.1.237` |

Official local package evidence:

- `/opt/homebrew/lib/node_modules/@openai/codex/package.json` identifies `@openai/codex` 0.147.0, Apache-2.0, with repository `openai/codex`. Its launcher spawns the versioned native binary.
- `/opt/homebrew/lib/node_modules/oh-my-claude-sisyphus/node_modules/@anthropic-ai/claude-agent-sdk/package.json` identifies official `@anthropic-ai/claude-agent-sdk` 0.1.77. Its public type declarations define `query()`, sessions, message/control unions, permissions, and a subprocess transport. The SDK source constructs `--output-format stream-json --input-format stream-json` and spawns a Claude Code executable.
- A local `claude-code-source` checkout was found, but it is an unofficial reverse-engineered repository and is older than the installed binary. It was not treated as normative evidence.

Public official sources:

- [OpenAI Codex non-interactive mode](https://developers.openai.com/codex/noninteractive) documents `codex exec`, JSONL events, structured final output, `--ephemeral`, resume, and that saved CLI authentication is reused by default.
- [OpenAI Codex app-server](https://developers.openai.com/codex/app-server) documents the JSON-RPC-like protocol, stdio JSONL transport, initialization, threads/turns, approvals/events, and version-specific schema generation. It also marks WebSocket transport experimental and recommends the Codex SDK for ordinary automation/CI.
- [OpenAI Codex authentication](https://developers.openai.com/codex/auth) distinguishes ChatGPT subscription access from API-key billing, documents cached-login reuse/refresh, and warns that credential files contain access tokens.
- [Anthropic Agent SDK overview](https://platform.claude.com/docs/en/agent-sdk/overview) describes the SDK as the Claude Code agent loop exposed to Python/TypeScript, suggests CLI `-p --output-format json` for other languages, and states the third-party claude.ai-login restriction.
- [Anthropic Agent SDK Python reference](https://platform.claude.com/docs/en/agent-sdk/python) documents `query()`, `ClaudeSDKClient`, streaming messages, interrupts, permissions, sessions, `cli_path`, and a newline-JSON transport to the CLI process.

The public pages and installed versions can drift. Adapters must probe the executable version and validate the event/protocol shape at runtime rather than assuming this snapshot indefinitely.

## Formal machine interfaces

### OpenAI Codex

#### 1. `codex exec --json` — recommended MVP

This is the stable, documented non-interactive automation surface.

Relevant installed help:

- prompt as argv or stdin (`codex exec -`)
- `--json`: JSONL event stream on stdout
- `--output-schema FILE`: structured final answer
- `--output-last-message FILE`: final message artifact
- `--ephemeral`: do not persist session rollout files
- `--ignore-user-config`: skip config while auth still uses `CODEX_HOME`
- `--ignore-rules`: skip user/project exec-policy rules
- `--sandbox read-only|workspace-write|danger-full-access`
- `--ask-for-approval never|on-request|untrusted`
- `-C DIR`, `--add-dir DIR`, `exec resume`

Officially documented JSONL examples include `thread.started`, `turn.started`, `item.started`, `item.completed`, `turn.completed`, `turn.failed`, and `error`; item types cover agent messages, reasoning, command execution, file changes, MCP calls, searches, and plans.

Why it fits first: one process per task, easy timeout/cancellation, bounded parsing, no auth material crosses the adapter, and no long-lived daemon lifecycle. Limitation: it is not a bidirectional approval protocol; automation should use fail-closed approval settings.

Suggested safe baseline (conceptual, not executed here):

```text
codex exec --json --ephemeral --ignore-user-config --ignore-rules \
  --sandbox workspace-write --ask-for-approval never -C <disposable-workspace> -
```

Do not use `--dangerously-bypass-approvals-and-sandbox`.

#### 2. `codex app-server` — rich integration, higher cost

The installed CLI supports stdio by default plus Unix/WebSocket transports. `codex app-server generate-json-schema --out <tmp>` and `generate-ts` produced version-specific protocol artifacts without accessing auth.

The generated 0.147.0 contract confirms:

- lifecycle: `initialize` -> client `initialized`
- work: `thread/start` or `thread/resume` -> `turn/start` -> streamed notifications -> `turn/completed`
- control: `turn/interrupt`
- approvals: server requests for command execution, file changes, dynamic tool calls, MCP elicitation, permissions, and user input
- status/events: `thread/started`, `turn/started`, `item/started`, deltas, `item/completed`, token usage, errors, and account/rate-limit updates
- auth management endpoints exist, including account login/logout/read

The adapter should use `account/read` only for redacted capability/status. It must not call token-import/refresh variants or expose their schema fields in its public contract. User login should happen in the official CLI/browser flow outside a run.

App-server is appropriate only for a UI needing live approvals, steering, resume, or detailed item state. The command and WebSocket transport are explicitly experimental; prefer local stdio, generate schemas from the exact installed version, and fail closed on unknown required methods/fields.

#### 3. Other Codex commands

- `codex mcp-server` exposes Codex as an MCP server over stdio, but it is not a raw subscription completion endpoint and is not the clearest task-runner boundary for this harness.
- `codex exec-server` is explicitly experimental and includes remote environment/agent-identity concerns; it should not be the first local subscription adapter.
- `codex login --with-access-token` accepts a token on stdin. That is deliberately out of scope: the bridge must reuse CLI-owned saved login, never import a token.

### Claude Code

#### 1. Official Python Agent SDK — preferred typed Python integration when permitted

The official Python reference provides two useful levels:

- `query(...) -> AsyncIterator[Message]` for one-shot tasks
- `ClaudeSDKClient` for multiple exchanges, streaming input, `interrupt()`, session continuity, permission changes, and server information

It accepts `ClaudeAgentOptions` such as `cwd`, tools/allowed/disallowed tools, permission mode, session resume, output format, `cli_path`, settings sources, hooks, time/budget limits, and stderr handling. Its default transport is a Claude CLI subprocess; custom `Transport` is explicitly low-level and may change.

The installed TypeScript SDK declarations corroborate the wire contract:

- stdout: SDK messages plus `control_request`, `control_response`, cancel, and keepalive
- stdin: user messages plus control request/response and keepalive
- controls include initialize, interrupt, `can_use_tool`, permission mode, model, MCP status/messages, rewind, and MCP server updates
- message union includes assistant/user/result/system-init/stream events/status/hook/tool progress/auth status, all correlated by session IDs
- result messages include terminal subtype, duration, turns, usage, permission denials, result/errors, and session ID

For a Python project, use the official package rather than independently reverse-engineering every SDK message. Pin/test a compatible version, and point `cli_path` at the intended installed executable only when that behavior is covered by the chosen deployment/auth policy.

#### 2. `claude -p` stream JSON — documented headless subprocess

Installed `claude --help` formally exposes:

- `-p/--print` non-interactive execution
- `--input-format stream-json` and `--output-format stream-json`
- `--json-schema`, `--include-partial-messages`, `--include-hook-events`
- `--session-id`, `--resume`, `--continue`, `--fork-session`
- `--no-session-persistence`
- `--allowedTools`, `--disallowedTools`, `--tools`
- `--permission-mode` and `--max-budget-usd`
- `--safe-mode`, which disables user customizations but keeps auth, model, built-in tools, and permissions working

This is a suitable local process boundary if the SDK adds unnecessary coupling. Prefer `--safe-mode`, explicit tool/permission configuration, a disposable workspace, and no session persistence unless resume is a required feature. Do not use `--dangerously-skip-permissions`.

Do not use `--bare` for subscription login: installed help explicitly says bare mode never reads OAuth or keychain credentials. Do not invoke `claude setup-token`; although the command exists for a long-lived subscription token, making the bridge receive that secret would violate this design's credential boundary.

#### Claude subscription policy boundary

Technically, the normal installed CLI can own and reuse the user's subscription login (`claude auth login` defaults to `--claudeai`). That does **not** establish permission to ship subscription login as a third-party product feature. Anthropic's official Agent SDK overview says:

> Unless previously approved, third-party developers may not offer claude.ai login or rate limits for their products, including Agent SDK products.

Therefore:

- personal/local development: a feature-flagged adapter invoking the user's already-authenticated official CLI is technically testable without token reuse;
- distributed, hosted, or multi-user product: require Anthropic approval or use documented API-key authentication/Managed Agents instead;
- never describe a local technical success as general commercial authorization.

## Why this is not a `ModelProvider`

Current `ModelProvider.complete(messages, tools) -> ModelTurn` assumes the Python runner owns the loop: it sends canonical tool definitions, receives one model turn, executes exactly one bounded tool call set, records it, and repeats.

Both official bridges instead own:

- context construction and compaction;
- tool selection and execution;
- permission/approval flow;
- session persistence;
- agent loop termination;
- provider usage and rate-limit handling.

There is no documented subscription-backed raw-completion surface in either CLI. Pretending one exists would either discard the CLI's agent capabilities or let a second agent bypass the Python harness's exact tool/path policy.

Recommended new boundary (illustrative):

```python
class ExternalAgentBackend(Protocol):
    backend_name: str
    async def probe(self) -> BackendProbe: ...
    async def run(self, request: ExternalAgentRequest) -> AsyncIterator[ExternalAgentEvent]: ...
    async def interrupt(self, run_id: str) -> None: ...
    async def aclose(self) -> None: ...
```

`BackendProbe` should contain only non-secret facts: executable path, version, interface kind, login state enum (`ready`, `signed_out`, `unknown`), protocol version/schema hash, and capabilities. It must not contain email, account IDs, token strings, credential paths, or raw auth-status payloads.

`ExternalAgentRequest` should contain:

- harness run/task ID;
- absolute disposable workspace path and base commit;
- instruction and allowed path patterns;
- explicit sandbox and approval policy;
- permitted CLI tools/capabilities;
- wall-time, output-byte, turn/step, and budget limits;
- session policy (`ephemeral` by default; resume ID only when requested);
- final-output JSON Schema when supported.

Normalized `ExternalAgentEvent` kinds should be a closed union:

```text
session.started
turn.started
item.started | item.delta | item.completed
approval.requested | approval.resolved
usage.updated
rate_limit.updated
warning
turn.completed | turn.failed
process.exited
```

Each event needs backend/version, run/session/turn/item correlation IDs, monotonic sequence, timestamp, bounded redacted payload, and raw-event type for forward-compatible diagnostics. Unknown events may be recorded as bounded metadata, but unknown approval requests or terminal states must fail closed.

The terminal result should normalize status, summary/final text, usage, permission denials, provider terminal reason, session ID (if persistence was requested), process exit code, and protocol warnings. It should never claim verification success; the outer harness must still inspect the Git diff, reject paths outside `allowed_paths`, enforce patch-size limits, and independently rerun declared checks.

## Security and operational risks

1. **Credential adjacency.** A CLI subprocess needs access to its own cached login/keychain. Unlike the current direct-provider design, that same full agent may run repository commands. Never pass token environment variables; prefer OS credential-store login; keep repository verification subprocesses on the existing sanitized environment; use OS/container isolation for hostile repositories.
2. **Policy bypass by full-agent tools.** CLI sandbox/tool policies do not exactly equal `SafePathPolicy`. Use a disposable clone, least-privilege CLI sandbox, explicit allowed tools, outer diff/path validation, and independent checks. Reject out-of-scope changes even if the CLI reports success.
3. **Nested-agent semantic mismatch.** Do not translate CLI internal tool calls into `ModelTurn.tool_calls` and feed them to the existing loop.
4. **Protocol/version drift.** Pin minimum versions, probe `--version`, validate every line, cap line/event sizes, snapshot generated Codex schema per supported version, and retain fixture transcripts with no secrets.
5. **Deadlocks/backpressure.** Drain stdout and stderr concurrently, use bounded queues, close stdin deliberately, implement graceful interrupt then kill escalation, and treat malformed/truncated JSON as provider failure.
6. **Unattended approvals.** Never leave a headless process waiting for TTY input. MVP should deny/fail closed. Rich adapters must correlate each approval request and enforce a timeout/default deny.
7. **Config/plugin injection.** Claude project/user settings and Codex config/rules can add hooks, MCP servers, or instructions. Use Claude safe mode/explicit settings sources and Codex ignore-user-config/ignore-rules where compatible with the desired auth path. Log effective non-secret capability choices.
8. **Session leakage.** Default to ephemeral/no persistence. Session IDs are not credentials, but transcripts can contain source and prompt data; store them only under the run artifact policy.
9. **Cost/rate limits.** A subprocess smoke test can consume subscription allowance or API spend. Keep live tests opt-in and label which auth mode billed the run without logging account identity.
10. **Terms/branding.** Anthropic's third-party subscription restriction is a product blocker until approved. Technical token isolation does not resolve contractual permission.

## Verification strategy

All default CI tests can remain offline and credential-free.

### Shared contract tests

- Fake executable fixtures emit deterministic JSONL, partial lines, oversized lines, stderr, non-zero exits, malformed JSON, unknown events, delayed approval, and signal termination.
- Assert argv is a list (no shell), cwd is the disposable workspace, timeouts kill the process group, stderr is bounded, and events never serialize environment/config/auth payloads.
- Assert diff/path/verification gates remain authoritative after an external agent exits successfully.

### Codex tests

- Probe test: `codex --version`, `codex exec --help`, and optionally `codex login status` through a redactor that returns only a login-state enum.
- Schema compatibility test: run `codex app-server generate-json-schema --out <tmp>` and hash/validate required lifecycle and approval types.
- Fake `codex exec --json` transcript test for documented event kinds.
- Fake app-server JSONL test: initialize, initialized, thread/start, turn/start, approval/default-deny, turn/completed, interrupt.
- Opt-in live smoke test: temporary Git repository, read-only/minimal task, ephemeral session, strict time/output cap. Never run it in ordinary CI.

### Claude tests

- Prefer official `claude-agent-sdk` Python message classes in adapter tests; inject a fake/custom transport rather than authenticating.
- Direct-process fixtures should cover system-init, assistant/result, `control_request can_use_tool`, control response, interrupt, auth failure, budget/turn terminal errors, and session correlation.
- Probe test: `claude --version` and help only. If auth status is checked, parse only boolean/state and discard the raw response immediately.
- Opt-in live smoke test only for a developer-owned local setup whose Anthropic usage is authorized; no default CI subscription test.

## Suggested delivery order

1. Introduce provider-vs-external-agent orchestration boundary and offline fake backend.
2. Add `CodexExecBackend` using subprocess JSONL, ephemeral runs, fail-closed approvals, and outer verification.
3. Add `ClaudeAgentBackend` behind an explicit local/experimental flag, preferably with `claude-agent-sdk`; document the Anthropic approval boundary in configuration/UI.
4. Add app-server only after a concrete requirement for steering, approvals, or resumable rich sessions.
5. Before any hosted/multi-user Claude subscription offering, obtain an explicit policy/legal answer from Anthropic; otherwise support API-key billing instead.

## Bottom line

- **Yes:** attach to official CLIs/official SDK transport and let them own subscription login.
- **No:** reuse OAuth tokens, emulate refresh, import token files, or expose subscription credentials to the Python process.
- **No:** present a full CLI agent as a raw `ModelProvider`.
- **Codex first interface:** `codex exec --json`; app-server later for rich clients.
- **Claude first interface:** official Python Agent SDK or `claude -p` stream JSON, but only within the documented authorization boundary; third-party subscription productization remains unapproved by default.
