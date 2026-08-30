# Cloudflare looplane Resource Migration

Status: closed; both the mistaken looplane deployment and legacy M6 deployment were deleted

## Correction

The current looplane interactive runtime uses local Claude Agent SDK and Codex app-server sessions backed by the user's Claude/Codex subscriptions. It does not use this Cloudflare model-proxy path and does not require `OPENAI_API_KEY`.

The `cloudflare/` service is the separate legacy M6 hosted sandbox experiment. It deliberately uses a server-side OpenAI-compatible provider credential and explicitly does not relay consumer subscription credentials. The newly created `looplane-control-plane` resources therefore do not represent the current looplane runtime and should not be treated as the rename/cutover of that runtime.

No `OPENAI_API_KEY` was configured or exposed. At the time the mismatch was discovered, both deployments were left intact until the user explicitly authorized destructive cleanup; the cleanup result is recorded below.

## Cleanup result

User confirmed that neither the mistaken looplane deployment nor the legacy M6 deployment needed to be retained.

Permanently deleted:

- Workers `looplane-control-plane` and `python-coding-agent-control-plane`.
- Container applications `looplane-control-plane-sandbox` and `python-coding-agent-control-plane-sandbox`.
- Registry image `looplane-control-plane-sandbox:9b47313e`.
- Registry images `python-coding-agent-control-plane-sandbox:143a97aa` and `python-coding-agent-control-plane-sandbox:c6b76578`.

Cloudflare Durable Object tombstone reconciliation reported both `Sandbox` and `RunCapability` as stale for both Worker names, proving the namespaces no longer existed. Temporary cleanup-only Workers used for that verification were deleted again.

## Goal

Create and verify the looplane-named Cloudflare Worker and Container resources without deleting the existing `python-coding-agent-control-plane` deployment until cutover is proven safe.

## Checklist

- [x] Confirm current remote Worker and Container inventory.
- [x] Confirm local target name is `looplane-control-plane`.
- [x] Audit the dirty `cloudflare/` worktree for deployment safety.
- [x] Verify Wrangler version, authentication, config, and secret-source availability without exposing values.
- [x] Run local tests and `wrangler deploy --dry-run`.
- [x] Deploy the new `looplane-control-plane` Worker and derived `looplane-control-plane-sandbox` Container app.
- [ ] Provision the five required secrets from an approved source without logging values. Four of five are configured; `OPENAI_API_KEY` is missing.
- [ ] Smoke-test health, authentication, and one bounded sandbox/model request. Health and unauthenticated rejection pass; model request is blocked on the missing provider key.
- [x] Verify remote deployment, Container app, and URL.
- [x] Audit repository callers and cutover readiness. No live endpoint references were found outside historical M6 evidence; external callers remain unknowable from the repository.
- [x] Keep the old Worker/Container for rollback until deletion is separately confirmed safe.

## Safety notes

- The repository contains extensive uncommitted rename and TUI work; deployment must not accidentally publish an incoherent snapshot.
- Cloudflare secret values cannot be copied back from the old Worker. Only names are readable; values must come from the original environment or a secure source.
- No secret values are written to this log.

## Pre-deploy verification

- Python tests: 347 passed.
- Cloudflare tests: 44 passed.
- TypeScript typecheck: passed.
- Wrangler generated types check: passed.
- Wrangler dry-run: passed and built `looplane-control-plane-sandbox:worker`.
- Required production secrets found in process environment: none.
- Local secure source found: `.control-token` for `CONTROL_PLANE_TOKEN` only.

## Source fingerprint

The deployment source is currently uncommitted. These hashes identify the tested snapshot:

```text
37c34f4334cceefa8d74b457fe1044e41fa6dc8a62bfc9d450c2c9a9f554ed9e  pyproject.toml
b2021cad308259260233813eb6150a504d2ac93523093e126084c459c9dc01e3  uv.lock
85e4e1c887d26cd7b1cb4fc7fa067a2ac4a002bb7784056f40fa54ba1408b12b  cloudflare/wrangler.jsonc
fc50ce76a8555bc17d960679bd7cde448e83c7d8dad0401541459bf8a2718dd8  cloudflare/Dockerfile
a861c26d46bff89a56557a13a4fada3962a9326756f2cb5b2cdb7f495766e94b  cloudflare/looplane-sandbox-run
e89f97a25b488e0b168fe40d8e44d2a73882ed07617815343ae46bbdeddb2499  cloudflare/src/control-plane.ts
86af1be9da0f394f101330fba43bb581fec1a7106e470cb889a94754e8acf678  cloudflare/.artifacts/looplane-0.1.0-py3-none-any.whl
debbbee888e413d97154b9213fd254603e542e30aa2599221caf6e1d37ffee74  cloudflare/.artifacts/requirements.txt
ed784219f08afbd1cf05d335421cf60059c62257feb7c35b50906e374ce15ded  aggregate src/looplane source hash
```

## New remote resources

- Worker: `looplane-control-plane`
- Initial deployed version: `9b47313e-1b6e-49a7-b480-dbb4b5893192`
- URL: `https://looplane-control-plane.vincent-xu-work.workers.dev`
- Container application: `looplane-control-plane-sandbox`
- Container application ID: `a03fbf6e-37e0-4cc9-bcb1-8ef6866c88e0`
- Container namespace ID: `432e06e2f221474b81c694df33d99fce`
- Registry image digest: `sha256:ce86196c6f9cd590ef7cd06c390a7074139f911aaa07198a16ba29b9b206e942`
- Health response: `{"ok":true,"service":"looplane-control-plane"}`
- Unauthenticated `POST /v1/runs`: HTTP 401 with `{"error":"unauthorized"}`

## Secret state

Configured without logging values:

- `CONTROL_PLANE_TOKEN` from the existing mode-0600 local source.
- `RUN_TOKEN_SECRET` newly generated for this Worker.
- `OPENAI_MODEL` set to the previously verified Groq model `openai/gpt-oss-120b`.
- `MODEL_API_URL` set to Groq's official OpenAI-compatible chat-completions endpoint.

Missing:

- `OPENAI_API_KEY`. Cloudflare Worker secret values are not readable after creation, so the old Worker's value cannot be copied from Wrangler or the dashboard.
