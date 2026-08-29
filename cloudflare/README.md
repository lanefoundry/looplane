# Cloudflare Sandbox control plane

This subproject is the bounded M6 Worker/Sandbox slice. It accepts one asynchronous coding run,
stages a small text-only source tree in a fresh Sandbox from a background task, invokes one fixed
Python entrypoint, reads the bounded result bundle, persists terminal run metadata/artifacts, and
destroys the Sandbox in `finally`.

It does not accept Git URLs, archives, shell strings, provider credentials, consumer subscription
tokens, custom model IDs, or caller-selected upstream URLs.

## Routes

### `GET /healthz`

Unauthenticated liveness only:

```json
{"ok":true,"service":"rivumi-control-plane"}
```

This proves Worker routing, not Sandbox or model execution.

### `POST /v1/runs`

Requires `Authorization: Bearer <CONTROL_PLANE_TOKEN>` and `Content-Type: application/json`.

```json
{
  "instruction": "Change hello.py and keep the check green.",
  "model": "the exact operator-configured model",
  "files": [{"path": "hello.py", "content": "print('hello')\n"}],
  "allowedPaths": ["hello.py"],
  "checks": [["git", "diff", "--check"]],
  "limits": {"maxSteps": 12, "wallTimeSeconds": 180}
}
```

Accepted requests return `202` with the new `runId` plus status, event, and approval links. The
route creates a queued `RunSession` Durable Object record, then starts the Sandbox lifecycle through
`ExecutionContext.waitUntil()`. Clients attach through the status, event, approval, and artifact
routes below.

Terminal success is written to the run session after the background Sandbox run finishes. Exit `0`
is accepted only with `ok: true` plus a `completed` result. Exit `1` is accepted only with
`ok: false` plus a `failed` or `cancelled` result and the full artifact bundle. Every other
exit/result/schema combination fails closed by marking the run `failed` with a bounded error code.
Capability revocation and Sandbox teardown run after terminal result validation.

### `GET /v1/runs/:runId`

Requires `Authorization: Bearer <CONTROL_PLANE_TOKEN>`. Returns durable run metadata only:
status, model, timestamps, request summary, terminal summary/reason, execution result, cancellation
flag, and artifact key names. Artifact bodies are not included in this response.

### `GET /v1/runs/:runId/events`

Requires control-plane auth. Returns the stored terminal `events` artifact as
`application/x-ndjson`. While a run is active, the Sandbox mirrors emitted `RunEvent` JSONL lines to
the Worker and this route returns those live-appended lines; after completion it falls back to the
terminal bundled artifact if no live lines were received.

Pass `?stream=1` to receive `text/event-stream` frames. Stream mode replays the bounded stored
event buffer on attach, keeps the connection open for non-terminal runs, pushes newly appended
events, emits `: heartbeat` comments while idle, and closes when the run completes, fails, or is
cancelled. Clients may send `Last-Event-ID` to replay only events whose integer `sequence` is newer
than that cursor. A Durable Object restart drops in-memory subscribers, so clients should reconnect
and rely on replayed stored lines.

Python callers can use the lightweight attach client:

```python
from rivumi.cloudflare_client import CloudflareRunClient

client = CloudflareRunClient(base_url="https://control.example", token=token)
accepted = await client.start_run(request)
async for event in client.attach_events(accepted["runId"], last_event_id=0):
    print(event.event, event.data)
```

### `GET /v1/runs/:runId/approvals`

Requires control-plane auth. Returns `{pending, decisions}` from the durable run session. Pending
approval rows are derived from live `approval.requested` run events and contain only bounded
request/action IDs, effect, reason, policy reason, preview, and timestamp metadata.

### `POST /v1/runs/:runId/approvals/:approvalId`

Requires control-plane auth and a JSON body such as `{"decision":"allow_once"}`. Supported
decisions are `allow_once`, `allow_session`, `deny`, and `cancel`. Submitting a decision records it
durably and removes the approval from the pending list. The Sandbox entrypoint polls a dedicated
internal approval endpoint with a short-lived approval token, so a waiting remote run can consume the
submitted decision and continue through the normal runner approval path.

### `GET /v1/runs/:runId/artifacts/:name`

Requires control-plane auth. `:name` must be one of `request`, `events`, `checkpoint`, `patch`,
`test_log`, or `result`. Artifact contents may include source, prompts, diffs, and logs; callers
should treat this route as sensitive.

### `POST /v1/runs/:runId/cancel`

Requires control-plane auth. Marks cancellation requested in `RunSession`. For non-terminal runs,
the Worker revokes the run capability and destroys the sandbox best-effort, returning `202`. For
already-terminal runs it returns the terminal status with `200`.

A completed response is accepted only when every requested check appears exactly once with the
same argv and a passing exit status, and every reported changed file is covered by the request's
validated `allowedPaths`. Failed/cancelled responses may contain partial checks, but any reported
entry must still map exactly to the request contract.

Accepted check argv must exactly equal one of:

- `git diff --check`
- `python3 -m pytest -q`
- `python3 -m compileall -q .`
- `python3 -m unittest discover`

No shell parsing is used for these checks. The only Worker-to-Sandbox exec command is:

```text
/usr/local/bin/rivumi-sandbox-run
```

The root-owned, mode `0555` wrapper validates the staged workspace and token files, changes the
workspace owner to the image's non-root `rivumi` user, sets the tokens to owner-only mode `0600`,
and uses `setpriv --no-new-privs` before invoking the fixed Python module. Caller data is never
inserted into a shell command.

## Model capability boundary

The Sandbox receives two five-minute HMAC capabilities containing only route audience, run ID,
model, issued time, and expiry. The Worker writes the model-proxy token to
`/workspace/.rivumi-run-token` and the event-append token to `/workspace/.rivumi-event-token`; the
non-root Python entrypoint opens both without following links and immediately unlinks them. These
capabilities are never present in the Sandbox exec environment.

Each run also owns a strongly consistent `RunCapability` Durable Object. The Worker activates it
with a `maxSteps + 2` model-request budget, atomically consumes one unit before each upstream call,
and revokes it before Sandbox teardown. A correctly signed token is therefore rejected after
teardown, after expiry, after budget exhaustion, or for a different model. This state is backed by
Durable Object SQLite rather than an isolate-local map.

Each run also owns a `RunSession` Durable Object keyed by run ID. It records
`queued | running | completed | failed | cancelled` state, bounded request metadata, terminal
summary, artifact key names, and explicit cleanup/cancellation markers. Full artifact bodies are
available only through authenticated artifact routes.

The Sandbox calls `/internal/v1/chat/completions`; that route verifies both the HMAC and active DO
state, pins the operator model, rejects extra request fields/streaming, caps output tokens, and
bounds request and response bodies while streaming them into memory.

The Sandbox also posts live event JSONL batches to `/internal/v1/runs/:runId/events` with the
event-append token. The route verifies the event audience, requires `task_id` to match the
Cloudflare run ID, validates each line as one JSON object, and checks the run capability without
consuming model-request budget. `RunSession` caps stored live events by line count and UTF-8 bytes.

`OPENAI_API_KEY` remains in Worker env and is added only to the Worker-to-provider request. It is
never written to a source file, runner request, Sandbox exec env, result, or error response.

`MODEL_API_URL` is operator-owned and must be the exact HTTPS chat-completions endpoint. HTTP URLs,
credentials, query strings, fragments, caller overrides, and non-`/chat/completions` paths are
rejected. This supports OpenAI-compatible providers such as Groq or OpenRouter without weakening
the caller boundary.

Required Worker environment bindings:

- `CONTROL_PLANE_TOKEN` — at least 16 UTF-8 bytes
- `RUN_TOKEN_SECRET` — at least 32 UTF-8 bytes
- `OPENAI_API_KEY` — commercial/provider API credential
- `OPENAI_MODEL` — the single accepted model ID
- `MODEL_API_URL` — validated operator-owned HTTPS endpoint

Use Wrangler secrets or another Cloudflare-managed secret injection path for credentials. Do not put
secret values in `wrangler.jsonc`, Docker build arguments, or container environment configuration.

## Enforced limits

| Boundary | Limit |
| --- | ---: |
| Ingress JSON | 768,000 bytes |
| Text source files | 32 |
| One file | 64,000 UTF-8 bytes |
| Source tree | 512,000 UTF-8 bytes |
| Allowed paths | 32 |
| Exact checks | 4 |
| Instruction | 32,000 characters |
| Agent steps | 20 maximum |
| Requested wall time | 220 seconds maximum |
| Sandbox exec timeout | 240 seconds |
| Run capability | 300 seconds |
| Model requests per run | `maxSteps + 2` |
| Sandbox destroy wait | 5 seconds |
| Model request / response | 256,000 / 1,000,000 bytes |
| Sandbox response | 1,500,000 bytes |

Source paths are relative, POSIX-like, text-extension allowlisted, and cannot contain empty, dot,
parent, `.git`, backslash, or NUL segments. An allowed path must bind to an uploaded file exactly or
to an uploaded directory using a terminal `/**`.

## Verification

```sh
npm install
npm test
npm run typecheck
npm run types:check
npm run dry-run
```

Use `npm run deploy`, not a bare `wrangler deploy`, for a real release. The deploy script rebuilds
the wheel and locked runtime artifacts first so the uploaded container matches the current Python
source.

The dry run builds the current repository wheel into the digest-pinned Sandbox Python image but does
not deploy or create external resources. `build:runtime` exports a hash-locked Python requirement
set and a CycloneDX dependency manifest before building the wheel. On Apple Silicon, an explicit
standalone Docker smoke build may
need `--platform linux/amd64`; Wrangler's container dry run selects the target platform itself.
The image pins pytest 8.4.2 and pytest-asyncio 1.4.0 for the fixed Python check surface; arbitrary
uploaded project dependencies are not installed automatically.

Two clean builds from the same source and lockfiles must produce the same image ID. The image also
retains a sorted `/opt/rivumi/python-packages.txt` inventory for later provenance checks.

The M6 live evidence retains one completed Worker to Sandbox to Groq coding run with a verified
patch and check. The current endpoint now starts runs asynchronously and exposes durable status,
events, and terminal artifacts, but it is still not a full hostile-code containment claim.

## Durable Object configuration

`wrangler.jsonc` now declares three Durable Objects:

- `Sandbox`, registered by migration `v1`
- `RUN_CAPABILITIES` / class `RunCapability`, registered as a new SQLite class by migration `v2`
- `RUN_SESSIONS` / class `RunSession`, registered as a new SQLite class by migration `v3`

The capability object stores only model, expiry, maximum requests, and consumed count. It stores no
provider key, source, prompt, artifact, or raw run token. Run status and terminal artifacts are
persisted through `RunSession`.
