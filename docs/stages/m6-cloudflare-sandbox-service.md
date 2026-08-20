# M6: Deployed Cloudflare Sandbox service

> Status: complete and deployed.
> Date: 2026-08-21
> Baseline: M5 documentation commit `657bdbd`

## Scope

Keep the interactive Python CLI local while adding a real headless deployment target. A Cloudflare
Worker authenticates and bounds each request, owns the provider credential, creates one disposable
Sandbox, and returns the existing `AgentRunner` artifact bundle. Caller source arrives as a small
UTF-8 file map; Git credentials, provider keys, arbitrary commands, images, URLs, and consumer
subscription tokens never enter the request contract.

## Baseline and acceptance criteria

- deploy the existing Python agent loop rather than a second TypeScript agent;
- accept only bounded source files, allowed paths, model identity, limits, and exact check argv;
- keep the provider key in Worker secrets and give the Sandbox only a run-scoped capability;
- run the fixed Python entrypoint as non-root and keep caller data out of shell command text;
- enforce capability audience, model, expiry, request quota, and teardown revocation;
- stream-bound ingress, provider responses, and decoded Sandbox result files;
- fail closed on SDK errors, terminal/schema mismatch, changed paths, cleanup failure, and timeout;
- pin the container supply chain and prove local image reproducibility;
- complete a real deployed provider/tool/edit/check run and retain auditable evidence.

## References studied

| Reference | Decision used |
| --- | --- |
| Cloudflare Workers and Containers configuration | Worker owns HTTP/auth; one `lite` Sandbox container is a deployment binding |
| Cloudflare Sandbox SDK 0.12.7 types and package implementation | fixed `exec`, checked SDK result objects, bounded destroy, and `streamFile()` decoding for SSE file transport |
| Cloudflare Durable Objects | strongly consistent per-run capability activation, atomic request consumption, expiry, and revocation |
| QuidProQuo harness-system article | reuse the Python loop, tool guards, checkpoints, verification, and artifacts instead of rebuilding the agent in the Worker |
| QuidProQuo agent-security article | treat model output and repository checks as untrusted; put authority in code and process boundaries |
| Pi/OpenCode/OMP provider research from M2/M4 | API transport belongs behind `ModelProvider`; consumer subscription CLIs stay local and are not raw credential proxies |

The platform skill and installed SDK/types were used because the required `stealth_fetch` service
remained unavailable with `Transport closed`. Current behavior was then verified against Wrangler
4.125.0 dry-run and the deployed account rather than asserted from stale web documentation.

## Ideas borrowed and adjustments

The Worker is intentionally a control plane, not the coding agent. It stages validated input,
activates a capability, invokes `/usr/local/bin/pca-sandbox-run`, validates the terminal bundle, and
tears down. The Python wheel contains the same `AgentRunner`, model contract, tools, path policy,
approval policy, checkpoint, patch, and verifier used by local headless mode.

The initial short-lived HMAC design was strengthened after review. The token is written to an
owner-only file rather than process env; a root-owned wrapper changes `/workspace` to a dedicated
`pca` user, applies `no-new-privileges`, and the Python process disables dumpability before consuming
and unlinking the file. A SQLite Durable Object adds active-run state, `maxSteps + 2` request budget,
expiry, and revocation. The provider key never leaves the Worker.

The result boundary validates more than JSON shape: completed results must contain every requested
check exactly once with the same argv and a passing exit, and every changed file must match an
allowed path. Failed/cancelled results may be partial but cannot invent checks. Revoke and destroy
are independently time-bounded and any cleanup failure replaces an otherwise successful response.

## Ideas deliberately not adopted

- no Git URL/archive ingestion or Git/provider credentials in the Sandbox;
- no caller-selected shell, container image, model, upstream URL, or arbitrary dependency install;
- no Claude Pro/Max or ChatGPT subscription relay;
- no multi-tenant queue, background workflow, durable artifact store, status/cancel API, or SLA;
- no hostile-code containment claim while checks and agent share a container with outbound network.

## Implementation and failures found

The image pins the Cloudflare Sandbox base by digest. `uv export --frozen` produces hash-locked
runtime dependencies plus a CycloneDX manifest; the project wheel installs without dependency
resolution. Two identical builds produced the same image ID. `npm run deploy` always rebuilds the
wheel before invoking Wrangler so Python and Worker source cannot drift.

Independent review found and closed four material pre-release issues: environment-readable/replayable
capability, unchecked SDK success/terminal combinations, unbounded result/destroy paths, and a
floating runtime supply chain. A later review added bounded capability revocation and exact response
contract matching.

The first real deployment then found a platform-specific integration error that mocks missed:
Sandbox `readFileStream()` returns SSE framing. Raw JSON parsing therefore failed. Commit `0b65df9`
uses the official `streamFile()` decoder and applies the byte cap to decoded file content; dedicated
tests now preserve both transport framing and oversize behavior.

## Verification evidence

Local final gates:

```text
uv run pytest -q                         190 passed
uv run ruff check .                      All checks passed
npm --prefix cloudflare test             44 passed
npm --prefix cloudflare run typecheck    passed
npm --prefix cloudflare run types:check  generated bindings current
npm --prefix cloudflare run dry-run      Worker, 2 DO bindings, container built
git diff --check                         passed
```

Container evidence confirms root-owned mode `0555` wrapper, non-root `pca` execution, owner-only
workspace/response, consumed token file, blocked same-user `/proc` environment read, six original
sandbox contract tests plus the bounded error test, and repeatable image construction.

Deployed run `52afe9fb-7a79-482f-9d2b-eec5c240e113` used Groq
`openai/gpt-oss-120b`. It returned HTTP `201`, `completed / verified`, changed only `calculator.py`,
produced the exact subtraction-to-addition patch, and passed `python3 -m pytest -q`. The 44-event
journal ends in `run.completed`; exact-value scans found neither control token nor provider key in
the response.

Independent review: `.research/m6-release-review.md`, final verdict GO before the deployed stream
delta; the same reviewer performed a final delta review after the live fix.

## Known limitations

- The endpoint is synchronous and returns an ephemeral bundle; there is no durable queue/status,
  retry, cancellation, artifact retention service, or distributed session resume.
- Uploaded checks execute repository code in the same container as the agent. Token deletion,
  non-dumpable process state, quota, and revocation narrow provider abuse, but outbound network is
  not restricted and hostile multi-tenant execution is not claimed.
- Only a small text-source map and four exact verification commands are accepted; uploaded project
  dependencies are not installed.
- The service currently pins one operator model/upstream and one container instance.
- Local Claude Code and Codex subscription backends are deliberately not exposed through this
  service; remote runs require an authorized API credential.

## Artifact paths

- Architecture: `.research/m6-cloudflare-sandbox-design.md`
- Live evidence and hashes: `.research/m6-live-evidence.md`
- Request/response: `.research/evidence/m6-live-request.json` and
  `.research/evidence/m6-live-response.raw.json`
- Independent review: `.research/m6-release-review.md`
- Worker/service documentation: `cloudflare/README.md`
- Draft practice article: QuidProQuo M6 post, kept uncommitted for review

## Commit

- Initial implementation: `3bafdce`.
- Cleanup/terminal contract: `11549e7`.
- Bounded entrypoint error classification: `cd6e9ba`.
- Build-first deployment lifecycle: `cebe5c9`.
- Safe response-stage diagnostics: `e3a9908`.
- Sandbox SSE file decoding: `0b65df9`.
- Documentation/progress closure: this commit.
