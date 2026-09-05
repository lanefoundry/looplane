# Coding agent server architecture comparison

Date: 2026-09-05  
Status: research reference  
Scope: compare multi-session, persistent-service, client/API, and workspace
boundaries in established coding agents. This document informs a future Looplane
milestone; it is not an implementation commitment and does not change the M11 unified
conversation model.

## Conclusion

OpenCode is not the only coding agent moving toward a server-centric architecture.
OpenHands, Codex, Claude Code, Pi, Goose, and Oh My Pi expose different subsets of the
same shape:

```text
TUI / CLI / Desktop / SDK
             |
      protocol or API
             |
     session service
             |
  conversation / agent runtime
             |
   workspace and execution
```

No reviewed product should be treated as a complete template. The useful patterns are:

- OpenHands for Python package, server, client, and workspace separation.
- Codex for Thread/Turn/Item lifecycle and a versioned application protocol.
- Claude Code for choosing between shared-directory and per-session worktree modes.
- OpenCode for a detached local service and multi-session product surfaces.
- Pi for a small transport-neutral session server, client, leases, and protocol.
- Goose and Oh My Pi for narrower server, RPC, and live-collaboration patterns.

Looplane currently has durable conversation records, isolated conversation workspaces,
a single-session WebSocket attach surface, and a Python SDK facade. It does not yet
provide a service-owned multi-session runtime, TUI tabs, reconnectable live sessions,
or a conversation-management HTTP API.

## Capability vocabulary

These capabilities must be evaluated separately:

| Capability | Required evidence |
|---|---|
| Durable session record | transcript and metadata survive UI or process exit |
| Live persistent session | an active or idle runtime remains owned by a service after a client disconnects |
| Multi-session execution | different sessions may make progress concurrently while each session preserves ordering |
| Multi-client attachment | more than one client can observe or control one session under an explicit ownership policy |
| Multi-session UI | the shipped client exposes tabs, a switcher, or another simultaneous-session product surface |
| API/SDK | external callers can create, list, attach, steer, cancel, and observe sessions through a supported contract |
| Shared workspace | sessions intentionally read and write the same working tree |
| Isolated workspace | each session gets a container, clone, worktree, or equivalent filesystem boundary |

A session file is not a live backend. A WebSocket endpoint is not automatically a
multi-session API. Multiple tabs do not prove that concurrent writes to one working
tree are safe.

## Comparison

| Product | Multi-session product surface | Persistent service and clients | API or SDK | Workspace model | Assessment |
|---|---|---|---|---|---|
| OpenHands | Agent Canvas and custom clients consume conversation resources | Agent Server exposes isolated per-user sessions and supports standalone or container deployment | Python SDK plus REST and WebSocket; TypeScript client consumes the server contract | Local, Docker, and remote workspaces | Closest complete Python/server reference |
| OpenCode | Desktop maintains multiple session tabs; different session keys can run concurrently | Detached authenticated daemon with durable SQLite records and SSE subscribers | Experimental HTTP API and TypeScript SDK | Sessions may target the same directory; complete cross-session write coordination was not found | Strong product reference, but shared-directory safety is easy to overstate |
| Codex | App-server manages multiple Threads; a client can build tabs, but the CLI TUI is not itself evidence of tab support | App-server supports stdio and Unix socket; daemon and WebSocket paths exist with experimental boundaries | JSON-RPC protocol, generated schemas, and TypeScript/Python SDK surfaces | Thread/turn accepts cwd and sandbox settings; no automatic worktree-per-thread guarantee was found | Strongest protocol and lifecycle reference |
| Claude Code | Remote Control server mode supports multiple sessions and remote session selection; the local CLI is not a tabbed client | Remote Control provides persistent server-owned sessions and multi-device attachment | Agent SDK and stream-json machine interface; a generic local HTTP server is less clearly a stable public contract | Explicit `same-dir` and isolated `worktree` spawning modes | Strongest per-session worktree reference, subject to source-evidence limits |
| Pi | Protocol and client support several sessions on one connection; built-in TUI switches one active session instead of showing tabs | Experimental server core supports several connections and session leases; no standalone coding-agent daemon is supplied | SDK, JSONL RPC, transport-neutral CBOR protocol, and Unix socket building blocks; no bundled REST API | Sessions carry cwd; built-in sandbox isolation is not provided | Best small experimental server/client skeleton |
| Goose | Desktop and API surfaces have session concepts; CLI remains process-oriented | Desktop starts `goosed`; current client/server redesign remains in progress | Bespoke REST and SSE server exist; published general SDK direction is still evolving | Runtime-owned workspace rather than a demonstrated tab-level write-isolation contract | Useful secondary server reference, not the primary Looplane blueprint |
| Oh My Pi | Multiple top-level sessions can exist and Collab can share one live session | The host process remains authoritative; the relay keeps live connections rather than durable runtime state | Node/Bun SDK, stdio RPC, ACP, and WebSocket collaboration | Optional isolation exists mainly for delegated/subagent work; top-level session isolation is not automatic | Strong embedding and collaboration reference, different service ownership model |

## Reference findings

### OpenHands

OpenHands documents four Python packages: SDK, tools, workspace implementations, and
Agent Server. Agent Server exposes conversations and workspaces through REST and
WebSocket to Agent Canvas and custom clients. Production workspaces may use containers
or remote execution, so UI, agent behavior, and execution placement remain distinct.

Primary source:

- [OpenHands SDK architecture overview](https://docs.openhands.dev/sdk/arch/overview)
- [OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk)

Looplane should borrow the ownership boundaries, not the entire deployment footprint.
A local-first Looplane service does not initially need OpenHands' multi-user or
Kubernetes scope.

### OpenCode

The inspected OpenCode snapshot contains real support for session tabs, per-session run
coordination, a detached local server, SQLite persistence, an HTTP API, SSE events, and
a TypeScript SDK. Two limitations matter:

1. The coordinator serializes work by session identity, not by workspace directory.
   Multiple sessions targeting one working tree therefore do not by themselves have a
   proven conflict-resolution boundary.
2. Parts of the v2 API and multi-caller coordination remain experimental. Durable
   records do not imply that active execution can recover seamlessly after a server
   crash.

Local evidence snapshot: `/Users/xiaoxu/Projects/coding-agent-reference/opencode` at
`10765ff2a9da`.

### Codex

Codex app-server defines Thread, Turn, and Item as explicit protocol resources. It can
start, resume, fork, list, and archive threads; start or interrupt turns; and stream
typed notifications. It separates protocol schema, transport, application server,
core, and execution into distinct Rust crates.

Local evidence:

- `coding-agent-reference/codex/codex-rs/app-server/README.md`, especially the
  protocol, schema, and lifecycle sections.
- `coding-agent-reference/codex/codex-rs/app-server-protocol/` for the versioned
  request and event contract.
- `coding-agent-reference/codex/codex-rs/app-server-transport/` for raw transport
  ownership.

The reviewed snapshot is `88f776588f5e`. Its WebSocket transport is explicitly marked
experimental and unsupported. JSON-RPC and Unix socket behavior are better references
for an initial local Looplane service than exposing an unauthenticated TCP listener.

### Claude Code

Claude Code's Remote Control design supports several server-spawned sessions and
distinguishes `same-dir`, `worktree`, and single-session behavior. The worktree option
is the most relevant concurrency precedent for Looplane because it isolates edits while
preserving a shared repository identity.

Detailed local implementation observations come from the reconstructed Claude Code
2.1.88 sourcemap at
`/Users/xiaoxu/Projects/coding-agent-reference/claude-code-source`, snapshot
`83b3ecd74976`. It is not an official Anthropic source release, so feature-gated local
server code is supporting evidence rather than a stable public compatibility promise.

### Pi

Pi separates `protocol`, `client`, `server`, session persistence, the coding-agent SDK,
and TUI. One connection can attach several sessions, and shared or exclusive leases
make client ownership explicit. The server is intentionally transport-neutral and
ships a Unix-socket preset using length-prefixed CBOR.

The package is still labelled experimental and explicitly does not provide a standalone
CLI or coding-agent service; applications must implement the service interface.

Local evidence:

- `coding-agent-reference/pi-mono/packages/server/README.md`
- `coding-agent-reference/pi-mono/packages/client/README.md`
- `coding-agent-reference/pi-mono/packages/protocol/README.md`

Reviewed snapshot: `853a80d26c90`.

### Goose and Oh My Pi

Goose demonstrates a desktop client launching a separate Rust agent server and using a
REST/SSE surface. Its CLI is still an in-process agent, and the public client/SDK shape
continues to evolve. It is useful for server packaging and headless safety, but not as
the main multi-tab TUI model.

- [Goose repository](https://github.com/aaif-goose/goose)
- [Goose client/server design discussion](https://github.com/aaif-goose/goose/discussions/7697)

Oh My Pi has a different strength: file-backed sessions, an embeddable SDK, stdio RPC,
ACP, and encrypted live Collab. Guests can observe and steer the host's session, while
the host remains authoritative and runs every tool. The relay keeps no durable runtime
state, so this is live session sharing rather than a general persistent session service.

Local evidence:

- `coding-agent-reference/oh-my-pi/docs/sdk.md`
- `coding-agent-reference/oh-my-pi/docs/rpc.md`
- `coding-agent-reference/oh-my-pi/docs/collab.md`

Reviewed snapshot: `969062200754`.

## Current Looplane capability boundary

Looplane already has useful primitives:

- `ConversationStore` can create, list, resume, and persist multiple conversations.
- `ConversationController` owns one long-lived conversation runtime and serializes its
  turns.
- Codex and Claude conversation wrappers create disposable isolated workspaces.
- `ConversationWebSocketApp` exposes canonical turn, approval, interrupt, and event
  messages for one preselected session.
- Cloudflare run APIs provide durable bounded-run status, events, approval, artifact,
  and cancellation resources.
- `looplane.sdk` exposes Python-side contracts and helpers.

The product gaps are:

- the TUI stores only one active conversation binding;
- switching conversations closes the previous live runtime;
- `conversation-server` owns one session and closes its controller when the client
  disconnects;
- no service-owned session registry exposes create/list/get/attach/send/cancel/resume;
- no conversation client SDK reconnects by session id and event cursor;
- the production skill loader reads `.looplane/skills/*.md` and plugin references, not
  `.claude/skills` or `.opencode/skills`.

These gaps are a new product milestone, not a behavior-preserving modularization slice.

## Recommended Looplane target

```text
TUI / CLI / future Desktop / Python SDK / generated TS SDK
                         |
                   local authenticated API
                  HTTP + SSE or WebSocket
                         |
                  ConversationService
                         |
                    SessionManager
                +--------+--------+
                |                 |
           Session A         Session B
          Controller A      Controller B
          Workspace A       Workspace B
                |                 |
          runtime adapter   runtime adapter
```

Ownership rules:

- `ConversationService` owns client-independent lifetime and routing.
- `SessionManager` owns the live session registry, persistence/rehydration, leases,
  concurrency budgets, and shutdown recovery.
- Each session has one mutation coordinator and may have several observers.
- TUI tabs are views over session resources; tabs do not own agent processes.
- HTTP handles resource management; SSE or WebSocket carries ordered events,
  approvals, interrupts, and reconnect cursors.
- A generated TypeScript SDK should follow a versioned protocol and a real external
  client, rather than preceding both.

### Workspace decision

The safe default is one isolated workspace or Git worktree per live session. Sessions
may share repository identity, committed baseline, project context, skills, and model
configuration without sharing writable files.

An optional `same-dir` mode would need explicit safeguards:

- file-version or source-fingerprint checks before writes;
- per-path or repository mutation coordination;
- stale-writer fencing after cancel, disconnect, or retry;
- approval ownership and attribution;
- cross-session filesystem change notifications;
- deterministic patch reconciliation and conflict reporting.

Without those contracts, shared-directory tabs only make overwrite races easier to
trigger.

## Suggested implementation order

1. Complete the behavior-preserving modularization seams around conversation,
   transport, terminal binding, workspace, and process execution.
2. Introduce a UI-independent `SessionManager` while retaining the existing
   `ConversationRuntimeSession` and `ConversationController` contracts.
3. Move session lifetime into a local `ConversationService` with durable metadata,
   rehydration, shutdown, and stale-writer tests.
4. Add session resource APIs and an event subscription/reconnect contract.
5. Add a Python client SDK and make TUI/headless modes consume the same service seam.
6. Add TUI session switching and tabs only after service ownership is stable.
7. Add a TypeScript SDK when a web or desktop client becomes an actual consumer.
8. Treat multi-root skill discovery as a separate compatibility feature with explicit
   precedence, collision, symlink, size, count, and trust rules.

## Explicit non-goals for the first service milestone

- No Rust rewrite or Rust sidecar solely to imitate Codex.
- No Electron application before the local service contract has a second client.
- No multi-user or internet-exposed server by default.
- No implicit shared writable workspace across concurrent sessions.
- No claim of perfect Claude Code/OpenCode skill compatibility from directory discovery
  alone.
- No replacement of the M11 unified conversation model with legacy Ask/Agent modes.

## Verification gates

A future implementation should not be called complete until deterministic tests prove:

- two different sessions can progress concurrently while turns within one session stay
  ordered;
- closing and reopening the TUI does not terminate service-owned sessions;
- a client can reconnect from an event cursor without duplicate or missing durable
  events;
- multiple observers cannot become accidental concurrent writers;
- cancel/disconnect fences stale runtime writes;
- isolated workspaces cannot overwrite one another or the source repository without an
  explicit apply step;
- server restart behavior distinguishes durable records, rehydratable idle sessions,
  and non-recoverable in-flight execution honestly;
- local transport authentication and workspace scoping fail closed.

## Evidence limits

The open-source product findings were checked against the local snapshots listed above
and current official OpenHands documentation on 2026-09-05. Moving repositories must be
re-verified before implementation. OpenCode v2 and Pi server APIs contain experimental
surfaces. Claude Code implementation details come partly from a reconstructed source-map
snapshot. Product UI, durable storage, and backend concurrency are therefore recorded as
separate facts rather than inferred from one another.

## Related research and plans

- [Coding agent TUI architecture comparison](2026-09-05-coding-agent-tui-comparison.md)
- [Coding CLI landscape for Looplane](2026-08-22-coding-cli-landscape.md)
- [Repository modularization plan](../plans/repository-modularization-plan.md)
- [M11 unified native conversation](../stages/m11-unified-native-conversation.md)
- [M13 external coding CLI adapters plan](../plans/m13-external-coding-cli-adapters-plan.md)
