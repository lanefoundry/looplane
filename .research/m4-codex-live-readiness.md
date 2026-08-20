# M4 Codex subscription live-readiness review

Date: 2026-08-21 (Asia/Taipei)

Scope: read-only review of the current `codex_oauth.py`, loopback login helper, CLI wiring,
Codex-focused tests, relevant README/stage documentation, and metadata-only inspection of this
application's own credential location. No token value or credential-bearing environment value was
read. `~/.codex` was not inspected or imported. This report is the only file changed by the review.

## Verdict

**Live Codex E2E is BLOCKED ON USER ACTION, not on an available unattended check.** The default
application auth directory and credential file are both absent. The existing mocked contract tests
pass and support the implementation-level claims, but they cannot establish that the authorization
server currently accepts this client, that the account is entitled to a selected model, or that the
live Codex Responses SSE/tool protocol still matches the adapter.

The experimental adapter can proceed to a user-assisted smoke test. It is **not ready to be
promoted from experimental or claimed as live-verified**. A successful browser grant is necessary
but is not, by itself, evidence that an independently registered third-party OAuth client is
authorized: the implementation uses the public client identifier copied from pinned OpenCode/Pi
references rather than a client registration owned by this project.

## What is implemented and supported by local evidence

- The provider is a dedicated `openai_codex_responses` protocol, not a generic configurable base
  URL. Access tokens can only be sent to the fixed
  `https://chatgpt.com/backend-api/codex/responses` endpoint.
- Login uses an authorization-code flow with PKCE S256, a high-entropy state value, a fixed
  loopback redirect, and `offline_access`. Callback state is compared with
  `hmac.compare_digest`.
- The application owns a separate JSON credential store under the XDG state directory. It rejects
  symlinks, non-regular files, and any group/other permission bits; saves are atomic with a 0600
  file in a 0700 auth directory.
- Credential objects redact their repr, OAuth/provider errors omit response bodies and submitted
  secret values, and request shaping includes the bearer token and ChatGPT account-routing header
  only at the fixed endpoint.
- Expired credentials refresh before use; a 401 triggers exactly one forced refresh and retry.
  Refresh is single-flight within one Python process, and a rotated refresh token is persisted.
- The model adapter is fail-closed unless `--experimental-subscription` reaches its constructor.
  Bare interactive use, `run`, `resume`, and `gateway` all expose that opt-in.
- Mock transport tests cover PKCE parameters, exchange/account extraction, error redaction,
  credential permissions/symlink rejection, in-process refresh coalescing, Codex request/tool/SSE
  translation, native tool-call replay, the 401 retry, and experimental fail-closed behavior.
- Documentation is honest about the current evidence boundary: M2 says HTTP/OAuth tests are
  mocked and claims no live grant; M3 says the app credential was absent and claims no Codex E2E.

The access-token JWT payload is decoded without signature verification only to recover the account
routing identifier. It is not used as a local authorization decision, and the token is received
from the fixed HTTPS token endpoint; this is a conscious routing boundary, not evidence of live
acceptance.

## Metadata-only credential result

The path was derived from the same rule as `_codex_credential_path()`:

```text
${XDG_STATE_HOME:-$HOME/.local/state}/python-coding-agent/auth/openai-codex.json
```

Only existence/type/mode metadata was requested. Current result:

```text
auth_dir=absent
credential=absent
```

Consequences:

1. There is no app grant to load or refresh.
2. A live authenticated request cannot be made without first completing browser authorization.
3. An unauthenticated request could at most prove network reachability and an expected 401/403. It
   would not prove subscription entitlement, accepted model identity, payload compatibility, SSE
   parsing, tool calls, refresh, or the coding loop, so it is not meaningful additional release
   evidence.

## Findings

### High (promotion blocker): no project-owned client registration or current authorization evidence

`CODEX_CLIENT_ID` is documented in source as the public Codex client used by pinned OpenCode/Pi
implementations. The project does create and isolate its own *grant/credential file*, but it does
not own that OAuth client identity. The README's “app-owned OAuth” wording is accurate only in the
credential-isolation sense; it must not be interpreted as a project-registered OAuth application.

This is acceptable behind the current explicit experimental switch and truthful “upstream
authorization can change” caveat. Before stable/product use, obtain current provider authorization
for this integration or keep it permanently experimental/private. A successful grant proves
technical acceptance for that account at that moment, not durable product-policy permission.

### Medium: callback lifecycle is fragile

`login_codex()` calls `webbrowser.open()` before `wait_for_codex_callback()` constructs and binds
the HTTP server. A fast redirect can arrive before port 1455 is listening. The callback helper also:

- binds only `127.0.0.1` while the registered redirect host is `localhost` (an IPv6-first localhost
  resolution can miss the IPv4-only listener);
- uses one fixed port with no fallback or preflight;
- handles exactly one request, so an unrelated/invalid first request consumes the login attempt;
- has no `--no-browser`, manual callback/code fallback, or retry loop;
- collapses provider denial/error callback details into a generic invalid-callback timeout.

PKCE and state prevent credential theft through these conditions, but they do not prevent local
denial of service or a poor login experience. The listener should be bound before opening the URL,
continue until one valid state-bearing callback or deadline, and provide a safe manual fallback.

### Medium: concurrent processes can race refresh-token rotation

The asyncio lock only fences waiters inside one `CodexCredentialManager`. A gateway, CLI run, and
resume process can independently read the same stale refresh token and refresh concurrently. If
the server rotates/invalidate-on-use refresh tokens, one process may persist a credential that
another has invalidated or receive an auth failure. M2 documentation already lists inter-process
OAuth refresh fencing as deferred; this remains a real operational limitation for a daily service.

### Medium: the repeatable live eval runner cannot select the Codex opt-in

`scripts/eval_live_provider.py` always passes `--tool-calling` and `--unsafe-local-exec`, but it has
no `--experimental-subscription` option and never appends that flag to `pca run`. Therefore
`--provider openai-codex` fails at constructor time and cannot be used for the same repeatable
artifact-producing E2E used by M3. A manual `pca run` can opt in, but the durable eval path cannot.

Add an explicit eval-runner opt-in before using it for Codex evidence; never infer the switch merely
from the provider name. The resulting summary should record the opt-in, model, run ID, tool
completion events, changed patch, verification result, and redacted usage without copying auth
artifacts.

### Low: auth and model preflight UX is incomplete

The auth CLI has only `login-codex`; it has no redacted `status`, `logout`, account selector, expiry
status, or credential-path display. The model prompt/help does not discover or verify currently
supported subscription models, and the README necessarily uses `<supported-codex-model>`. A bad
or unavailable model is distinguishable only after an authenticated request.

Also, the `pca run` docstring/help says provider credentials are read only from environment
variables, which is false for `openai-codex`; the README provider table is correct. Add a redacted
preflight/status command and describe “environment variables or the app-owned Codex store.”

### Low: credential storage is permission-hardened plaintext, not an OS keychain

0600/0700, symlink rejection, atomic replacement, and redaction are good filesystem controls, but
the access and refresh tokens remain plaintext JSON readable by the user account and any process
already running with that account's authority. This should be stated plainly. An OS keychain is a
future hardening option, not a prerequisite for the current local experimental flow.

## Minimum user-assisted verification flow

The browser grant cannot be completed correctly without the user authenticating, choosing the
intended ChatGPT organization/account if prompted, reviewing the consent page, and approving or
denying it. Do not automate that decision and do not substitute credentials from `~/.codex`.

### 1. Complete the separate PCA grant

First ensure local port 1455 is free, then run:

```bash
uv run pca auth login-codex
```

The user completes the browser flow. On return, inspect only `lstat`/`stat` metadata first: require
a regular non-symlink file with mode 0600 and a 0700 auth directory. Do not print or hash the file;
even a hash is unnecessary credential-derived material.

### 2. Prove authenticated text transport with the public CLI boundary

Start the loopback gateway with an explicitly confirmed, currently supported Codex model:

```bash
uv run pca gateway \
  --provider openai-codex \
  --model <confirmed-supported-codex-model> \
  --experimental-subscription \
  --port 8787
```

From another terminal, send one small non-streaming `/v1/chat/completions` request asking for an
exact sentinel. Confirm a 200 response, exact configured model, non-empty assistant text, and usage;
then stop the gateway cleanly. This proves grant loading plus live Codex request/SSE translation,
but not tool use or the coding harness.

### 3. Prove canonical tool and coding-loop E2E

Use a fresh Git-initialized copy of the tiny Python fixture and a run root outside it. Invoke the
public headless command with:

```text
pca run
--provider openai-codex
--model <confirmed-supported-codex-model>
--experimental-subscription
--tool-calling
--allowed-path <the one source glob>
--check "pytest -q"
--unsafe-local-exec
--max-steps <finite bound>
--wall-time <finite bound>
```

Accept the result only if durable events contain successful `read_file`, successful editing, and
successful `run_check`/verification completions; the expected single-file patch is exact; the final
result is `completed`/`verified`; the original source worktree remains unchanged; and no request,
event, stderr, result, or patch artifact contains authorization/account credential material.

For reproducibility, first add the explicit experimental flag plumbing to
`scripts/eval_live_provider.py`, then use its predeclared attempts/threshold and retained hashes
instead of treating one lucky run as daily-ready.

## Commands executed

```text
metadata-only stat of the app auth directory and credential path
  auth_dir=absent
  credential=absent

uv run ruff check src/coding_agent/codex_oauth.py src/coding_agent/oauth_login.py \
  src/coding_agent/cli.py tests/test_codex_oauth.py tests/test_oauth_login.py \
  tests/test_cli.py scripts/eval_live_provider.py
  All checks passed!

uv run pytest -q tests/test_codex_oauth.py tests/test_oauth_login.py tests/test_cli.py
  20 passed

uv run pca auth --help
uv run pca auth login-codex --help
uv run pca run --help
uv run python scripts/eval_live_provider.py --help
  all exited successfully; auth exposes login only, run exposes the experimental flag,
  eval_live_provider does not
```

No network call, browser authorization, token load, token refresh, model request, or external
credential import was performed during this review.
