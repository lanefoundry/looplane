# M6 final release review

Date: 2026-08-21

Verdict: **GO**

Scope: independent read-only review of the corrected `cloudflare/**` production slice, plus the
referenced Sandbox entrypoint and lockfile. No deployment or credential access was performed.

## Release decision

No release blocker remains in reviewed HEAD `0b65df9`.

The prior local GO was invalidated by live evidence: Cloudflare Sandbox `readFileStream()` returns an
SSE transport, not raw file bytes. The corrected implementation now injects the SDK's official
`streamFile()` decoder (`cloudflare/src/index.ts:1-23`), iterates its decoded `string` or
`Uint8Array` chunks, and enforces the 1,500,000-byte limit on decoded file content
(`cloudflare/src/control-plane.ts:413-445,817-834`). Decoder failures and decoded oversize remain
fixed, content-free `502` classes. Regressions distinguish SSE framing from decoded JSON and test the
decoded-byte boundary (`cloudflare/test/control-plane.test.ts:334-367`).

The deploy lifecycle now runs `build:runtime` before Wrangler (`cloudflare/package.json:7-13`). The
builder clears `.artifacts`, rebuilds the wheel, and exports frozen hash-locked requirements plus the
CycloneDX manifest before deployment (`cloudflare/scripts/package-runtime.mjs:6-55`). A failed build
therefore stops the `&&` chain instead of allowing a stale wheel to deploy. The supported release
command and bare-Wrangler warning are documented at `cloudflare/README.md:137-139`.

Retained live evidence closes the transport/runtime integration gap: deployed run
`52afe9fb-7a79-482f-9d2b-eec5c240e113` returned HTTP `201`, execution success/exit `0`, and
`completed` / `verified` with the exact passing pytest check. The retained response SHA-256 is
`2abaded8f4f180262943c464e1cbf88e36fb434537af64f3f024a5c6d2bae0f9` and matches
`.research/m6-live-evidence.md:22-43`.

The two provisional blockers are closed:

- Capability revocation has its own five-second bound
  (`cloudflare/src/control-plane.ts:514-535`). Its failure or timeout is caught before the independent
  bounded Sandbox destroy is attempted (`cloudflare/src/control-plane.ts:843-859`), so a pending DO
  RPC can no longer prevent container teardown.
- `validateSandboxResponse()` receives the validated checks and allowed paths
  (`cloudflare/src/control-plane.ts:613-618`). Completed results require the exact unique check
  mapping and argv with passing status (`cloudflare/src/control-plane.ts:672-702`), and every changed
  file must be covered by an allowed path (`cloudflare/src/control-plane.ts:703-715`). Negative
  regressions cover missing, unrelated and failed checks plus an out-of-scope changed path
  (`cloudflare/test/control-plane.test.ts:406-460`).

## Previous findings

| Previous finding | Status | Evidence |
| --- | --- | --- |
| Capability exposure and unbounded replay | Closed | Token is written to a file rather than exec env (`control-plane.ts:791-806`), wrapper drops to non-root (`pca-sandbox-run:7-28`), entrypoint consumes/unlinks the owner-only token and hardens the process (`sandbox_entry.py:40-102,203-213`), and DO consumption/revocation is enforced (`capability-do.ts:80-117`; `control-plane.ts:843-859,884-907`). |
| Unbounded cleanup | Closed | Revocation and SDK destroy have independent bounds and cleanup remains fail-closed (`control-plane.ts:493-535,843-859`). |
| Sandbox response transport and size bound | Closed | Official `streamFile()` decodes the SSE transport, then decoded bytes are bounded before JSON parsing (`index.ts:1-23`; `control-plane.ts:413-445,817-834`). |
| Non-reproducible runtime supply chain | Closed | Base image uses a digest (`Dockerfile:1`); Python requirements are hash-locked and installed with `--require-hashes` (`Dockerfile:4-11`; `scripts/package-runtime.mjs:27-55`); CycloneDX and installed-package inventory are produced. |

## Docker, Wrangler, and Durable Object review

- SDK and Sandbox image are aligned at `0.12.7`; the image digest resolves locally.
- The built image contains `setpriv`, a non-root `pca` user, a root-owned mode `0555` wrapper,
  Python 3.11.14, an importable Sandbox entrypoint, and no broken Python requirements.
- `.dockerignore` limits build context to the Dockerfile, wrapper, wheel, and locked requirements.
- Wrangler declares both DO bindings and sequential SQLite migrations `v1`/`v2`
  (`cloudflare/wrangler.jsonc:13-22`); generated bindings have no drift.
- `RunCapability` uses per-run DO identity plus transactional storage consumption. No provider key,
  source, prompt, raw token, or artifact is placed in DO storage.

No additional Docker, Wrangler, Durable Object, non-root, response-contract, or supply-chain release
blocker was found.

## Verification evidence

- `npm test`: 44/44 passed across two Vitest files.
- `npm run typecheck`: passed.
- `npm run types:check`: passed; generated Worker bindings are current.
- `uv lock --check`: passed.
- `PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest -p no:cacheprovider -q`: 190/190 passed.
- `uv run --frozen ruff check .`: passed.
- `git diff --check`: passed.
- Current local image ID `sha256:25a65f1f9a03bd6b9c764d3781aada90b18a71ee2cb8c975ea43f52428e2dd50`
  resolves to deployed registry digest
  `sha256:ced2ee89e40a8719d27583cff7ef9a941e3cda4ed02ef6dff815072e077951bd`.
- Requirements and CycloneDX artifacts are present, hash-locked, and parse successfully.
- Retained live response fields and SHA-256 were independently checked against the evidence report.

This review did not deploy, invoke a provider, or access credentials. It verified the already-retained
non-secret live evidence. Durable queue/status, hostile-code outbound confinement, multi-tenant auth,
and subscription-token relay remain explicitly outside M6's demonstrated scope.
