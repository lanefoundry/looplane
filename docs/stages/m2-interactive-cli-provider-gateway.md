# M2: Interactive CLI, resumable sessions, and provider gateway

> Status: implementation, release gate, and independent review complete; commit pending.
> Date: 2026-08-21
> Baseline: M1 commit `859db23`

## Scope

Turn the M1 patch harness into this project's own daily CLI. Bare `pca` must run the Python agent
loop with live trace and approval, retain a durable session that can resume after interruption,
keep `pca run` machine-readable, accept local/custom provider endpoints, and expose an optional
model gateway. Launching an installed coding-agent CLI is explicitly not the product architecture.

## References studied

| Reference | What M2 used |
|---|---|
| Pi (`badlogic/pi-mono`) | Provider identity, wire protocol, endpoint, credentials, and model capabilities are separate; Ollama is an OpenAI Chat preset |
| OpenCode | Custom `baseURL` is client routing; ChatGPT/Codex requires a dedicated OAuth/request adapter; permission decisions are distinct from tool execution |
| OMP / oh-my-pi | Explicit protocol discriminators; optional auth gateway does foreign wire → neutral context → provider adapter → foreign wire with no raw passthrough |
| Claude Code recovered source | Append-only session events, checkpoint hydration, approval before side effects, and a separate headless boundary |
| OpenAI Codex CLI | Interactive approvals, resumable sessions, and headless JSON are different frontends over durable state |
| QuidProQuo harness articles | The model API is replaceable; permissions, checkpoints, security, and eval evidence belong to the harness |

Pinned-source evidence and file-level links are retained in
`.research/provider-bridge-comparison.md`. No credential value from another application was read.

## Architecture

```text
bare pca ---------------------------------------------------------------+
  | live ConsoleEventSink                                               |
  | TTYApprovalPolicy                                                   v
  +------------------------------------------------------------> AgentRunner
                                                                  |  |
pca run -> HeadlessApprovalPolicy --------------------------------+  +-> SessionStore
                                                                  |      session.json
pca resume -> writer lease + validation --------------------------+      events.jsonl
                                                                  |
                                                                  v
                                                          ModelProvider
                                                             protocol
                                      +-------------------------+------------------+
                                      |                         |                  |
                                openai_chat        openai_codex_responses   native adapters
                                      |                         |
                              Ollama/API URL         app-owned OAuth grant

pca gateway -> OpenAI Chat parser -> canonical messages/tools -> ModelProvider
```

## Implementation decisions

### Approval is injected, not embedded in tools

`ToolEffect` classifies every tool as read, modify, or execute. Read is automatic; `apply_patch`,
model-requested checks, and final verification pass through the same `ApprovalPolicy`. Interactive
decisions are once/session/deny/cancel. Headless mode never reads stdin and retains its explicit
`--unsafe-local-exec` acknowledgement.

Durable approval events are written before console projection. A denied model action becomes a
failed `ToolObservation` so the model can adapt; cancellation creates an auditable terminal result.

### Resume is validated continuation, not rerun

Each run now has a versioned `session.json`. An OS `flock` is the real single-writer fence; the
manifest token prevents a stale writer from saving after ownership changes. Resume verifies:

- request/session run ID, task ID, base SHA, provider, explicit wire protocol, and model;
- contiguous event sequence and matching last sequence;
- existing workspace Git root and pinned HEAD;
- supported non-terminal lifecycle state.

Messages, usage, step, repetition guard, verification state, and event sequence are hydrated. The
workspace is not recloned or reset. A real Ctrl-C Ollama run resumed at the next event sequence.
Each event first commits the complete resumable state and intended sequence, then appends the
durable JSONL record. Resume can repair the single safe crash window where the manifest is one
sequence ahead. Pending approval is abandoned as an explicitly unexecuted action so the model can
request it again. If the last durable event is `tool.started` or `verification.started`, automatic
resume fails closed because exactly-once completion cannot be proven. Step count, accumulated
active wall time, and session-scoped effect grants persist across process restarts.

### Provider bridge is not one generic URL

M2 identifies the wire protocol independently of provider name. Remote custom endpoints must use
HTTPS; loopback HTTP is permitted only for `localhost`, `127.0.0.1`, or `::1`, with URL credentials,
query, and fragment rejected. Ollama is an `openai_chat` preset with keyless loopback auth, bounded
output, and Qwen's `/no_think` compatibility prefix.

The experimental ChatGPT/Codex adapter is `openai_codex_responses`, with fixed OAuth audience,
account routing header, Codex request shaping, and SSE parsing. It owns a separate 0600 credential
store and never imports another CLI's token. Construction and CLI use both require an explicit
experimental flag. Its HTTP/OAuth tests are mocked; no live app grant is claimed in this milestone.

Claude Pro/Max OAuth is not implemented. Current OpenCode documentation describes third-party
subscription plugins as prohibited while other projects still contain technical implementations.
M2 therefore supports Anthropic API keys or an operator-approved endpoint, not token scraping or a
claim that Claude plan limits power this harness.

### The optional gateway copies OMP's boundary

`ModelGateway` is a pure ASGI translator, not arbitrary passthrough. It supports `/healthz`,
`/v1/models`, and non-streaming `/v1/chat/completions`; strictly parses OpenAI wire messages/tools,
invokes the canonical model contract, and encodes the response. Request size, model selection,
tool-result ancestry, optional Bearer auth, error redaction, loopback-only CLI binding, and
same-event-loop provider shutdown are enforced in code.

## Real-provider evidence and failure boundary

The local Ollama service was tested with `qwen3:0.6b` and `qwen3:4b`:

- direct `OpenAICompatibleModel` text returned `ADAPTER_OK` with real usage;
- a forced tool request returned one canonical `read_file(path="src/example.py")` call;
- `pca gateway` returned health, model catalog, and `GATEWAY_OK` through a real HTTP request;
- the first gateway shutdown exposed a cross-event-loop client close bug; ASGI lifespan ownership
  fixed it, and the second Ctrl-C shutdown exited cleanly;
- a full tiny-bug run did **not** complete: 0.6B proposed shell text instead of a patch; 4B read the
  correct source but emitted an invalid unified diff and later exhausted its response budget.

Those failures are retained as evidence, not hidden. Approval rejected unsafe content,
`git apply --check` rejected malformed diffs, Ctrl-C left a resumable session, and a new
`finish_reason=length` guard prevents truncated output from being mistaken for a final answer.
This proves transport/harness behavior, not sufficient 4B coding-agent quality.

## Security boundaries

- The local check runtime is still not an OS sandbox; TTY approval does not hide host files or
  disable network.
- OAuth secrets are scoped to the fixed Codex audience and redacted from repr/errors/events.
- The gateway does not accept an arbitrary upstream URL per request and refuses non-loopback CLI
  binding.
- Session resume rejects symlinks, invalid JSON, unrecoverable event gaps, workspace mismatch,
  terminal state, concurrent writers, and an ambiguously interrupted side effect.
- Gateway streaming, inter-process OAuth refresh fencing, TLS remote service, hostile-code sandbox,
  and Cloudflare deployment remain later work.

## Verification gate

Final local release-gate result on 2026-08-21:

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
uv run pca --help
uv run pca gateway --help
git diff --check
```

- `uv sync --extra dev`: resolved 35 packages and audited 33 packages.
- `uv run ruff check .`: all checks passed.
- `uv run pytest`: 128 tests passed in 14.31 seconds after the final release-review fixes.
- `uv build`: built both the source distribution and wheel.
- `uv run pca --help`, `uv run pca gateway --help`, `uv run pca run --help`, and
  `uv run pca resume --help`: all command surfaces rendered successfully.
- `git diff --check`: passed.
- An isolated checkout of the exact staged snapshot at `/tmp/pca-m2-stage.hzFDPK` independently
  passed Ruff, all 128 tests in 15.27 seconds, and built both distributions before commit.
- QuidProQuo `pnpm check:references`: checked 1087 posts with no reference errors. The repository-
  wide `pnpm check:post-quality` still exits 1 because of pre-existing unrelated articles; the M2
  draft is not listed among its errors.

## Commit

The practice article is drafted at
`quidproquo/src/content/posts/ai/2026-08-21-python-coding-agent-interactive-cli-provider-gateway.md`
and remains uncommitted for user review. The independent release review reached GO after three
fault-injection fixes; its report is `.research/m2-release-review.md`. The M2 commit SHA will be
appended after isolated staged-snapshot verification.
