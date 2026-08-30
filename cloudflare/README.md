# Cloudflare Sandbox control plane

This subproject is the bounded M6 Worker/Sandbox slice. It accepts one asynchronous coding run,
stages a small text-only source tree in a fresh Sandbox from a background task, invokes one fixed
Python entrypoint, reads the bounded result bundle, persists terminal run metadata/artifacts, and
destroys the Sandbox in `finally`.

It does not accept Git URLs, archives, shell strings, provider credentials, consumer subscription
tokens, caller-selected model IDs, or caller-selected upstream URLs. Callers select only an
operator-managed model profile.

## Routes

### `GET /healthz`

Unauthenticated liveness only:

```json
{"ok":true,"service":"looplane-control-plane"}
```

This proves Worker routing, not Sandbox or model execution.

### `GET /v1/model-profiles`

Requires `Authorization: Bearer <CONTROL_PLANE_TOKEN>`. Returns the default profile plus the safe
operator-managed profile list:

```json
{
  "default": "openrouter-primary",
  "profiles": [
    {
      "id": "openrouter-primary",
      "provider": "openrouter",
      "protocol": "openai-chat",
      "model": "operator-approved-model-id",
      "ready": true
    }
  ]
}
```

`ready` reports only whether the configured secret binding currently contains a non-empty value.
Endpoint, secret-binding name, profile fingerprint, and provider key are never returned. Python
callers can use `await client.model_profiles()` and select a ready profile before constructing a run
request.

### `POST /v1/runs`

Requires `Authorization: Bearer <CONTROL_PLANE_TOKEN>` and `Content-Type: application/json`.

```json
{
  "instruction": "Change hello.py and keep the check green.",
  "modelProfile": "openrouter-primary",
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
status, model profile, provider, model, timestamps, request summary, terminal summary/reason,
execution result, cancellation flag, and artifact key names. Artifact bodies are not included in
this response.

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
from looplane.cloudflare_client import CloudflareRunClient

client = CloudflareRunClient(base_url="https://control.example", token=token)
profiles = await client.model_profiles()
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
/usr/local/bin/looplane-sandbox-run
```

The root-owned, mode `0555` wrapper validates the staged workspace and token files, changes the
workspace owner to the image's non-root `looplane` user, sets the tokens to owner-only mode `0600`,
and uses `setpriv --no-new-privs` before invoking the fixed Python module. Caller data is never
inserted into a shell command.

## Model capability boundary

The Sandbox receives three five-minute HMAC capabilities containing only route audience, run ID,
model profile, provider, model, an opaque profile fingerprint, issued time, and expiry. The Worker
writes the model-proxy, event-append, and approval tokens to owner-only files; the non-root Python
entrypoint opens them without following links and immediately unlinks them. These capabilities are
never present in the Sandbox exec environment. Endpoint and secret-binding names are not included
in the token.

Each run also owns a strongly consistent `RunCapability` Durable Object. The Worker activates it
with a `maxSteps + 2` model-request budget, atomically consumes one unit before each upstream call,
and revokes it before Sandbox teardown. A correctly signed token is therefore rejected after
teardown, after expiry, after budget exhaustion, for a different profile/provider/model, or after
the selected profile's routing configuration changes. This state is backed by Durable Object
SQLite rather than an isolate-local map.

Each run also owns a `RunSession` Durable Object keyed by run ID. It records
`queued | running | completed | failed | cancelled` state, bounded request metadata, terminal
summary, artifact key names, and explicit cleanup/cancellation markers. Full artifact bodies are
available only through authenticated artifact routes.

The Sandbox calls `/internal/v1/chat/completions`; that route verifies both the HMAC and active DO
state, resolves the signed operator profile, pins its provider and model, rejects extra request
fields/streaming, caps output tokens, and bounds request and response bodies while streaming them
into memory.

The Sandbox also posts live event JSONL batches to `/internal/v1/runs/:runId/events` with the
event-append token. The route verifies the event audience, requires `task_id` to match the
Cloudflare run ID, validates each line as one JSON object, and checks the run capability without
consuming model-request budget. `RunSession` caps stored live events by line count and UTF-8 bytes.

`MODEL_PROFILES_JSON` is an operator-owned, non-secret catalog. Each profile fixes a provider,
protocol, model, exact HTTPS chat-completions endpoint, and the name of a separate secret binding.
HTTP URLs, credentials, query strings, fragments, caller overrides, non-`/chat/completions` paths,
unknown fields, and unknown profiles are rejected. Phase 1 accepts only the `openai-chat` protocol.

```json
{
  "default": "openrouter-primary",
  "profiles": {
    "openrouter-primary": {
      "provider": "openrouter",
      "protocol": "openai-chat",
      "model": "operator-approved-model-id",
      "apiUrl": "https://openrouter.ai/api/v1/chat/completions",
      "apiKeyBinding": "MODEL_PROVIDER_KEY_OPENROUTER"
    },
    "nvidia-nim": {
      "provider": "nvidia-nim",
      "protocol": "openai-chat",
      "model": "operator-approved-model-id",
      "apiUrl": "https://integrate.api.nvidia.com/v1/chat/completions",
      "apiKeyBinding": "MODEL_PROVIDER_KEY_NVIDIA_NIM"
    }
  }
}
```

The caller sends only `modelProfile`. It cannot supply a provider, model, endpoint, binding name,
credential, or extra upstream header. The Worker resolves the profile again on every internal model
request and selects only its configured secret.

Required Worker environment bindings:

- `CONTROL_PLANE_TOKEN` — at least 16 UTF-8 bytes
- `RUN_TOKEN_SECRET` — at least 32 UTF-8 bytes
- `MODEL_PROFILES_JSON` — non-secret profile catalog, normally configured as an environment-specific
  Worker variable
- every profile's `apiKeyBinding` — an independent commercial/provider API credential; the batch
  setup below creates all bindings with one `wrangler secret bulk` request, while manual operations
  may use `wrangler secret put`

Use Wrangler secrets or another Cloudflare-managed secret injection path for credentials. Do not put
secret values in `wrangler.jsonc`, Docker build arguments, or container environment configuration.

### Batch provider setup

Operators do not need to edit an escaped `MODEL_PROFILES_JSON` value or upload provider keys one at
a time. Copy `providers.example.json`, keep the catalog non-secret, and list every desired profile
in one file. Known OpenAI-compatible providers need only a provider and model; looplane pins their
catalog endpoint and derives the Worker secret binding.

Run these commands from the repository root after installing the Python environment. Install the
Cloudflare package, confirm Wrangler authentication, and ensure a Docker-compatible container
runtime is available before building the Sandbox image:

```sh
npm --prefix cloudflare ci
(cd cloudflare && npx wrangler whoami)
```

```sh
cp cloudflare/providers.example.json cloudflare/providers.json
```

Put the referenced provider keys in a local dotenv file and restrict it to the current user:

```dotenv
CONTROL_PLANE_TOKEN=replace-with-at-least-16-bytes
RUN_TOKEN_SECRET=replace-with-at-least-32-bytes
OPENROUTER_API_KEY=replace-me
GROQ_API_KEY=replace-me
```

```sh
chmod 600 cloudflare/.env.cloudflare
uv run looplane cloudflare providers apply cloudflare/providers.json \
  --secrets-env cloudflare/.env.cloudflare
```

The command validates the complete manifest first, uploads all referenced keys in one
`wrangler secret bulk` stdin request, builds the pinned runtime, and deploys the catalog. It never
writes provider keys into the catalog or passes them in process arguments. Use `--dry-run` to
validate and build without uploading secrets or deploying, and `--env NAME` for a named Wrangler
environment. `wrangler.jsonc` keeps remotely managed variables so a later regular runtime deploy
does not erase the applied catalog. `CONTROL_PLANE_TOKEN` and `RUN_TOKEN_SECRET` are optional in an
existing deployment; include them in the same dotenv file to bootstrap a new Worker without two
additional secret commands. Dry-run still reads and validates every provider key named by the
manifest; each real apply also requires all of those provider keys, even when bindings already
exist remotely.

Known shorthand profiles and their required dotenv names:

| Provider | Dotenv key |
| --- | --- |
| `openrouter` | `OPENROUTER_API_KEY` |
| `deepseek` | `DEEPSEEK_API_KEY` |
| `groq` | `GROQ_API_KEY` |
| `moonshotai` | `MOONSHOT_API_KEY` |
| `zai` | `ZAI_API_KEY` |
| `xai` | `XAI_API_KEY` |
| `nvidia-nim` | `NVIDIA_API_KEY` |
| `opencode-zen` | `OPENCODE_ZEN_API_KEY` |
| `ollama-cloud` | `OLLAMA_CLOUD_API_KEY` |

Custom providers require all three routing fields in addition to provider and model:

```json
{
  "provider": "trusted-gateway",
  "model": "operator-approved-model-id",
  "apiUrl": "https://models.example/v1/chat/completions",
  "apiKeyBinding": "MODEL_PROVIDER_KEY_TRUSTED_GATEWAY",
  "apiKeyEnv": "TRUSTED_GATEWAY_API_KEY"
}
```

Apply such a manifest only with `--allow-custom-endpoint`. The endpoint must be credential-free
HTTPS without a query or fragment and must end in `/chat/completions`; enabling it sends the named
credential to that host, so use it only for a trusted upstream.

After deployment, call authenticated `GET /v1/model-profiles` and check that the selected profile
reports `ready: true`. This proves only that its secret binding is non-empty. A real `/v1/runs`
smoke is still required to validate the credential, model ID, and upstream behavior.

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
retains a sorted `/opt/looplane/python-packages.txt` inventory for later provenance checks.

The M6 live evidence retains one completed Worker to Sandbox to Groq coding run with a verified
patch and check. The current endpoint now starts runs asynchronously and exposes durable status,
events, and terminal artifacts, but it is still not a full hostile-code containment claim.

## Durable Object configuration

`wrangler.jsonc` now declares three Durable Objects:

- `Sandbox`, registered by migration `v1`
- `RUN_CAPABILITIES` / class `RunCapability`, registered as a new SQLite class by migration `v2`
- `RUN_SESSIONS` / class `RunSession`, registered as a new SQLite class by migration `v3`

The capability object stores only model profile, provider, model, profile fingerprint, expiry,
maximum requests, and consumed count. It stores no endpoint, secret-binding name, provider key,
source, prompt, artifact, or raw run token. Run status and terminal artifacts are persisted through
`RunSession`.
