# Repository modularization plan

Date: 2026-09-05
Status: proposed
Scope: decompose `tui.py`, `cli.py`, `loop.py`, `tools.py`, and
`codex_app_server.py` without changing the M11 unified-conversation product model or
the native/external runtime trust boundary.

## Decision

Looplane should move from a flat collection of large modules to feature packages with
explicit ports. The objective is change locality and testability, not a cosmetic line
count target. Mature reference agents still contain large coordinators; their useful
property is that terminal capabilities, protocol transport, approval, tools, and
session state have named owners around those coordinators.

Run the required refactor in two waves, with one explicitly conditional follow-up:

1. Stabilize the external-runtime seam and product entry points before further M13
   adapter work: Codex protocol session, CLI composition, and terminal UI boundaries.
2. Decompose the native harness: tool execution first, then `AgentRunner` orchestration.
3. Only after the Python process-execution contract is stable, decide whether a Rust
   execution sidecar has measured security, portability, or performance value.

Each slice is a separate behavior-preserving commit. Do not combine module moves with
new product features, permission changes, or provider behavior changes.

## Current evidence

| Current owner | Size | Concentration |
|---|---:|---|
| `looplaneApp` in `tui.py` | 3,188 lines, 119 methods | layout, input precedence, configuration, conversation/session state, event projection, approvals, interruption, export |
| command functions in `cli.py` | 3,261-line module | Typer declarations, auth/configuration, provider construction, runtime factories, TUI/headless dispatch, sessions, servers |
| `AgentRunner` in `loop.py` | 2,703 lines, 65 methods | run state, retry, context assembly, approvals, checkpoints, tool scheduling, subagents, verification, completion |
| `ToolExecutor` in `tools.py` | 1,531 lines, 35 methods | definitions, MCP bridge, filesystem/search, patching, Git, verification, transactions, dispatch |
| `CodexAppServerSession` | 1,399 lines, 63 methods | subprocess/RPC, frame handling, event mapping, approvals, ID correlation, process termination |

The current baseline has eight Ruff violations outside the recently changed TUI files.
Repair those in an independent prerequisite commit so every extraction starts from a
green canonical lint gate. The full pytest suite was green when this plan was written;
re-establish that fact at the start of implementation.

Looplane is not starting from zero. It already has three different contracts at three
different abstraction levels:

| Existing seam | Meaning | Decision |
|---|---|---|
| `ConversationRuntimeSession` in `conversation_runtime.py` | one live multi-turn runtime with canonical events, approval, interruption, compaction, and close | keep and evolve; this is the existing Pi-like session boundary |
| `ExternalAgentBackend` in `backends.py` | one bounded whole-task delegation to another agent | preserve as a compatibility name; make `ExternalAgentRunner` the canonical semantic name |
| `run_bounded_command` and `CommandSandbox` in `runtime.py` | low-level local process capture, cancellation, environment, and sandbox launch | extract behind a process-execution port before considering another implementation language |

`ConversationController`, `runtime_registry`, and `runtime_semantics` are also valid
seams. The refactor should relocate or narrow them, not replace them with parallel
`AgentSession`, `CodingCliAdapter`, or generic execution hierarchies.

## What to borrow from the local references

The checkouts under `/Users/xiaoxu/Projects/coding-agent-reference` support these
bounded choices:

Evidence was checked against local snapshots Codex `88f776588f5e`, OpenCode
`10765ff2a9da`, Pi `853a80d26c90`, and reconstructed Claude Code
`83b3ecd74976`. Re-check moving references before implementation.

| Reference | Useful boundary | Do not copy blindly |
|---|---|---|
| Codex | separate app-server protocol and raw transport crates; `core/agent`, `core/session`, `core/tools/handlers`; feature-oriented TUI modules | connection policy and request routing still live above raw transport; directory count alone does not prove low coupling |
| OpenCode | feature packages for `agent`, `provider`, `session`, `tool`, `permission`; separate CLI, server, protocol, and TUI packages | this snapshot contains old and new architectures together, and TUI/CLI still import core directly; it is migration-direction evidence, not a settled dependency graph |
| Pi | low-level `Agent`, product-level shared `AgentSession`, mode-specific I/O, reusable TUI, and explicit remote protocol | coding-agent RPC uses JSONL while the separate remote-session protocol uses length-prefixed CBOR; do not conflate them or merely rename a monolith `AgentSession` |
| Oh My Pi | explicit Rust/native and TypeScript package boundaries around platform capabilities | Native acceleration and package breadth are unnecessary for Looplane's current Python scope |
| Claude reconstructed source | renderer/selection under `ink`, product widgets under `components`, permissions and commands in feature directories | This is reconstructed Claude Code 2.1.88 source, not an official current Anthropic source release |

Local evidence anchors:

- Codex: `codex/codex-rs/app-server-protocol/src/lib.rs`,
  `app-server-transport/src/lib.rs`, `app-server/src/transport.rs`,
  `app-server/src/message_processor.rs`, and `exec-server/src/lib.rs`.
- OpenCode: `opencode/packages/core/src/session/runner/`,
  `core/src/pty/pty.node.ts`, `cli/src/tui.ts`, `server/src/routes.ts`, and
  `desktop/src/main/sidecar.ts`.
- Pi: `pi-mono/packages/agent/src/agent.ts`,
  `coding-agent/src/core/agent-session.ts`, `coding-agent/src/modes/rpc/jsonl.ts`, and
  the separate remote-session `protocol/src/framing.ts`.
- Claude reconstructed snapshot: `claude-code-source/src/cli/structuredIO.ts` and
  `src/replLauncher.tsx`; these are supporting observations only.

The common lesson is capability ownership. Clipboard code owns clipboard routing;
protocol adapters own vendor frames; approval features own approval presentation;
root applications coordinate these components without implementing all of them.

The references do not justify a Rust rewrite. Codex is a Rust workspace, OpenCode's
desktop sidecar hosts its local server, and Pi's client/server boundary remains
TypeScript. What transfers to Looplane is the protocol and ownership boundary. A
sidecar is only one possible implementation behind that boundary.

## Reconciliation with the proposed protocol/sidecar shape

Accept these parts of the proposed shape:

- session core is shared while TUI, headless, WebSocket, and SDK surfaces own only
  their input/output adaptation;
- vendor frames, canonical events, process execution, and sandbox policy are distinct
  responsibilities;
- UI and conversation controllers depend on ports and canonical events, never directly
  on subprocess, PTY, or Landlock implementation details;
- future local-process and sidecar process runners must pass the same contract suite.

Apply these Python- and Looplane-specific corrections:

- use `terminal/`, not `tui/`, while the public `tui.py` compatibility module exists;
  otherwise `looplane.tui` has a file/package import collision;
- reuse `ConversationRuntimeSession` and `ConversationController`; do not introduce a
  second `AgentSession` abstraction with the same lifecycle;
- keep model providers (`ModelProvider`) distinct from external coding runtimes
  (`ConversationRuntimeSession` / `ExternalAgentRunner`); a `providers/` package must
  not mix those two trust and billing models;
- keep framing with the runtime or transport that owns its wire format. Create a shared
  `protocol/` package only when a versioned cross-process schema has at least two real
  consumers; internal Python calls do not need framing;
- keep raw byte/line I/O, frame bounds, and process shutdown in transport, but keep
  initialization state, request routing, capability negotiation, and approval policy
  in the session/application layer;
- call a future low-level process port `ProcessRunner` and its live handle
  `RunningProcess`; create that Protocol only when a second implementation or a real
  substitution boundary exists.

## Naming convention

Names describe responsibility and lifecycle. `Backend` is not a target architecture
term in Looplane; the fact that a component sits behind another component is not its
domain meaning.

| Current or provisional name | Canonical target name | Meaning |
|---|---|---|
| `ExternalAgentBackend` | `ExternalAgentRunner` | delegates one bounded task to an external coding agent |
| `StreamJsonCliBackend` | `StructuredCliRunner` | runs a CLI that produces structured events |
| `CodexCliBackend` | `CodexCliRunner` | delegates a task through Codex CLI |
| `ClaudeCodeBackend` | `ClaudeCodeRunner` | delegates a task through Claude Code |
| `OpenCodeBackend` / `PiBackend` / `OmpBackend` | `OpenCodeRunner` / `PiRunner` / `OmpRunner` | vendor-specific task runners |
| `BackendTurnLimiter` | `TurnLimiter` | bounds concurrent conversation turns |
| provisional `ProcessExecutionBackend` | `ProcessRunner` | starts and controls one process through a `RunningProcess` handle |
| provisional `PythonSubprocessBackend` | `LocalProcessRunner` | local Python subprocess implementation |
| provisional `RustSidecarExecutionBackend` | `SidecarProcessRunner` | optional sidecar process implementation |

Use `Session` for long-lived multi-turn state, `Runner` for executing a bounded unit of
work, `Transport` for moving bytes or frames, `Mapper` for schema conversion, `Store`
for persistence, `Launcher` for OS/process startup, and `Controller` for application
use-case coordination.

Existing `*Backend` imports remain compatibility aliases during migration. Rename them
in a dedicated compatibility slice, not in the same commit as moving implementation
code. Do not add new canonical classes or modules with `Backend` in their name.

## Target package shape

```text
src/looplane/
├── cli.py                         # compatibility entry point and Typer app only
├── commands/
│   ├── chat.py                    # chat/headless route selection
│   ├── sessions.py
│   ├── auth.py
│   ├── plugins.py
│   ├── serve.py
│   └── bootstrap.py               # factories and lazy construction
├── terminal/
│   ├── app.py                     # Textual composition and input state machine
│   ├── types.py
│   ├── events.py
│   ├── status.py
│   ├── approvals.py
│   ├── transcript.py
│   ├── selectors.py
│   ├── onboarding.py
│   ├── projection.py
│   ├── conversation_binding.py    # UI subscription and generation fence only
│   ├── clipboard.py
│   └── links.py
├── runtimes/
│   ├── structured_cli.py          # shared StructuredCliRunner mechanics
│   └── codex/
│       ├── conversation.py        # isolated workspace and patch-audit host
│       ├── session.py             # public runtime-session implementation
│       ├── task_runner.py         # bounded CodexCliRunner delegation
│       ├── transport.py           # process, JSON-RPC, stderr, shutdown
│       ├── event_mapper.py         # vendor frame to canonical event mapping
│       ├── approval_mapper.py
│       └── correlation.py         # vendor/local turn and action IDs
├── execution/                     # extracted from runtime.py after characterization
│   ├── runner.py                  # ProcessRunner / RunningProcess when justified
│   ├── local_process.py           # initial Python subprocess implementation
│   ├── events.py                  # process events, not agent events
│   └── cancellation.py
├── sandbox/
│   ├── policy.py                  # normalized roots and enforceable OS policy
│   ├── launcher.py                # platform selection only
│   ├── macos.py
│   └── linux.py                   # bwrap/Landlock adapter boundary
├── workspace/
│   ├── local_git.py
│   └── conversation.py            # disposable clone and patch audit
├── tooling/
│   ├── executor.py                # thin registry/dispatch coordinator
│   ├── definitions.py
│   ├── mcp_bridge.py
│   ├── filesystem.py
│   ├── search.py
│   ├── patching.py
│   ├── git.py
│   ├── verification.py
│   └── transactions.py
├── agent/
│   ├── runner.py                  # run state machine and orchestration only
│   ├── run_lifecycle.py           # bounded-run state/persistence facade
│   ├── state.py
│   ├── checkpoints.py
│   ├── context.py
│   ├── model_calls.py
│   ├── tool_scheduler.py
│   ├── subagent_dispatch.py
│   ├── verification.py
│   └── completion.py
├── contracts.py                   # framework-neutral shared domain types
├── conversation_runtime.py        # retained canonical live-runtime contract initially
├── runtime_semantics.py           # retained semantic policy contract initially
├── external_agents.py             # canonical ExternalAgentRunner contract
└── backends.py                    # temporary compatibility re-exports only
```

Existing `looplane.tui`, `looplane.loop`, `looplane.tools`, and
`looplane.codex_app_server` remain temporary compatibility facades. Internal feature
modules import canonical packages, never these facades. The new names avoid Python's
file-versus-package collision during migration.

## Dependency rule

```text
commands/ ────────┬──> terminal/
                  ├──> agent/
                  └──> runtimes/

terminal/ ───────────> conversation/runtime contracts
runtimes/* ──────────> conversation/runtime contracts
agent/ ──────────────> tooling ports + model-provider/domain/session contracts
tooling/ ────────────> execution/ + domain/policy/MCP primitives
execution/ ──────────> sandbox policy/launcher ports
sandbox/ ────────────> platform primitives only

domain and policy modules never import commands/, terminal/, or vendor runtimes.
```

Use narrow Protocols or returned commands/events across boundaries. Do not introduce
mixins that continue reaching into dozens of private `self` fields.

## Wave 0: trustworthy baseline

### Slice 0.1 — restore green gates

- Fix the eight current Ruff findings in a standalone commit.
- Run `uv run ruff check .` and `uv run pytest -q`.
- Record startup/import and package-build baselines.

### Slice 0.2 — enforce architecture constraints

- Add a small AST/import test forbidding feature packages from importing compatibility
  facades or higher layers.
- Enforce that canonical conversation events have one owner, vendor transports do not
  import TUI/CLI/controllers, `tooling/` does not import `agent/`, and `agent/` does not
  import concrete model-provider implementations.
- Record the production import graph. Do not add new strongly connected components;
  Wave 2 must remove the existing `loop.py` ↔ `subagents.py` cycle.
- Add one checked mapping between registry/discovery capabilities and the live
  `RuntimeCapabilities` reported by a constructed session; do not add a third
  capability representation.
- Consolidate the duplicate native `RunEvent` sink Protocols in `console.py` and
  `sdk.py`, while retaining separately typed conversation and external-agent sinks.
- Add smoke imports for public compatibility names.
- Add an sdist listing check so generated dependencies, `.work`, local caches, and old
  package names cannot enter release archives.

Exit condition: lint and tests are green, intended imports are acyclic, and a clean
sdist is bounded and rooted at `looplane-*`.

## Wave 1: stabilize product entry points and M13 seams

### Slice 1.1 — pure contracts and leaf helpers

- Extract TUI request/event types and status formatting without importing the App.
- Extract Codex safe-ID, bounded parsing, tool status/summary, and decision mapping.
- Extract tool value types and definitions while re-exporting existing public names.
- Introduce the semantic `ExternalAgentRunner` contract and runner class/module names.
  Keep `ExternalAgentBackend` and existing `*_backend.py` imports as compatibility
  aliases; do not mix their eventual removal with implementation extraction.

### Slice 1.2 — Codex runtime protocol

- Extract ID correlation and bounded frame parsing first.
- Extract notification/item mapping into canonical conversation events.
- Extract approval request/result conversion.
- Leave subprocess start/RPC/read loop/close in a transport-owned session shell.
- Preserve `IsolatedCodexConversation` as the workspace/audit host around that session;
  transport code must not own disposable-clone integrity or patch reconciliation.

Risk: protocol drift, out-of-order frames, stale turn IDs, output bounds, and shutdown
races. Use recorded frame sequences and unknown-frame fail-closed tests.

### Slice 1.3 — CLI composition

- Move plugin, auth, session, policy, and server commands into command modules.
- Centralize provider/native/external runtime construction in `commands/bootstrap.py`.
- Reduce `cli.py` to Typer registration, argument declarations, and lazy dispatch.
- Preserve the public `looplane.cli:app` entry point.

Risk: M12 startup performance and optional-dependency loading. Every slice runs lazy
import tests and the startup regression script.

### Slice 1.4 — terminal feature widgets

- Move the complete approval policy/modal/inline cluster together.
- Move composer, scroll, transcript/tool widgets, selectors, and status by feature.
- Move `OnboardingModal` as one unit before separating provider/model loading; partial
  movement risks dismissed-screen worker races.
- Move the existing clipboard and link modules under `terminal/`, leaving re-exports.

Risk: Textual focus and message routing. Characterize Enter, arrows, number keys,
Escape, selection copy, resize, and unmount behavior before each move.

### Slice 1.5 — terminal controllers

- Convert runtime event handlers into a projection service returning explicit view
  commands.
- Keep turn, approval, context injection, and compaction lifecycle in the existing
  `ConversationController` and durable state in `ConversationStore`.
- Move only UI subscription, resource cleanup, generation fencing, and view-command
  projection into `terminal/conversation_binding.py`.
- Keep `terminal/app.py` responsible for composition and one explicit input-precedence
  state machine.

Risk: stale async writers and generation fencing. Cancellation alone is not a write
fence; existing generation/lease checks must remain deterministic.

Wave 1 exit condition: adding an M13 external runtime requires one runtime package,
one registry entry, and capability tests—not edits across a 3,000-line CLI or TUI.

## Wave 2: decompose the native harness

### Slice 2.1 — tool definitions and adapters

- Extract built-in definitions and MCP bridge discovery.
- Keep definitions declarative; execution policy does not live in model-facing text.

### Slice 2.2 — filesystem, search, and patch operations

- Extract bounded file walking/read/search.
- Extract unified-diff validation, exact replacement, rollback, and snapshots.
- Keep path containment and symlink checks in shared deterministic guards.

### Slice 2.3 — Git, verification, and transactions

- Extract Git command boundaries and reviewable patch generation.
- Extract named verification command execution and structured transaction rollback.
  This layer executes an already-authorized check and returns evidence; it does not
  decide when the agent should verify.
- Reduce `ToolExecutor` to registry, dependency ownership, and dispatch.

Risk: no slice may broaden allowed paths, commands, timeouts, output bounds, or
rollback semantics. Security and contract tests are the gate.

### Slice 2.4 — runner state, checkpoints, and context

- Extract state restoration, manifest/checkpoint writing, and event emission.
- Extract context-pressure, workspace, IDE, instruction, and project-context assembly.
- Return explicit context additions instead of mutating runner messages from helpers.
- Separate the low-level turn engine from the bounded-run lifecycle facade. Pi's
  `Agent`/`AgentSession` split is the reference concept, but Looplane should name these
  by its own semantics instead of creating another generic session type.

### Slice 2.5 — model calls and tool scheduling

- Extract retry/backoff/cancellation and usage/cache accounting.
- Extract prepared calls, concurrent read-only batches, fingerprint guards, and
  subagent dispatch behind a scheduler port.
- Make `AgentRunner` depend on a scheduler Protocol or callback and remove the current
  `loop.py` ↔ `subagents.py` production import cycle.

### Slice 2.6 — verification and completion

- Extract agent-level verification/review-lane policy, orchestration, and result
  persistence. This layer decides which checks run and interprets their evidence.
- Extract final status/result assembly.
- Leave `AgentRunner.run()` as the visible state machine coordinating explicit services.

Risk: session resume, approval reconciliation, cancellation, deadlines, workspace
integrity, parallel result ordering, and checkpoint ordering. Build sequence fixtures
before extracting each transition.

Wave 2 exit condition: `AgentRunner` describes the loop; it does not implement every
context source, tool detail, retry implementation, and persistence mechanism.

## Conditional Wave 3: process runner and possible Rust sidecar

First extract and stabilize the Python-owned process functions and data models. Do not
create a Protocol merely to complete the directory shape. If a second implementation,
test substitution point, or cross-process boundary appears, introduce this lower-level
contract; it must not carry model, conversation, tool, approval, or provider semantics:

```python
class ProcessRunner(Protocol):
    async def start(self, request: ProcessRequest) -> RunningProcess: ...

class RunningProcess(Protocol):
    def events(self) -> AsyncIterator[ProcessEvent]: ...
    async def cancel(self) -> None: ...
    async def wait(self) -> ProcessResult: ...
```

Until that abstraction is justified, keep a semantic `run_local_process()` function.
If the Protocol is introduced, wrap the same behavior as `LocalProcessRunner` without
changing policy. Before considering Rust, contract tests must cover process-group
termination, stdout/stderr bounds, incremental UTF-8 decoding, backpressure,
timeout/cancel races, environment profiles, and all macOS/Linux sandbox fail-closed
behavior.

Add `SidecarProcessRunner` only if measurements show a concrete benefit that
cannot be achieved cleanly in Python—for example a stronger independently audited
launcher boundary, a PTY portability requirement, or a demonstrated process-control
bottleneck. If added, write a separate ADR covering protocol version negotiation,
request/event schemas, maximum frame size, correlation IDs, crash recovery, binary
distribution, platform support, and Python fallback behavior. A shared `protocol/`
package becomes justified at that point because the schema crosses a process and
language boundary.

Rust must remain below the process interface. It does not own the agent loop,
conversation state, model providers, permissions, verification policy, or TUI. This
wave is not required for the current modularization or M13.

## Test layout migration

```text
tests/
├── terminal/
├── commands/
├── runtimes/codex/
├── execution/
├── sandbox/
├── workspace/
├── tooling/
├── agent/
├── contracts/
└── integration/
```

- Move tests in the same slice as production features.
- Keep cross-component state-machine tests in `integration/`.
- Prefer recorded protocol frames, fake clocks/runners, and filesystem fixtures over
  real providers.
- Retain real PTY, Linux sandbox, package-build, WebSocket, and opt-in provider smokes
  as separate evidence layers.

## Per-slice verification

Every behavior-preserving slice must pass:

1. focused tests for the moved feature;
2. import-boundary and compatibility-import tests;
3. `uv run ruff check .`;
4. `uv run pytest -q`;
5. startup/lazy-import checks when CLI or TUI imports change;
6. package build and archive listing when public paths or manifests change;
7. Cloudflare/VS Code checks only when their consumed Python/API contract changes.

Live provider execution is unnecessary for a pure move. It becomes necessary when
wire behavior, environment forwarding, authentication, or cancellation changes.

## Success metrics

- A feature module has one primary reason to change and no imports back to a facade.
- Root App/Runner/Executor classes coordinate typed services instead of sharing their
  private state with mixins.
- Adding a runtime, tool, or terminal capability touches its package plus registry and
  tests, not all five former monoliths.
- Focused tests run without importing every provider or starting a real subprocess.
- Public entry points and serialized session/conversation formats remain compatible.
- Repository-wide lint, tests, startup checks, and bounded builds are green at every
  merge point.

Line count may fall substantially, but it is an observation rather than the acceptance
criterion.

## Explicit non-goals

- No Textual, language, or framework rewrite.
- No Rust crate or sidecar in Wave 1 or Wave 2.
- No revival of the pre-M11 Ask/Agent product split.
- No change to permission, sandbox, credential, billing, or provider ownership.
- No simultaneous redesign of M13 runtime capabilities.
- No simultaneous split of the large `models.py` provider implementations. That is
  real deferred debt, but model-provider adapters must remain separate from external
  coding-runtime adapters and deserve their own plan.
- No mega-PR or bulk path move without compatibility re-exports.

## Related analysis

- [TUI modularization audit](../diagnoses/tui-modularization-audit.md)
- [Coding agent TUI architecture comparison](../research/2026-09-05-coding-agent-tui-comparison.md)
- [M13 external coding CLI adapters](m13-external-coding-cli-adapters-plan.md)
- [模型只是元件，harness 才是系統](https://quidproquo.cc/posts/ai/2026-08-10-model-component-harness-system)
- [CS146S Week 5：agent 就緒度是可以量的](https://quidproquo.cc/posts/ai/2026-08-16-cs146s-agent-ready-codebase)
