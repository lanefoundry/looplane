# Cloudflare Sandbox control plane

This subproject is the bounded M6 Worker/Sandbox slice. It accepts one synchronous coding run,
stages a small text-only source tree in a fresh Sandbox, invokes one fixed Python entrypoint, reads
the bounded result bundle, and destroys the Sandbox in `finally`.

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

The accepted terminal HTTP status is `201`; `output.ok` carries the agent's terminal success. Exit
`0` is accepted only with `ok: true` plus a `completed` result. Exit `1` is accepted only with
`ok: false` plus a `failed` or `cancelled` result and the full artifact bundle. Every other
exit/result/schema combination is a fail-closed `502`. The route is currently synchronous and has no
durable run-artifact/status/cancel API. Capability revocation and Sandbox teardown happen before the
response returns.

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

The root-owned, mode `0555` wrapper validates the staged workspace and token file, changes the
workspace owner to the image's non-root `rivumi` user, sets the token to owner-only mode `0600`, and
uses `setpriv --no-new-privs` before invoking the fixed Python module. Caller data is never inserted
into a shell command.

## Model capability boundary

The Sandbox receives a five-minute HMAC capability containing only route audience, run ID, model,
issued time, and expiry. The Worker writes it to `/workspace/.rivumi-run-token`, then the non-root
Python entrypoint opens it without following links and immediately unlinks it. The capability is
never present in the Sandbox exec environment.

Each run also owns a strongly consistent `RunCapability` Durable Object. The Worker activates it
with a `maxSteps + 2` model-request budget, atomically consumes one unit before each upstream call,
and revokes it before Sandbox teardown. A correctly signed token is therefore rejected after
teardown, after expiry, after budget exhaustion, or for a different model. This state is backed by
Durable Object SQLite rather than an isolate-local map.

The Sandbox calls `/internal/v1/chat/completions`; that route verifies both the HMAC and active DO
state, pins the operator model, rejects extra request fields/streaming, caps output tokens, and
bounds request and response bodies while streaming them into memory.

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
patch and check. The synchronous endpoint and ephemeral result bundle are still not a production
durability or hostile-code containment claim.

## Durable Object configuration

`wrangler.jsonc` now declares two Durable Objects:

- `Sandbox`, registered by migration `v1`
- `RUN_CAPABILITIES` / class `RunCapability`, registered as a new SQLite class by migration `v2`

The capability object stores only model, expiry, maximum requests, and consumed count. It stores no
provider key, source, prompt, artifact, or raw run token. Run artifacts and status are still not
durably persisted by this slice.
