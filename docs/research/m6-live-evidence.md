# M6 deployed Cloudflare evidence

Date: 2026-08-21
Verdict: completed and verified

## Deployment

- Worker: `python-coding-agent-control-plane`
- URL: `https://python-coding-agent-control-plane.vincent-xu-work.workers.dev`
- Worker version: `b000d4da-c7a8-4a47-9ed9-fc5fccf4e842`
- Container application: `python-coding-agent-control-plane-sandbox`
- Container application ID: `a03eacca-8af7-40be-a2b4-4562ddceb50d`
- Registry image digest: `sha256:ced2ee89e40a8719d27583cff7ef9a941e3cda4ed02ef6dff815072e077951bd`
- Local image ID for the deployed Python runtime: `sha256:25a65f1f9a03bd6b9c764d3781aada90b18a71ee2cb8c975ea43f52428e2dd50`
- `wrangler containers list`: application state `ready`, one live instance.
- Unauthenticated `GET /healthz`: `{"ok":true,"service":"python-coding-agent-control-plane"}`.

Secrets were injected with Wrangler and are absent from config, request, response, artifacts, and
Git. The local control token is mode `0600` and ignored. An exact-value scan of the retained response
for both the control token and Groq provider key was clean.

## Real coding run

- Provider/model: Groq OpenAI-compatible / `openai/gpt-oss-120b`
- Control-plane run ID: `52afe9fb-7a79-482f-9d2b-eec5c240e113`
- Agent run ID: `9c9cfe05039942e8ada354abaae2a0e7`
- Uploaded source base SHA: `5c5a1b29c3bdab76f0479087bd43c61db89e1e1e`
- HTTP/execution: `201`, success `true`, exit `0`
- Terminal: `completed`, `verified`
- Changed files: exactly `calculator.py`
- Patch: `return left - right` to `return left + right`
- Final check: exact `python3 -m pytest -q`, exit `0`, one test passed
- Tool path: `list_files` retry, `read_file`, `replace_text`, `run_check`
- Event journal: 44 contiguous events ending in `run.completed`
- Usage: 4,599 input, 252 output, 4,851 provider total tokens

## Retained evidence hashes

- request file: `bd216525d2dcfb9b89e9d13646ee90b5d501df2fd538d8c2155632c846da17a3`
- raw response: `2abaded8f4f180262943c464e1cbf88e36fb434537af64f3f024a5c6d2bae0f9`
- returned patch text with extraction newline: `e172725e31ebfc990dfdc4a45b28c4d5895759b30e317ab383fa41dfef8859a3`
- returned events text with extraction newline: `ee94375a6cdb1e660375b5e2e80a9a4eb2a58feee9cf5e77d7173a34966c9df9`
- returned result text with extraction newline: `061276051f15f7734e54bb0e896234b121a6ab2b3cd5d7703dadc15df8ebe1d4`

Files:

- `.research/evidence/m6-live-request.json`
- `.research/evidence/m6-live-response.raw.json`
- `.research/evidence/m6-live-attempt-before-stream-fix.json`

## Failure that changed the implementation

The first deployed call returned `invalid_sandbox_response`. The actual Cloudflare Sandbox SDK
returns `readFileStream()` as SSE framing, while the local mock had returned raw JSON bytes. Commit
`0b65df9` now decodes with the SDK's `streamFile()` utility and enforces the size bound on decoded
file bytes. A second issue was also fixed: `npm run deploy` now rebuilds the current Python wheel
before Wrangler packages the container, so Worker and Python runtime cannot silently drift.

## Evidence boundary

This proves a real Worker to Sandbox to model proxy to Groq to tool/edit/check path on the selected
Cloudflare account. It does not prove a durable queue/status API, multi-tenant authorization,
outbound network confinement for hostile checks, or subscription-token relay. Claude Code and Codex
subscription backends remain local-only by design.
