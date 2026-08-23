# M13: Extensible external coding CLI runtimes

## Outcome

Let users select OpenCode, Pi, or OMP beside Claude Code and Codex CLI without replacing or
embedding them underneath Rivumi's native harness. Rivumi Agent remains independently implemented;
external CLIs remain separately owned agent loops reached through a normalized runtime boundary.

## Non-negotiable architecture boundary

```text
Rivumi
├── Native harness: Rivumi Agent
│   └── ModelProvider: OpenAI / Anthropic / Gemini / compatible APIs / local models
└── External coding CLI runtimes
    ├── Claude Code
    ├── Codex CLI
    ├── OpenCode
    ├── Pi
    └── OMP (Oh My Pi)
```

- Native mode: Rivumi owns the loop, tools, context, approvals, session state, compaction, and
  verification. A model API is only a provider transport.
- External mode: the selected CLI owns the loop, model interaction, internal tools, and native
  session. Rivumi must not wrap it in a second model-driven loop.
- OpenCode, Pi, and OMP may inform Rivumi's design, but none is a runtime dependency of Rivumi
  Agent.

## Runtime contract

Reuse and generalize the existing provider-neutral `ConversationRuntimeSession` port rather than
creating a parallel `CodingCliAdapter` hierarchy. It must cover:

- executable discovery and version/capability negotiation;
- start, send turn, cancel, close, and supported resume behavior;
- streamed assistant text, correlated tool lifecycle, diffs, approval requests, usage, completion,
  and classified errors;
- runtime/model switching and the context boundary it creates;
- bounded stdout/stderr/frame handling and fail-closed protocol validation;
- vendor IDs retained privately by the adapter and Rivumi-owned IDs exposed to the controller.

Machine-protocol preference order:

1. documented versioned SDK or RPC;
2. ACP or an equivalent structured agent protocol;
3. documented JSON/JSONL event stream;
4. bounded stdin/stdout subprocess protocol;
5. PTY rendering only as an explicit degraded integration, never the default contract.

## Capability model

Do not claim feature parity. Each adapter reports whether it supports:

- streaming text;
- correlated tool start/result events;
- interactive approval and denial;
- audited edit/diff reporting;
- multi-turn native sessions and resume;
- model discovery/switching;
- MCP or extension visibility;
- cancellation acknowledgement;
- token/cost/plan usage reporting.

The UI hides or labels unsupported controls instead of emulating guarantees the CLI does not
provide.

## Authentication and billing boundary

- The child CLI owns its login and credential storage.
- Rivumi may report installation and an adapter-provided readiness check; it must not infer a valid
  subscription merely from an executable or credential file being present.
- Rivumi never reads, copies, refreshes, proxies, or converts another CLI's OAuth credential into
  native model-provider access.
- Subscription, extra-usage, and per-token API billing must be documented per CLI from current
  upstream policy. An external CLI login never becomes a generic `ModelProvider` credential.

## Workspace and security boundary

- Reuse the long-lived disposable committed-HEAD conversation workspace and independent patch
  audit established for Claude Code and Codex.
- Send only the isolated workspace to the child runtime; preserve source-worktree integrity,
  allowed-path, symlink/binary/size, Git-control, secret-environment, and verification invariants.
- Mediate approvals at the real tool boundary when the protocol supports it. If a runtime cannot
  expose a reliable approval boundary, label and constrain that adapter rather than inventing one.
- Unknown protocol/tool/approval frames, incomplete tool correlation, output overflow, cancellation
  timeout, and unaccounted workspace changes fail closed.

## Delivery slices

### Slice 1: Extract the common port

- Refactor the current Codex app-server and Claude Agent SDK implementations behind the existing
  `ConversationRuntimeSession` contract and extend that contract only where a shared capability is
  proven necessary.
- Preserve behavior with fake-runtime tests before adding a new CLI.
- Add a capability matrix consumed by the controller and TUI.
- Generalize the current Claude/Codex-only runtime options and dispatch in `src/rivumi/cli.py` and
  the durable runtime schema in `src/rivumi/conversation.py`, with migration/backward-compatibility
  coverage.

### Slice 2: OpenCode

- Validate the current documented SDK/server/ACP/JSON boundary and pin a minimum compatible
  version.
- Prefer a structured long-lived interface and retain all OpenCode-specific identifiers internally.
- Prove conversation, tools, approval/cancellation behavior, and patch reconciliation separately.

### Slice 3: Pi

- Validate Pi's supported programmatic/event interface and extension lifecycle.
- Implement only capabilities backed by stable upstream contracts; do not depend on its private
  credential files or turn a subscription transport into Rivumi native model access.

### Slice 4: OMP

- Confirm OMP means Oh My Pi and record its exact divergence from upstream Pi before coding.
- Reuse the Pi adapter only where protocol/version evidence proves compatibility; otherwise keep a
  separate adapter and capability declaration.

### Slice 5: Product surface and evidence

- Add installed runtime choices beside Rivumi Agent, Claude Code, and Codex CLI.
- Add actionable unavailable/not-authenticated/protocol-version messages.
- Run fake-CLI contract suites for every adapter and opt-in live smokes only where the installed
  runtime and its supported login are available.
- Record exact versions, commands, event coverage, policy sources, limitations, and artifacts in
  the M13 stage report.

## Explicitly out of scope

- Using OpenCode, Pi, or OMP as Rivumi Agent's underlying harness.
- Sharing or importing another CLI's credentials into Rivumi native mode.
- Advertising a subscription as a model API.
- Hiding nested-agent execution behind a generic provider name.
- Claiming identical approvals, resume, MCP, costs, or tool visibility across runtimes.
