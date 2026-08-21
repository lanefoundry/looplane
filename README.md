# Python Coding Agent

Temporary engineering name for a Python-first coding agent. It works from a fixed Git commit in a
disposable clone, gives a model a small bounded tool set, reruns deterministic checks, and returns a
patch plus an auditable run bundle. The source repository is never edited.

The public product name remains deliberately deferred. The local CLI is usable today, and the
separate `cloudflare/` control plane contains the bounded Worker + Sandbox deployment slice.

## Current capabilities

- Explicit provider-neutral agent loop with step, repetition, and wall-time guards.
- Common `ModelProvider` contract with canonical messages, tool calls, capabilities, usage, and
  errors.
- Adapters for OpenAI-compatible APIs, Anthropic, Gemini, Cloudflare Workers AI, and deterministic
  scripted tests.
- Disposable Git clone pinned to a full base commit.
- Bounded `list_files`, `read_file`, `search_text`, `replace_text`, `apply_patch`, `run_check`, and
  `git_diff` tools.
- Segment-aware path allowlists, traversal/symlink protection, exact command argv, process-group
  timeouts, bounded output capture, and a subprocess environment without model/GitHub credentials.
- JSONL events, atomic checkpoints, final patch, test log, and result artifacts.
- Bare `pca` interactive entry with live tool/check traces and approval before modify/execute.
- Versioned `session.json`, OS writer fencing, strict workspace/event validation, and
  `pca resume` for interrupted non-terminal runs.
- State-first event journaling repairs a crash between manifest commit and JSONL append. An
  interruption after `tool.started` or `verification.started` is deliberately not auto-resumed,
  because the process cannot prove whether that side effect completed.
- Explicit model protocols, loopback Ollama, custom OpenAI-compatible API URLs, an experimental
  app-owned ChatGPT/Codex OAuth transport, and an optional bounded local model gateway.
- A PCA-audited `ExternalCodingRunner` for local delegation to the installed official Codex and
  Claude Code CLIs. They edit only a pinned disposable clone; PCA independently checks the full
  path-bounded patch and runs exact final verification. They never become `ModelProvider`s.

## Set up with uv

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```

The `.venv/` directory is created and managed by `uv`; dependencies are defined in
`pyproject.toml` and locked in `uv.lock`. There is no separate `requirements.txt`.

Install the editable daily command once:

```bash
uv tool install --editable /Users/xiaoxu/Projects/python-coding-agent
pca --help
```

## Offline proof

Run the deterministic fixture through the real loop and tool execution path:

```bash
uv run python scripts/demo_fixture.py
```

The command prints a completed `RunResult` and retains the run bundle below `runs/`.

## Repeatable real-provider eval

The live eval invokes the public headless CLI in five fresh Git repositories and requires at least
four verified completions. It checks the exact changed file and patch, the required edit tool, and
that the source repository's HEAD, status, and bytes remain unchanged:

```bash
eval_root=$(mktemp -d /tmp/pca-live-eval.XXXXXX)
uv run python scripts/eval_live_provider.py \
  --provider ollama \
  --model qwen3:4b \
  --output-dir "$eval_root/ollama-qwen3-4b"
```

The 2026-08-21 M3 gate passed 5/5 attempts against a real local Ollama service. This is evidence
for the committed tiny-calculator fixture and provider configuration, not a claim that a 4B model
can reliably complete arbitrary repository tasks.

## Interactive CLI

Bare `pca` runs this project's Python loop; it does not launch Codex or Claude Code behind the
scenes. On a real terminal it opens a full-screen Textual application with repository/model
context, a task composer, live harness events, approval dialogs, safe Stop, and the final patch /
verification summary. Its daily surface follows the familiar Claude Code, Codex, Pi, and OpenCode
conventions:

```bash
# First run: choose a provider and model. Local Ollama models are discovered automatically.
pca config --interactive

# Or save non-secret defaults directly. API keys remain environment variables.
pca config --provider ollama --model qwen3:4b

cd /path/to/a/git/repository
pca
pca 'Fix the failing test without changing its intent.' --check 'pytest -q'
pca -C /path/to/another/repo 'Explain and fix the failure.'
pca --plain  # line-oriented fallback for limited terminals and SSH troubleshooting
```

For a one-off model selection, `-m ollama/qwen3:4b` also selects both the provider and model,
matching the compact provider/model form used by Pi and OpenCode.

`pca [PROMPT]` is interactive, `pca -p [PROMPT]` and `pca exec [PROMPT]` are non-interactive,
and `pca resume` resumes the latest validated non-terminal session. `pca run`, `--task`, and
`--repo` remain compatibility aliases. `-p` now means `--print`, as it does in Claude Code and Pi;
use the long `--provider` option or a saved config default to choose a provider.
Headless `-p`/`exec` checks still require `--unsafe-local-exec`; `exec` also requires
`--tool-calling` unless the chosen transport is intentionally text-only.

On an unconfigured TTY, bare `pca` opens provider-aware setup before asking for a coding task.
It offers models from a bounded fixed-loopback Ollama discovery request and otherwise asks for a
provider model ID. `-p` and `exec` never open setup or prompt, even when attached to a TTY; missing
configuration or prompt fails with an actionable command. The experimental `openai-codex`
subscription transport remains outside interactive setup and must be selected explicitly with its
required experimental flag.

`Ctrl+C` in the full-screen application requests a cooperative stop. A pending model request can
stop immediately; a tool or verification command that has already started is allowed to finish its
bounded execution and durable completion event before the session closes. This avoids abandoning a
background thread while claiming the run is safely resumable. `PCA_NO_TUI=1` is equivalent to
`--plain` for terminals that cannot use an alternate screen.

The config path is `${PCA_CONFIG:-${XDG_CONFIG_HOME:-~/.config}/python-coding-agent/config.json}`.
Its strict schema contains only `provider`, `model`, and `api_url`; unknown fields, embedded URL
credentials, and symlink config files are rejected. Resolution order is command line, environment,
saved config, then built-in default.

Read tools run automatically. A patch or repository-code command is shown in the terminal and
requires `once`, `session`, `deny`, or `cancel`. An interrupted run keeps its disposable workspace:

```bash
uv run pca resume last
uv run pca resume <run-id>
```

Sessions default to `${XDG_STATE_HOME:-~/.local/state}/python-coding-agent/runs`; override with
`PCA_RUN_ROOT` or `--run-root`.

For an API key or an existing OpenAI Chat-compatible proxy:

```bash
export OPENAI_API_KEY='...'
pca config --provider openai-compatible --model your-model \
  --api-url https://gateway.example/v1
export OPENAI_API_KEY='...'
pca -C /path/to/repo 'Fix the failing test.' --check 'pytest -q'
```

Remote API URLs require HTTPS. Plain HTTP is accepted only for exact loopback hosts; credentials,
query strings, and fragments in the endpoint URL are rejected.

## ChatGPT/Codex subscription (experimental)

Pi and OpenCode implement ChatGPT/Codex as a dedicated OAuth + Codex Responses transport, not as a
generic OpenAI-compatible URL. This project follows that boundary and creates its own credential;
it never reads `~/.codex`, Claude Code, Pi, OpenCode, or OMP credential files.

```bash
uv run pca auth login-codex
uv run pca auth status-codex
uv run pca --provider openai-codex --model <supported-codex-model> \
  --experimental-subscription --repo /path/to/repo --task '...' --check 'pytest -q'
```

If the browser callback cannot reach localhost, use `pca auth login-codex --manual`; the pasted
callback is hidden and only the PCA-owned credential store is written. `pca auth logout-codex`
removes that store without touching the official Codex CLI.

This path is deliberately opt-in because upstream authorization and protocol behavior can change.
Its current public OAuth client identity is borrowed from a pinned ecosystem implementation, so it
remains experimental until this project has its own registration and current authorization proof.

Anthropic's current Agent SDK policy says third-party products may not offer `claude.ai` login or
subscription rate limits without prior approval. PCA therefore keeps its own loop on the native
Anthropic API-key adapter. For local/private experiments only, the installed official Claude Code
CLI can edit a disposable clone with only `Read`, `Glob`, `Grep`, and `Edit`; PCA runs the final
check separately:

```bash
pca backend claude-code \
  --repo /path/to/trusted/repo \
  --task 'Fix the failing test.' \
  --allowed-path 'src/**' \
  --check 'pytest -q' \
  --experimental-subscription \
  --allow-external-modify \
  --unsafe-local-exec
```

That command uses the official CLI's own login (and therefore permits that official child to read
its own auth state), but PCA never reads or copies the login. Bash, Write, WebFetch, WebSearch,
MCP, subagent tools, and session persistence are not enabled. This is not evidence for PCA's own
agent loop, and a hosted Claude subscription proxy is intentionally not implemented.

The separately installed official Codex CLI can be used through the same outer harness. It owns
its ChatGPT login and agent loop, runs ephemeral with user config/rules ignored, and receives only
the disposable clone through Codex's `workspace-write` sandbox:

```bash
pca backend codex-cli \
  --repo /path/to/trusted/repo \
  --task 'Fix the failing test.' \
  --allowed-path 'src/**' \
  --check 'pytest -q' \
  --experimental-subscription \
  --allow-external-modify \
  --unsafe-local-exec
```

The two explicit acknowledgements are separate: one permits the external CLI to modify only the
clone; the other permits PCA to execute repository verification code on the host. New untracked
files are rejected in this initial external-coding milestone; use PCA's own `apply_patch` path for
reviewable create/delete work. The source repository must start clean. PCA removes Git metadata
from the child working tree, rejects index/config mutation, hashes all source entries outside
`.git` (including ignored files), and rechecks both source and patch after final verification.

## Local model gateway

`pca gateway` is the OMP-inspired model gateway boundary. It translates OpenAI Chat wire messages
into canonical contracts and dispatches through one configured provider; it is not arbitrary URL
passthrough.

```bash
uv run pca gateway --provider ollama --model qwen3:4b --port 8788
curl http://127.0.0.1:8788/v1/models
```

The MVP exposes `/healthz`, `/v1/models`, and non-streaming `/v1/chat/completions`, binds loopback
only, caps request bodies, and optionally requires `PCA_GATEWAY_TOKEN`. SSE and remote binding are
deferred.

## Headless run

The run directory must be outside the target source repository.

```bash
export OPENAI_API_KEY='...'
export CODING_AGENT_MODEL='your-tool-capable-model'

pca exec 'Fix the bounded bug and keep the existing behavior.' \
  -C /absolute/path/to/a/git/repository \
  --allowed-path 'src/**' \
  --allowed-path 'tests/**' \
  --check 'pytest -q' \
  --tool-calling \
  --unsafe-local-exec \
  --run-root /absolute/path/to/coding-agent-runs
```

Provider credentials are read by the coordinator process and are not forwarded to repository
checks. Python checks also run with bytecode writes disabled so an immediate equal-size source
patch cannot accidentally reuse a stale timestamp-based `.pyc` from an earlier verification.

`--unsafe-local-exec` is intentionally noisy: exact argv prevents shell interpolation, but pytest,
build scripts, and other checks still execute repository code on the host. Use it only for a
trusted repository. Untrusted work requires the later Docker/Cloudflare Sandbox backend.

| Provider | CLI value | Credential environment |
|---|---|---|
| OpenAI or compatible endpoint | `openai-compatible` | `OPENAI_API_KEY`, optional `OPENAI_BASE_URL` |
| Ollama | `ollama` | local: none; remote HTTPS: `OLLAMA_API_KEY`; optional `PCA_API_URL` |
| ChatGPT/Codex subscription | `openai-codex` | app-owned OAuth via `pca auth login-codex` |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY`, optional `ANTHROPIC_BASE_URL` |
| Gemini | `gemini` | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |
| Cloudflare Workers AI | `workers-ai` | `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` |

For `pca exec` and its `pca run` compatibility alias, `--api-url` is the preferred spelling and
`--base-url` remains a compatibility alias. Remote endpoints require HTTPS and an explicit
provider credential; a local Ollama process never receives `OLLAMA_API_KEY` even when it exists in
the parent environment.

Model capabilities vary across every provider. Headless `exec`/`run` keeps tool calling fail-closed
until `--tool-calling` explicitly asserts support. Native Anthropic, Gemini, and Workers AI
credentials are restricted to their official HTTPS hosts; a custom native endpoint additionally
requires the explicit `--allow-custom-provider-endpoint` acknowledgement.

## Run bundle

```text
runs/<run-id>/
  request.json
  events.jsonl
  checkpoint.json
  session.json
  verification.json
  changes.patch
  test.log
  result.json
  workspace/          # disposable clone
```

`result.json` is successful only after the harness reruns every declared verification command.
Model text alone cannot mark a run successful.

## Boundaries

The local runtime does not provide an OS/container sandbox. The disposable clone and Python policy
layer protect the source worktree and narrow tool behavior, but they are not a substitute for
process isolation. Codex CLI adds its own `workspace-write` sandbox; the local Claude Code path has
an exact file-tool allowlist and post-run patch enforcement, but the official child still receives
the user's `HOME` for its own authentication and is not filesystem-isolated by PCA. Do not use
these local backends on hostile repositories. The separate `cloudflare/` service now packages the
project-owned Python runtime behind a thin Worker and Cloudflare Sandbox with a run-scoped model
capability. It remains synchronous and ephemeral; consumer subscription logins are not relayed
there. See [cloudflare/README.md](cloudflare/README.md) for its exact API and deployment boundary.

See [progress.md](progress.md) for current acceptance criteria and
[docs/stages](docs/stages/README.md) for milestone evidence.
