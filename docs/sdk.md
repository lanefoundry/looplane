# looplane SDK Facade

`looplane.sdk` is the stable import surface for embedding looplane from Python.
The CLI and TUI remain user-facing entrypoints; third-party code should import
from this facade instead of reaching into adapter internals.

Stability: `0.x` contracts are typed and versioned, but may change before 1.0.

## Run API

```python
from looplane.sdk import TaskContract, run_task

result = await run_task(task, model, "/tmp/looplane-runs")
```

`run_task()` enables sandboxed verification by default. Pass
`sandbox_checks=False` only for a trusted local escape hatch.

## Conversation Attach

`ConversationWebSocketApp` exposes a stateful ASGI WebSocket endpoint at
`/v1/conversation/attach`.

Client messages:

```json
{"type":"turn","text":"Fix the failing test."}
{"type":"approval","request_id":"approval-1","decision":"allow_once"}
```

Server messages:

```json
{"type":"event","event":{"event_type":"text_delta","sequence":0,"turn_id":"turn","text":"..."}}
{"type":"result","result":{"status":"completed","terminal_reason":"conversation_turn_completed"}}
```

SSE/NDJSON consumers can keep using existing event JSONL files; WebSocket is
the parity attach path for long-lived sessions and inline approvals.

`BackendTurnLimiter` can be shared across `ConversationWebSocketApp` or native
conversation controllers to bound concurrently active backend turns. The TUI
shares one limiter across cached native controllers by default.

## Replay And Fork

```python
from looplane.sdk import fork_run_at_event, replay_run_events

state = replay_run_events("runs/<run-id>/events.jsonl")
seed = fork_run_at_event(
    source_run_dir="runs/<run-id>",
    run_root="runs",
    sequence=42,
)
```

Replay reducers are deterministic and side-effect-free. Forking creates a new
workspace from the recorded base commit and never replays prior tools, checks,
subprocesses, model calls, or commits.

## Subagents

`derive_subagent_task()` creates a child `TaskContract` that keeps the parent
repository/base boundary while allowing narrower instructions, paths, checks,
and limits. `run_subagent_task()` executes that child through `AgentRunner`
under `run_root/subagents/<id>`, giving each subagent its own looplane run
directory and disposable workspace.

The native loop also exposes `dispatch_subagents` to the model as a fan-out and
handoff tool. It accepts up to four named `scout`, `analyst`, or `reviewer`
agents. `normalize_subagent_schedule()` validates and normalizes that
model-requested graph before execution, rejecting unsafe ids, unsupported roles,
unknown dependencies, cycles, and out-of-budget step counts. The native loop
emits the normalized waves as `subagents.schedule_normalized`, runs
dependency-free agents in parallel, injects bounded `depends_on` handoff
summaries into later agents, keeps every child in an isolated workspace with
direct modify/execute approvals disabled, and returns a bounded summary
observation to the parent turn. A child spec may include one
`proposed_transaction`; after that child completes, the parent executes those
steps sequentially through the existing `tool_transaction` approval, permission,
check, and rollback path. `AgentRunner(subagent_models=...)` lets embedders
route child agents by role or id while keeping the model routing outside
model-controlled tool arguments. The SDK exports
`A10_SUBAGENT_PLANNER_POLICY_VERSION` and `render_subagent_planner_policy()` so
embedders can inspect or reuse the same versioned planner guidance looplane
injects into native prompts. `analyze_subagent_schedule_jsonl()` and
`scripts/analyze_subagent_schedules.py` summarize emitted schedule traces for
planner tuning; `looplane sessions --analyze-subagents <run>` exposes the same
analysis for persisted run artifacts.

## Role Lanes And Cost

`role_candidates()` returns static provider/model candidates for lanes such as
`primary`, `reasoning`, `reviewer`, `summarizer`, and `parser`.
`estimate_cost()` turns provider usage into an estimated USD breakdown when the
model exists in looplane's explicit pricing table. Missing prices return `None`;
looplane must not invent a cost.

## MCP OAuth Metadata

Native MCP HTTP configs may use either `bearerTokenEnvVar` or authorization-code
metadata under `oauth`. looplane first reads `oauth.accessTokenEnvVar`; if it is
unset, `looplane auth login-mcp` can create an app-owned authorization-code grant
and store it in looplane's private credential store. looplane never imports another
client's credential files.

## Hooks And Skills

Project skills are markdown files under `.looplane/skills/*.md`. They are loaded
as bounded, lower-priority prompt guidance and do not execute code. External
coding runners project the same resolved skill bundle into delegated prompts
and persist `skill-resolution.json` metadata in the run artifacts.
`TaskContract(enabled_skills=("reviewer",))` narrows native and external
skill projection to exact skill-name matches. The default empty tuple keeps the
current load-all behavior; unknown or duplicate names fail closed.

Project hooks live in `.looplane/hooks.json` and are disabled unless
`LOOPLANE_ENABLE_PROJECT_HOOKS=1` is set. Hook commands use exact argv, receive a
JSON payload on stdin, and may deny `pre_tool_use`, `post_tool_use`, or
`approval_request` events. Native compaction fallback also emits `pre_compact`
and `post_compact`; `pre_compact` may deny the fallback before it rewrites
history. Long-lived external runtime compaction through `ConversationController`
uses the same `pre_compact` / `post_compact` hook events. Hooks cannot grant
approval or bypass deny rules.

Project plugins are JSON manifests under `.looplane/plugins/*.json`. A plugin can
package markdown skills, deny-only hooks, and bounded discovery metadata such as
keywords, homepage, repository, license, and author; packaged hooks still require
`LOOPLANE_ENABLE_PROJECT_HOOKS=1` before any command executes.
Use `looplane plugin install ./plugin.json` to copy a local plugin manifest and
referenced markdown skills into the repository, and `looplane plugin list` to
inspect installed package manifests. Discovery metadata is local manifest data;
looplane does not fetch, install, or trust remote marketplace content from those
fields.

## Instructions

looplane loads user instructions first, then project `AGENTS.md` / `LOOPLANE.md`
files from repository root toward the working directory. Project
`AGENTS.override.md` / `LOOPLANE.override.md` keeps user instructions but replaces
earlier project instruction layers. The native loop reloads a changed resolved
instruction bundle as injected context and watches instruction, skill, plugin,
and hook sources for turn-boundary reload signals through
`project_context_watch_snapshot()`. Hosts that need a long-lived service can use
`watch_project_context_changes()` to receive an async stream of fingerprint
changes with changed categories and source lists.
`project_context_watch_capabilities()` advertises the backend policy: portable
fingerprint polling is available without optional dependencies, while OS-native
filesystem notifications are explicitly unavailable until looplane selects a
cross-platform dependency or per-platform backend. External coding runners
project the same resolved bundle into delegated prompts, write
`external-instruction-policy.json`, and instruct child runtimes not to apply their
own duplicate instruction
discovery on top of looplane's resolved bundle. The policy artifact also records
the backend's declared native duplicate-discovery controls as
`native_suppression`: `configured` includes the exact argv/env controls the
wrapper claims to apply, while `prompt_only` means looplane projected the resolved
bundle and suppression directive but the backend did not declare a stable native
disable control.

## Prompt Sections And Injected Context

`PromptSection` and `render_prompt_sections()` provide the stable prompt
section surface used by looplane's native loop. Sections carry deterministic
names plus stable/dynamic cache metadata so callers can preserve prompt-prefix
discipline without parsing the rendered prompt string.
The native loop now renders core policy, tool policy, runtime facts,
interaction policy, instructions, skills, workspace state, and memory as
separate named sections. Workspace state includes a bounded initial
`git status --short --branch` snapshot when available.
`render_tool_prompt_context()`, `render_interaction_prompt_context()`,
`render_runtime_prompt_context()`, and
`render_workspace_prompt_context()` expose the same section payload builders to
embedders.

`RuntimeInjectedContext` is the app-server input contract for context supplied
by an embedding application. The WebSocket attach surface accepts
`{"type":"inject_items","items":[{"source":"ide","content":"..."}]}` and queues
accepted items for the next conversation turn, where looplane renders them under
`[app-server-injected-context-v1]`.

Native runs may also opt into repository-local runtime context providers through
`.looplane/context-providers.json`. Providers are exact-argv commands, disabled
unless `LOOPLANE_ENABLE_PROJECT_HOOKS=1`, receive bounded run metadata on stdin,
and must emit `RuntimeInjectedContext` JSON. Valid provider output is injected
before the next model request as `InjectedContext(source="context_provider:<name>")`;
provider failures are reported as run events and do not silently mutate context.
`ConversationController(context_provider_runner=...)` applies the same contract
to external app-server turns: providers run before `send_turn`, receive bounded
turn lifecycle metadata, and are projected into the turn text as
`[injected_context:context_provider:<name>]`.

`RuntimeAttachment` is the bounded app-server attachment contract for turn-time
context. A WebSocket `turn` may include `attachments` with inline text content
or file-reference URIs; looplane projects them under `[app-server-attachments-v1]`
without reading arbitrary local files on behalf of the client.
Native provider adapters can also receive user-message attachments through
`Message(provider_metadata={"attachments": [...]})`. OpenAI-compatible Chat
Completions, Responses, Anthropic, and Gemini map supported image/PDF URI or
base64 items into native multimodal content blocks where the provider shape
supports it, and preserve unsupported/text attachments as explicit text fallback
blocks.

`InjectedContext` represents harness-injected context such as context-pressure
reminders, deterministic history summaries, and post-compaction workspace
state. Provider adapters render it as marked context text, distinct from
ordinary user-authored messages.

For Anthropic direct provider calls, sectioned system prompts are rendered as
system content blocks and the contiguous stable prefix is marked with
`cache_control: {"type":"ephemeral"}`.

OpenAI-compatible Chat Completions and Responses adapters derive a
`prompt_cache_key` from stable system sections and tool schemas, excluding
dynamic sections and conversation-tail content.
`provider_cache_mapping()` documents the default cache hint location per
provider (`extra_body.prompt_cache_key`, top-level `prompt_cache_key`, or
Anthropic `cache_control`), and `apply_provider_cache_defaults()` applies those
defaults without overwriting caller-supplied provider hints.
`provider_cache_trace()` extracts prompt-cache metadata from concrete adapter
request payloads, including prompt cache keys, tool schema fingerprints, and
Anthropic cache-control block counts.
Native runs persist adapter-exposed traces to `cache-traces.jsonl` and emit
`model.cache_trace` events when a provider exposes `last_cache_trace`.
`cache_aware_prompt_ordering()` provides a trace-gated ordering policy for
callers that want to move stable sections into a contiguous cache prefix: it
only reorders when supplied provider traces are cache-ready, otherwise it
returns the original order with warnings. Call sites can make the opt-in
explicit with `CacheAwarePromptOrderingMode`: `disabled` preserves source order,
`trace_ready` is the conservative default, and `always` is reserved for
validation experiments that need forced stable-prefix ordering.

`ContextTelemetry.input_cache_hit_rate` reports cached input as a provider-neutral
ratio, and the TUI displays that rate in `/context` and `/usage` when provider
usage includes cached input tokens.

Live provider cache reuse is validated outside normal unit tests with
`scripts/validate_provider_cache_reuse.py`. Set
`LOOPLANE_CACHE_VALIDATE_PROVIDER`, `LOOPLANE_CACHE_VALIDATE_MODEL`, and
`LOOPLANE_CACHE_VALIDATE_API_KEY`; optionally set `LOOPLANE_CACHE_VALIDATE_BASE_URL`
and `LOOPLANE_CACHE_VALIDATE_OUTPUT`. The script makes two repeated calls, prints
the persisted trace shape plus provider-reported cached input tokens, and exits
77 when live credentials are not configured.

## IDE Diagnostics

Editor or LSP adapters can export diagnostics to
`.looplane/ide/diagnostics.json`. looplane accepts a bounded looplane-shaped list or
an LSP `publishDiagnostics` envelope with `file://` URIs, rejects paths outside
the repository, and injects changed snapshots as
`InjectedContext(source="ide_diagnostics")` before model requests.

The SDK exports `IdeDiagnostic`, `IdeRange`, `IdePosition`,
`IdeDiagnosticSeverity`, `IdeDiagnosticsSnapshot`, and
`render_ide_diagnostics_context()` for adapter authors. When a renderer receives
`project_root`, diagnostics include a bounded editor deep link generated with
`build_editor_deep_link()`.

For hosts that want looplane to own the diagnostics process, the SDK also exports
`LspServerCommand` and `ManagedLspServer`. The supervisor starts an exact-argv
long-lived LSP subprocess inside the project root, reads `Content-Length`
JSON-RPC frames from stdout, consumes `textDocument/publishDiagnostics`, writes
the same `.looplane/ide/diagnostics.json` bridge file atomically, and exposes
`start()`, `wait_for_diagnostics()`, and `aclose()` lifecycle methods.

Adapters can also export open-file state to `.looplane/ide/open-files.json`.
looplane accepts active file, cursor, and selection ranges, then injects changed
snapshots as `InjectedContext(source="ide_open_files")`. `build_editor_deep_link()`
supports VS Code `vscode://file/...` links with one-based line/column positions
and plain `file://` URIs; paths are normalized through the same repository-boundary
checks as diagnostics.

The repository includes a packageable VS Code extension scaffold in
`editors/vscode`. It listens for VS Code diagnostics and visible-editor changes,
then writes those IDE bridge files atomically for looplane to consume. The
extension package includes its npm lockfile and supports `npm run compile`,
`npm audit --audit-level=moderate`, `npm run package`, and isolated
`code --install-extension` smoke checks. When `looplane.ideContext.webSocketUrl`
is set, the extension also pushes typed
`{"type":"ide_context","diagnostics":...,"open_files":...}` messages to the
conversation WebSocket attach endpoint; the server validates paths against its
configured project root before queuing rendered `ide_diagnostics` and
`ide_open_files` context for the next turn.

## Instruction Resolution

`resolve_instruction_documents()` returns the active user/project instruction
bundle together with source-priority diagnostics for active and suppressed
files. `render_instruction_diagnostics()` renders the same metadata looplane uses
in native prompt context and external-runner `instruction-resolution.json`
artifacts.

## Policy And Artifacts

Project policy lives at `.looplane/policy.json`. Org policy is supplied by
`LOOPLANE_ORG_POLICY` or an explicit path passed to policy discovery. Deny rules
from user, org, and project policy win before allow rules.

Patch acceptance, verification terminal output, transcript export, and OTel
artifact export pass through conservative secret scanning/redaction. Findings
record path, line, and pattern only.
