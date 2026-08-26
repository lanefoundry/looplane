# M3 real-provider E2E audit

> Audit date: 2026-08-21 (Asia/Taipei)
> Scope: read-only provider/auth/model discovery. No OAuth flow, token export, model download, or
> remote mutation was performed. Secret values and external CLI credential files were not read.

## Executive finding

Only one provider is immediately runnable by `python-coding-agent` in the current process:
loopback Ollama with the already installed `qwen3:4b`. Its transport, canonical tool-call parsing,
and local gateway were previously exercised, but its full coding task failed because the model
produced malformed unified diffs. The in-progress M3 `replace_text` tool directly addresses that
observed failure; a fresh full-loop eval is still required before calling the model daily-ready.

The official Codex and Claude Code CLIs are installed and their read-only auth status commands
succeed. That does **not** make them PCA providers:

- PCA's app-owned Codex credential file and its parent state directory do not exist. The direct
  `openai-codex` adapter has mocked protocol tests only and cannot run live until the user performs
  PCA's separate OAuth grant.
- Claude Code reports an authenticated `claude.ai` method, but PCA has no Claude subscription
  transport. The native `anthropic` adapter consumes `ANTHROPIC_API_KEY`, which is API billing and
  is not the Claude Code subscription.
- Invoking an official CLI as a subprocess is technically possible without scraping its token,
  but that bridge does not yet exist and is not equivalent to a neutral model API. It must have an
  explicit contract, policy review, bounded subprocess behavior, and real eval before adoption.

API-key providers are implemented but their expected variables are absent from the current login
shell. A separate QuidProQuo `.dev.vars` has some relevant provider key *names*, but PCA must not
implicitly read another project's secrets. That file is Git-ignored but currently mode `0644`, so
it is also not an acceptable credential store for this agent without an explicit user-controlled
injection path and tighter local permissions.

## Evidence gathered safely

### Host and local model inventory

| Item | Observed state | What it proves |
|---|---|---|
| Host | Apple M3 Pro, 12 cores, 36 GB unified memory | Ample headroom for the installed 4B quantized model; not proof that any uninstalled larger model works |
| Free data-volume space | about 106 GiB | Disk is not the immediate constraint; no download was attempted |
| Ollama | installed, service responding, version `0.32.5` | Local endpoint is reachable today |
| `qwen3:4b` | installed, Q4_K_M, 4.0B, 2.5 GB, advertises tools | Best immediately runnable candidate |
| `qwen3:0.6b` | installed, 522 MB | Transport candidate only; prior full run showed inadequate edit behavior |
| `ollama ps` | no model loaded at audit time | No inference workload was active |

The advertised 262,144-token context is model metadata, not a tested usable PCA context on this
machine. M3 should record the actual request size and runtime context used rather than treating
that maximum as an operational guarantee.

### CLI and authentication posture

| Surface | Read-only observation | PCA consequence |
|---|---|---|
| `uv run pca` | help renders; `auth` exposes only `login-codex` | Project CLI works from the repository, but has no `auth status` or `auth logout` command |
| Codex CLI | installed (`0.147.0`); `codex login status` exits 0 | A user-owned official CLI session exists, but PCA deliberately does not read it |
| Claude Code | installed (`2.1.237`); auth reports logged in through `claude.ai` | Subscription is usable by Claude Code, not by current PCA code |
| OpenCode | installed (`1.14.48`); auth-list command exits 0 | Reference client only; provider identities were not imported |
| OMP | command absent | No local OMP gateway is available |
| PCA Codex store | expected default `~/.local/state/python-coding-agent/auth/openai-codex.json` absent; state/auth directories also absent | Live `openai-codex` is unavailable; file permission safety is not yet observable |

If the PCA Codex store is later created, the live preflight must use `lstat` only and require:

1. a regular file, never a symlink;
2. exact mode `0600` (no group/other bits);
3. an auth parent directory with mode `0700`;
4. ownership by the current uid;
5. `pca auth status` to validate expiry/refresh without printing account, token, or claims.

The current store implementation enforces regular-file/symlink checks and rejects group/other file
permissions; its save path uses atomic `0600` replacement and secures the parent to `0700`. Those
are code and mocked-test guarantees, not a live credential observation.

### Environment-variable availability

Only presence was inspected; values were never printed.

| Provider route | PCA expects | Current shell | Separate QuidProQuo evidence | Verdict |
|---|---|---:|---|---|
| OpenAI-compatible | `OPENAI_API_KEY`; optional `OPENAI_BASE_URL`/`PCA_API_URL` | absent | Other compatible-provider key names exist, not PCA-authorized | Adapter ready; live auth/config missing |
| Ollama loopback | none; optional `OLLAMA_HOST`/`PCA_API_URL` | no override; default service works | not needed | Ready now |
| Ollama Cloud/API URL | an authenticated compatible endpoint | no PCA credential mapping | `OLLAMA_CLOUD_API_KEY` name exists | Current `ollama` preset is keyless and loopback-oriented; use an explicitly injected compatible key/URL or add a dedicated credential mapping |
| PCA Codex OAuth | app-owned credential store plus `--experimental-subscription` | store absent | unrelated | Blocked on user OAuth grant and live protocol verification |
| Anthropic API | `ANTHROPIC_API_KEY`; optional `ANTHROPIC_BASE_URL` | absent | configured key name is supported in application code, not present in inspected local `.dev.vars` | Unit-tested adapter only |
| Claude subscription | approved Claude Code/SDK bridge | official CLI authenticated | unrelated | Not implemented |
| Gemini | `GEMINI_API_KEY` or `GOOGLE_API_KEY` | absent | `GEMINI_API_KEY` name exists in Git-ignored `.dev.vars` | Strong next remote candidate after explicit injection; not authorized for automatic reuse |
| Workers AI | `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN` | absent | application code knows these names; inspected `.dev.vars` did not expose them | Unit-tested adapter only |
| Groq/NVIDIA/Cerebras compatible APIs | compatible URL + key | PCA-specific variables absent | corresponding key names exist | Generic adapter can work after explicit mapping; no preset or live PCA test |

This audit describes the environment inherited by the current login-shell command. It does not
claim that the user lacks credentials in a password manager, Cloudflare secret store, or another
process.

## Provider gaps by contract layer

| Provider | Construction | Auth preflight | Live text | Live tool call | Full verified edit | Daily-ready |
|---|---:|---:|---:|---:|---:|---:|
| Ollama `qwen3:4b` | yes | yes (keyless loopback) | previously passed | previously passed | previously failed; rerun after `replace_text` | no |
| OpenAI-compatible URL | yes | missing key/URL | unverified here | unverified here | unverified | no |
| PCA Codex subscription | yes, experimental | no app grant | mocked only | mocked only | unverified | no |
| Anthropic API | yes | key absent | mocked only | mocked only | unverified | no |
| Claude Code subscription | no adapter | official CLI auth exists | not a PCA call | not a PCA call | unverified | no |
| Gemini API | yes | key not injected | mocked only | mocked only | unverified | no |
| Workers AI | yes | account/token absent | mocked only | mocked only | unverified | no |

The most important missing product behavior is a provider preflight/status surface. At present a
user discovers an absent credential only during model construction or the first request. Add a
redacted `pca providers status` (or provider-scoped `pca auth status`) that reports protocol, model,
endpoint host, credential source name, credential presence, and last E2E result without values.

## Repeatable E2E eval matrix

### One manifest per provider/model

Store a checked-in, secret-free manifest such as
`evals/providers/<provider>-<model>.toml` with these fields:

```toml
schema_version = 1
provider = "ollama"
protocol = "openai_chat"
model = "qwen3:4b"
endpoint_class = "loopback"
credential_env_names = []
fixture = "tiny-python-bug"
task = "Fix add so the existing test passes. Do not change tests."
allowed_paths = ["src/**"]
checks = [["pytest", "-q"]]
max_steps = 12
wall_time_seconds = 300
repetitions = 5
required_successes = 4
```

Secrets, account ids, raw endpoint query parameters, OAuth claims, and auth-file paths outside the
PCA-owned default must never be serialized into an eval manifest or run bundle.

### Gates and objective pass criteria

| Gate | Action | Pass evidence |
|---|---|---|
| E0 discovery | Resolve adapter, protocol, endpoint class, model, and credential source | No secret value printed; provider/model identity matches manifest; unsafe HTTP/custom-host rules remain enforced |
| E1 transport | One minimal text-only `ModelProvider.complete` | Non-empty canonical content, terminal finish reason, non-negative usage, bounded latency, clean close |
| E2 tools | Ask for one exact `read_file` call against a synthetic path | Exactly one canonical call, correct name and JSON object arguments, unique call id; no filesystem mutation |
| E3 edit | Run the tiny bug through the real agent loop | Model reads target before edit and requests `replace_text` or a valid bounded patch; only the allowed source file changes in disposable workspace |
| E4 verification | Let the harness rerun declared check | `pytest -q` exit 0 recorded in `verification.json`/`test.log`; model text alone is insufficient |
| E5 completion | Inspect result and artifacts | `status=completed`, `terminal_reason=verified`, expected `changes.patch`, contiguous events, non-zero real-provider usage where supplied |
| E6 isolation | Compare source repository before/after | Original HEAD, porcelain status, and target bytes are identical; all edits stay in disposable workspace |
| E7 recoverability | Interrupt after a read-only turn, then resume | Same run id/workspace, increasing event sequence, persisted budgets; ambiguous side effect still fails closed |
| E8 repetition | Repeat with fresh run ids/workspaces | Smoke: 1/1 success. Daily-ready: at least 4/5 tiny-bug successes, no policy breach, and median/p95 latency plus tokens recorded |

Daily-ready should additionally require two non-identical fixtures: one multi-step read/edit/check
task and one expected-failure task that must stop safely. A model passing only the one-line
calculator fixture is provider-E2E-capable, not yet a trustworthy daily coding model.

### Suggested command shape

The eventual runner should avoid ad-hoc shell substitution and emit a machine-readable summary:

```bash
uv run pca-eval provider \
  --manifest evals/providers/ollama-qwen3-4b.toml \
  --run-root /absolute/path/outside/source/repository
```

Until `pca-eval` exists, the exact Ollama full-loop smoke can be run manually after M3 editing tests
pass:

```bash
uv run pca run \
  --repo evals/fixtures/tiny-python-bug \
  --provider ollama \
  --model qwen3:4b \
  --task 'Fix add so the existing test passes. Do not change tests.' \
  --allowed-path 'src/**' \
  --check 'pytest -q' \
  --tool-calling \
  --unsafe-local-exec \
  --max-steps 12 \
  --wall-time 300 \
  --run-root /tmp/pca-provider-evals
```

The fixture directory itself must be a clean Git repository for this command. The eval runner
should create a temporary Git copy from the checked-in fixture rather than mutating or initializing
the source fixture in place.

## Recommended execution order

1. **Run Ollama `qwen3:4b` first.** It is the only no-auth, already-installed real provider. Finish
   and test `replace_text`, then execute E0-E6 and retain the run bundle.
2. **Add the eval runner/manifest before trying more providers.** Otherwise every provider will be
   judged by a different prompt, timeout, or artifact checklist.
3. **Use an explicitly injected Gemini or compatible API key next.** QuidProQuo shows likely local
   availability by key name, but the user must choose to export/copy it into PCA's process; PCA
   should never import `.dev.vars` automatically.
4. **Add redacted provider/auth status.** This is necessary for a daily CLI and lets Codex OAuth be
   audited without reading tokens.
5. **Choose the subscription strategy explicitly.** For Codex, prefer the existing app-owned
   Responses adapter after a separate user grant. For Claude Code, either document API-key-only
   support or implement an opt-in subprocess/official-SDK bridge after current authorization terms
   are verified. Do not label a bridge complete merely because `claude -p` returns text.
6. **Only consider a larger local model after the 4B matrix is recorded.** This host has capacity
   headroom, but no larger model is installed or tested. Select a tool-capable quantized candidate
   with conservative memory headroom, then rerun the identical manifest; do not infer quality from
   parameter count or advertised context alone.

## Completion boundary for the parent goal

The provider portion of the daily-usable CLI goal is not complete today. It becomes evidenced only
when:

- at least one real provider passes E0-E8 and the broader daily fixture set;
- Codex subscription has a live app-owned grant and full tool/edit/check E2E, or is explicitly
  removed from the promised provider list;
- Claude subscription has a policy-compatible implemented bridge and the same E2E proof, or the
  product truthfully says Claude API-key only;
- custom/Ollama API URL credential mapping is explicit and tested, rather than relying on another
  project's env file;
- the CLI can report provider readiness without revealing credentials.

Until then, the accurate statement is: the canonical provider interfaces and mocked adapters are
implemented; local Ollama transport works; daily coding reliability and subscription bridges are
still under active verification.
