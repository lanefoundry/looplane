# M6 Worker control-plane plan

- [x] Freeze the ingress, source, command, model, token, and output bounds.
- [x] Implement the authenticated run route and isolated Sandbox lifecycle.
- [x] Implement the HMAC run capability and restricted internal model proxy.
- [x] Package the Python runtime and fixed container runner.
- [x] Add contract/security tests and generated Worker bindings.
- [x] Run tests, TypeScript, Wrangler type drift, Docker build, and dry-run checks.
- [x] Close review HIGHs: token file + non-root wrapper, exact SDK/result validation,
      Durable Object request budget/revocation, streamed result read, and bounded cleanup.
- [x] Pin the Sandbox base by digest, export hash-locked Python dependencies plus CycloneDX,
      and prove two clean builds produce the same image ID.

Deployment and a bounded real provider run are milestone evidence steps performed only after the
local release review is GO.

Verified 2026-08-21: 35 Vitest cases across Worker and capability DO contracts, strict TypeScript,
generated binding drift check, `wrangler deploy --dry-run`, and image smoke proving the wrapper is
root-owned `0555`, drops to non-root `pca`, consumes the token file, and writes owner-only output.
The final local image ID is `sha256:c658b3ed4175369952156d61108081e1aea9d9f46af3ae25b28bca2d43238501`
for two independent builds from the same source and lockfiles.
