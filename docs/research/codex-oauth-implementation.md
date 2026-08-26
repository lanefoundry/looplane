# Experimental ChatGPT/Codex OAuth provider

Date: 2026-08-21 (Asia/Taipei)

Implementation:

- `src/coding_agent/codex_oauth.py`
- `tests/test_codex_oauth.py`

This implementation is based on the pinned upstream comparison in
`docs/research/provider-bridge-comparison.md`. It did not read or reuse any value
from Codex CLI, Claude Code, Pi, OpenCode, or OMP credential storage. All tests
use generated fake JWT claims and mocked HTTP transports.

## Boundary

`openai_codex_responses` is not an alias for an OpenAI-compatible URL. It has a
fixed credential audience and transport:

- authorization issuer: `https://auth.openai.com`;
- model endpoint: `https://chatgpt.com/backend-api/codex/responses`;
- OAuth PKCE grant owned by this application;
- ChatGPT account routing claim/header;
- Codex-flavoured Responses request and SSE events.

The adapter has no `base_url` argument. Consequently its OAuth bearer cannot be
accidentally sent to a custom gateway, Ollama, or another OpenAI-compatible
host. Generic API URLs belong in a different provider implementation with a
different credential source.

## Authorization status

The pinned OpenCode documentation says ChatGPT Plus is supported, while current
OpenCode and Pi sources implement the same public OAuth client identifier and
Codex backend protocol. That is useful implementation evidence, but it is not a
durable OpenAI product-policy guarantee for this new application.

Therefore the adapter is fail-closed unless construction explicitly passes
`experimental=True`. Before a release, recheck current first-party terms and
protocol behavior. If that evidence changes or becomes ambiguous, keep the
feature disabled. Do not work around a rejected grant, copy another client's
credential, or silently fall back to token scraping.

## Login integration contract

The OAuth client deliberately has no browser or callback-server side effects:

1. `CodexOAuthClient.begin_login()` returns an authorization URL, PKCE verifier,
   state, and fixed redirect URI.
2. The CLI opens the URL and listens only on loopback, or accepts the full
   callback URL/manual code.
3. The CLI must verify callback `state` exactly before passing the code to
   `exchange_code()`.
4. The returned credential is saved through `CodexCredentialStore.save()`.

The PKCE verifier, state, callback code, access token, refresh token, and account
ID must never be written to run events or normal logs. The callback listener
should be short-lived, bind to `127.0.0.1`, validate the route and state, and
return a generic success/error page.

## Credential controls

- The project owns a separate JSON grant; it never imports another harness's
  credential file.
- The credential representation is redacted.
- The store rejects symlinks/non-regular files and group/world-accessible files.
- Parent directory and credential mode are forced to `0700` and `0600`.
- Refresh-token rotation is written to a same-directory temporary file, fsynced,
  and atomically replaced.
- Concurrent refresh is single-flight within the process and rechecks disk after
  taking the lock.
- A 401 forces exactly one refresh and retry.
- OAuth and inference errors report status/category only and do not echo upstream
  bodies which might contain submitted or returned secret material.

An inter-process refresh lock is not implemented yet. Until it exists, only one
Rockcode process should own a given credential file. A future gateway daemon
should be the canonical writer, matching OMP's broker/gateway separation.

## Transport subset

The adapter implements HTTPS + SSE only. It converts the neutral conversation
contract into Codex Responses input items and supports:

- system instructions;
- user/assistant text;
- function definitions;
- function calls with provider item IDs retained in `provider_metadata`;
- function call outputs;
- output text, completed function calls, usage, cached tokens, and terminal
  status.

WebSocket transport, image content, encrypted reasoning replay, prompt-cache
session IDs, retry/backoff for 429/5xx, and richer incomplete/error metadata are
deliberately deferred. The request asks for `reasoning.encrypted_content`, but
the current neutral `ModelTurn` does not retain it; long-lived multi-turn
reasoning continuity needs an explicit contract extension before claiming full
Codex parity.

## Verification

Executed:

```text
uv run pytest tests/test_codex_oauth.py
11 passed

uv run ruff check src/coding_agent/codex_oauth.py tests/test_codex_oauth.py
All checks passed!
```

Coverage includes PKCE parameters/challenge, mocked code exchange, error
redaction, atomic permissions, symlink rejection, single-flight refresh, fixed
endpoint/header/body shaping, SSE text/tools/usage decoding, native tool replay,
401 refresh, and experimental opt-in.
