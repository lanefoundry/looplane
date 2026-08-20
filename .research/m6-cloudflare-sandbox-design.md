# M6 Cloudflare Sandbox service design

Date: 2026-08-21
Status: deployed implementation and real provider coding run verified

## Objective

Run the existing Python `AgentRunner` in a disposable Cloudflare Sandbox while a Worker owns
HTTP authentication, provider credentials, model routing, input bounds, and teardown. This is a
headless deployment target; it does not replace the local interactive `pca` CLI.

## Boundary map

1. The authenticated caller sends task metadata and a bounded UTF-8 source-file map to the Worker.
2. The Worker validates paths, sizes, limits, model identity, and exact verification argv before it
   allocates a unique Sandbox.
3. The Worker writes only validated text files plus `request.json` into `/workspace` and executes the
   fixed root-owned wrapper `/usr/local/bin/pca-sandbox-run`.
4. The Python entrypoint creates a clean Git snapshot, runs the existing bounded agent loop in a
   disposable clone, performs the declared checks, and writes a bounded response bundle.
5. The Sandbox receives a short-lived HMAC capability for the Worker's internal model proxy through
   an owner-only file. The non-root entrypoint reads and unlinks it before the model loop starts. The
   upstream provider credential remains a Worker secret.
6. The Worker reads the bounded response, returns JSON, and destroys the Sandbox in `finally`.

## Capability claims

The run capability is bound to one random run ID, one configured model, one audience, and a short
expiry. A SQLite-backed Durable Object atomically enforces the active run, expiry, model binding, and
`maxSteps + 2` request budget, then revokes the run before teardown. The model proxy reconstructs
the upstream request rather than forwarding arbitrary headers or URLs. It rejects streaming, model
changes, oversized bodies, and oversized provider responses.

The root-owned wrapper changes the staged workspace to a dedicated non-root user and enables Linux
no-new-privileges. The Python entrypoint disables process dumpability before reading the capability;
local image evidence confirms a same-user child cannot read the agent process environment. This is
not a complete hostile-repository boundary: checks and the agent still share one container and
unrestricted outbound network. M6 therefore makes no production hostile-code containment claim.

## Deliberate exclusions

- No Claude Pro/Max or ChatGPT subscription relay. Those remain local official-CLI backends.
- No Git credentials, provider API keys, SSH agent, or host credential store in the Sandbox.
- No caller-selected shell command, image, upstream URL, or model.
- No binary or symlink upload in the first slice; source ingress is a bounded UTF-8 file map.
- No durable multi-tenant queue, background workflow, or production SLA claim.

## Acceptance evidence

- Python sandbox-entry contract tests execute the real `AgentRunner` and preserve the uploaded
  source snapshot.
- Worker tests cover authentication, traversal and metadata rejection, aggregate bounds, fixed
  execution, capability expiry/binding, model-proxy bounds, sanitized provider failure, and teardown.
- Python full tests and Ruff pass.
- Worker Vitest, TypeScript, generated binding drift, Wrangler dry-run, and Docker build pass.
- The base image is digest-pinned; Python dependencies are hash-locked and exported as CycloneDX;
  two clean local builds produce the same image ID.
- The deployed evidence is a full Worker to Sandbox to model-proxy to provider to edit/check run;
  its IDs and hashes are retained in `.research/m6-live-evidence.md`.

## Current platform evidence

Using Wrangler 4.125.0 with the inherited `CLOUDFLARE_ACCOUNT_ID` removed, `wrangler containers
list` succeeded and showed both the existing Groundlane application and the PCA Sandbox application.
The PCA Worker, two Durable Objects, container, Wrangler secrets, health route, and one real Groq
coding run are now deployed and verified; the exact evidence boundary is recorded separately.
