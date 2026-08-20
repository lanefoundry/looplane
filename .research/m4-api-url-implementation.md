# M4 remote API URL model-contract implementation

> Date: 2026-08-21 (Asia/Taipei)
> Ownership: `src/coding_agent/models.py`, `tests/test_models.py`, and this report only.
> No QuidProQuo secret value or credential file was read. No remote authenticated request was made.

## Outcome

The common `OpenAICompatibleModel` already had the correct transport abstraction for remote
Ollama-compatible and other OpenAI Chat-compatible endpoints:

```python
OpenAICompatibleModel(
    model="tool-capable-model",
    base_url="https://operator-gateway.example/v1",
    api_key=credential,
    provider_name="ollama",
    supports_tool_calling=True,
)
```

No Ollama-specific model class or relaxed URL rule is needed. This change makes the credential
contract less ambiguous and adds a direct remote-Ollama-compatible regression test:

- `api_key` is the canonical explicit credential parameter;
- the older `key` alias remains usable alone, but supplying both is rejected instead of silently
  choosing one;
- an explicit blank/whitespace key is rejected;
- remote HTTPS with an explicit key reaches the SDK with that exact fake test key and retains the
  `ollama` provider identity/tool capability;
- existing tests continue to require a key for remote endpoints and reject remote HTTP, lookalike
  loopback hostnames, URL-embedded credentials, query/fragment routing, relative URLs, and invalid
  ports;
- keyless loopback remains permitted and receives only the synthetic SDK placeholder.

## Current primary-source boundary

Ollama's current official authentication documentation says:

- local `http://localhost:11434` needs no authentication;
- direct programmatic access to the remote Ollama service requires an API key in the Bearer header;
- the documented environment variable is `OLLAMA_API_KEY`.

Ollama's current OpenAI-compatibility documentation confirms `/v1/chat/completions` supports tools
and that the OpenAI SDK requires a placeholder key for the local endpoint even though Ollama ignores
it locally.

Primary sources fetched through the required stealth fetcher:

- <https://docs.ollama.com/api/authentication>
- <https://docs.ollama.com/api/openai-compatibility>

The documentation does not make every arbitrary remote gateway an Ollama-operated service. PCA's
generic HTTPS adapter therefore keeps endpoint ownership/operator trust explicit and does not infer
credentials from the hostname.

## Security/contract audit

### Preserved guards

`_validated_openai_base_url()` runs before SDK construction and still enforces:

1. absolute `http` or `https` URL with a hostname;
2. plaintext `http` only for exact `localhost`, `127.0.0.1`, or `::1`;
3. no username/password embedded in the URL;
4. no query string or fragment;
5. valid port syntax.

After URL validation, client construction requires either:

- an explicit nonblank `api_key`/legacy `key` for a non-loopback endpoint; or
- a caller-owned injected client; or
- exact loopback, where the adapter supplies a non-secret placeholder only because the OpenAI SDK
  requires a nonempty value.

An injected client remains caller-owned and may own its own authentication. It cannot bypass URL
validation when a `base_url` is supplied.

### Deliberately not added

- no environment lookup in the model adapter;
- no reading another project's `.env`/`.dev.vars`;
- no remote HTTP exception for a private-looking hostname or IP;
- no API key in URL/query syntax;
- no automatic reuse of OpenAI, Codex, Claude, or Ollama CLI credential stores;
- no arbitrary per-request upstream URL override.

This preserves the model layer as a reusable transport. Credential source selection belongs to the
CLI/application composition layer.

## Exact CLI integration still required

The current Ollama CLI preset does not pass any `api_key`, so the model layer will correctly reject
a non-loopback URL. The CLI owner should map the official credential name into the existing
parameter without weakening the guard:

```python
resolved_url = base_url or os.environ.get(
    "OLLAMA_HOST",
    "http://127.0.0.1:11434/v1",
)

OpenAICompatibleModel(
    model=model,
    base_url=resolved_url,
    api_key=resolved_ollama_api_key,
    provider_name="ollama",
    supports_tool_calling=tool_calling,
    # retain existing bounded Ollama compatibility options
)
```

`resolved_ollama_api_key` should come from `OLLAMA_API_KEY` only for a non-loopback HTTPS endpoint.
For the default loopback endpoint, pass `None` even if that environment variable exists so a remote
credential is not unnecessarily sent to a local process. The CLI should never print the key or
persist it into request/session/event/result artifacts.

The three public surfaces should use the same mapping:

- bare interactive: `--api-url` / `PCA_API_URL`;
- gateway: `--api-url` / `PCA_API_URL`;
- headless: preferably add `--api-url` as the canonical name while retaining `--base-url` as a
  compatibility alias.

For a generic operator-controlled OpenAI-compatible endpoint, `OPENAI_API_KEY` remains appropriate.
Provider-specific key names should be resolved in the CLI, then passed through the same explicit
`api_key` parameter.

## Verification

Targeted verification on the shared current worktree:

```text
uv run pytest tests/test_models.py -o addopts=''
44 passed in 0.35s

uv run ruff check src/coding_agent/models.py tests/test_models.py
All checks passed!

git diff --check -- src/coding_agent/models.py tests/test_models.py
passed (no output)
```

The new test uses a fake key and a `.test` endpoint; it performs no network request.

## Remaining live proof

This is model-contract completion, not remote-provider E2E. Completion still requires an explicitly
authorized remote tool-capable endpoint and key, then a public CLI run that proves:

- the resolved endpoint is HTTPS and the key is present only in coordinator memory/request headers;
- a real canonical tool call round-trips;
- the coding task completes and deterministic checks pass;
- subprocess and run artifacts contain neither key nor authorization header;
- source repository HEAD/status/bytes remain unchanged;
- missing/blank key and remote HTTP fail before any network request.

Until that run exists, the accurate statement is: remote authenticated URLs are supported by the
model adapter and guarded by tests; the CLI credential mapping and live remote coding E2E are still
separate completion gates.
