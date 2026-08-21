# M8 independent release review

Date: 2026-08-22

Verdict: **GO**

## Reviewed risks

- `pca -p` is headless even when its stdin is attached to a TTY. Missing model or prompt exits with
  an actionable error and never opens setup.
- An explicit CLI/environment provider is locked through onboarding instead of becoming an
  advisory picker default.
- Ollama discovery uses a fixed loopback URL, disables proxy environment use, requests identity
  encoding, and enforces timeout, byte, count, name-length, deduplication, and printable-name bounds.
- Workers AI is ready only when both account ID and API token exist; partial state remains
  actionable and no credential value is printed or persisted.
- The isolated real onboarding flow detected two local Ollama models, saved only provider/model/API
  URL at mode `0600`, and reached the repository task prompt.

## Independent verification

```text
targeted CLI/onboarding/config: 37 passed
full suite: 217 passed
ruff: passed
uv build: sdist and wheel passed
git diff --check: passed
```

`openai-codex` remains deliberately absent from the generic picker because its app-owned OAuth
grant and experimental flag are a separate boundary. Explicit locked selection remains available.
