# Rivumi

Rivumi is a Python-first coding agent. It works from a fixed Git commit in a
disposable clone, gives a model a small bounded tool set, reruns deterministic checks, and returns a
patch plus an auditable run bundle. The source repository is never edited.

The local CLI is usable today, and the separate `cloudflare/` control plane contains the bounded
Worker + Sandbox deployment slice.

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
- Bare `rivumi` interactive entry with live tool/check traces and approval before modify/execute.
- Versioned `session.json`, OS writer fencing, strict workspace/event validation, and
  `rivumi resume` for interrupted non-terminal runs.
- State-first event journaling repairs a crash between manifest commit and JSONL append. An
  interruption after `tool.started` or `verification.started` is deliberately not auto-resumed,
  because the process cannot prove whether that side effect completed.
- Explicit model protocols, loopback Ollama, custom OpenAI-compatible API URLs, an experimental
  app-owned ChatGPT/Codex OAuth transport, and an optional bounded local model gateway.
- A Rivumi-audited `ExternalCodingRunner` for local delegation to installed coding CLIs — the
  official Codex and Claude Code CLIs plus local-only `opencode`, `pi`, and `omp`. They edit only a
  pinned disposable clone; Rivumi independently checks the full path-bounded patch and runs exact
  final verification. They never become `ModelProvider`s, and Rivumi never reads their credential
  stores.

## Set up with uv

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```

The `.venv/` directory is created and managed by `uv`; dependencies are defined in
`pyproject.toml` and locked in `uv.lock`. There is no separate `requirements.txt`.

Install or refresh the editable daily command:

```bash
scripts/install-dev-cli
rivumi --help
```

Editable installs read source changes immediately, but their isolated dependency environment does
not update when project dependencies change. Run `scripts/install-dev-cli` again after pulling or
editing dependency changes in `pyproject.toml` or `uv.lock`. The script exports locked runtime
constraints, refreshes the isolated tool environment, checks installed dependency compatibility,
and smoke-tests the actual global command. A stale lock or broken tool environment fails the script
instead of leaving a partially synchronized `rivumi` command.

For every TUI layout change, run the geometry tests and render both wide and narrow review images:

```bash
uv run pytest tests/test_tui.py -q
uv run python scripts/render_tui_screenshot.py --width 120 --height 36 --name wide
uv run python scripts/render_tui_screenshot.py --width 60 --height 22 --name narrow
uv run python scripts/render_tui_screenshot.py --state thinking --name loading
for frame in 0 1 2 3 4 5; do
  uv run python scripts/render_tui_screenshot.py \
    --state thinking --loading-frame "$frame" --name "loading-frame-$frame"
done
```

Review the generated `.artifacts/tui/*.png` images before treating the UI change as complete,
including an active state whenever loading or tool feedback changes. When the loading animation
itself changes, render every distinct frame and verify that the indicator moves while its terminal
cell width and status-label position remain fixed. The SVG artifacts are always produced; PNG
conversion uses Quick Look on macOS or ImageMagick when available.

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
eval_root=$(mktemp -d /tmp/rivumi-live-eval.XXXXXX)
uv run python scripts/eval_live_provider.py \
  --provider ollama \
  --model qwen3:4b \
  --output-dir "$eval_root/ollama-qwen3-4b"
```

The 2026-08-21 M3 gate passed 5/5 attempts against a real local Ollama service. This is evidence
for the committed tiny-calculator fixture and provider configuration, not a claim that a 4B model
can reliably complete arbitrary repository tasks.

## Interactive CLI

Bare `rivumi` opens one continuous full-screen coding conversation. With an installed Claude Code or
Codex CLI, the header shows the selected runtime, model, and repository; ordinary answers, tool
activity, edits, and checks stay in one semantic transcript. Side effects pause at the actual tool
boundary for approval. There is no separate Ask/Agent mode to choose before every message.

```bash
# First run: choose the runtime in the full-screen UI.
rivumi

# Or configure Rivumi's own model loop directly. API keys remain environment variables.
rivumi config --provider ollama --model qwen3:4b

cd /path/to/a/git/repository
rivumi
rivumi 'Fix the failing test without changing its intent.' --check 'pytest -q'
rivumi -C /path/to/another/repo 'Explain and fix the failure.'
rivumi --plain  # line-oriented fallback for limited terminals and SSH troubleshooting
```

For a one-off model selection, `-m ollama/qwen3:4b` also selects both the provider and model,
matching the compact provider/model form used by Pi and OpenCode.

The first screen chooses who owns the agent loop:

- **Claude Code** uses the installed official CLI and its own local login. Its model starts at the
  account's `Automatic` default; `Ctrl+L` can switch to Sonnet, Opus, Haiku, or Best.
- **Codex CLI** uses the installed official CLI and its own local ChatGPT login. Its model starts
  at the Codex `Automatic` default; `Ctrl+L` can select a current recommended Codex model.
- **Rivumi Agent** runs this project's provider-neutral Python loop. A discovered local Ollama model
  is selected automatically; API and custom endpoints keep their explicit model IDs.

`Automatic` is represented by omitting the model override, so the official CLI remains the source
of truth as account availability and recommended defaults change. Choosing a model saves only its
non-secret name; Rivumi never persists the official CLI's login or an API key.

The official runtime keeps one native session and one Rivumi-owned disposable committed-HEAD clone
across turns. It may answer without tools, inspect the clone, or request Edit/Bash permissions as
the conversation evolves. Rivumi never edits the source worktree directly: every reported file
change is matched against an independently audited bounded patch. Concurrent changes in the source
worktree do not invalidate the isolated conversation or its cleanup. `Ctrl+L` (or `/model`) changes
runtime/model; `/new`, `/resume`, and `/history`
manage Rivumi-owned conversation continuity without persisting vendor session identifiers.

`rivumi [PROMPT]` is interactive, `rivumi -p [PROMPT]` and `rivumi exec [PROMPT]` are non-interactive,
and `rivumi resume` resumes the latest validated non-terminal session. `rivumi run`, `--task`, and
`--repo` remain compatibility aliases. `-p` now means `--print`, as it does in Claude Code and Pi;
use the long `--provider` option or a saved config default to choose a provider.
Headless `-p`/`exec` checks still require `--unsafe-local-exec`; `exec` also requires
`--tool-calling` unless the chosen transport is intentionally text-only.

On an unconfigured TTY, bare `rivumi` opens runtime-first setup before asking for a coding task.
Installed official Claude Code and Codex CLIs are offered without claiming that their login is
valid. Rivumi's own runtime offers models from a bounded fixed-loopback Ollama discovery request;
unknown custom endpoints ask for a provider model ID only when a run needs one. `-p` and `exec`
never open setup or prompt, even when attached to a TTY; missing configuration or prompt fails with
an actionable command. The separate app-owned `openai-codex` ModelProvider transport remains
experimental and explicit; it is not the official Codex CLI runtime shown by the TUI.

`Ctrl+C` in the full-screen application requests a bounded cooperative stop during an active turn;
when idle it first clears a draft, and a second press within a moment confirms exit (`Ctrl+D` and
`Ctrl+Q` behave the same). `Escape` interrupts an active turn and never closes Rivumi by itself; when
idle, a single `Escape` is invisible and a second press opens `/rewind` when rewindable prompts
exist. Leaving the full-screen UI prints a bounded, app-owned semantic transcript — finalized user
prompts, assistant messages, tool outcomes, and notices plus a copyable `/resume` command — into the
terminal's primary buffer so history survives in scrollback. `RIVUMI_NO_TUI=1` is equivalent to
`--plain` for terminals that cannot use an alternate screen.

The full-screen TUI keeps a persistent metrics footer: the active model, a tool/queue HUD,
an estimated streaming token count (chars/4), a context-pressure readout (`ctx %` turns yellow
above the warning threshold and red above the critical threshold), and elapsed time. `/usage`
shows the aggregated token usage for the session, and `/context` renders the context window as a
segmented pressure bar. Optionally set `statusline_command` in the config so an external command
receives a machine-readable status JSON and renders its own status line, Claude Code-style.

The config path is `${RIVUMI_CONFIG:-${XDG_CONFIG_HOME:-~/.config}/rivumi/config.json}`.
Its strict schema contains only non-secret `runtime`, `runtime_model`, `provider`, `model`, and
`api_url` values; unknown fields, embedded URL credentials, and symlink config files are rejected.
Resolution order is command line, environment, saved config, then built-in default.
Existing pre-rename config, conversation, run, and OAuth paths are discovered when their Rivumi
replacement does not yet exist. The former command names are intentionally not installed.

Read tools run automatically. A patch or repository-code command is shown in the terminal and
requires `once`, `session`, `deny`, or `cancel`. An interrupted run keeps its disposable workspace:

```bash
uv run rivumi resume last
uv run rivumi resume <run-id>
```

Sessions default to `${XDG_STATE_HOME:-~/.local/state}/rivumi/runs`; override with
`RIVUMI_RUN_ROOT` or `--run-root`.

For an API key or an existing OpenAI Chat-compatible proxy:

```bash
export OPENAI_API_KEY='...'
rivumi config --provider openai-compatible --model your-model \
  --api-url https://gateway.example/v1
export OPENAI_API_KEY='...'
rivumi -C /path/to/repo 'Fix the failing test.' --check 'pytest -q'
```

Remote API URLs require HTTPS. Plain HTTP is accepted only for exact loopback hosts; credentials,
query strings, and fragments in the endpoint URL are rejected.

## ChatGPT/Codex subscription (experimental)

Pi and OpenCode implement ChatGPT/Codex as a dedicated OAuth + Codex Responses transport, not as a
generic OpenAI-compatible URL. This project follows that boundary and creates its own credential;
it never reads `~/.codex`, Claude Code, Pi, OpenCode, or OMP credential files.

```bash
uv run rivumi auth login-codex
uv run rivumi auth status-codex
uv run rivumi --provider openai-codex --model <supported-codex-model> \
  --experimental-subscription --repo /path/to/repo --task '...' --check 'pytest -q'
```

If the browser callback cannot reach localhost, use `rivumi auth login-codex --manual`; the pasted
callback is hidden and only the Rivumi-owned credential store is written. `rivumi auth logout-codex`
removes that store without touching the official Codex CLI.

This path is deliberately opt-in because upstream authorization and protocol behavior can change.
Its current public OAuth client identity is borrowed from a pinned ecosystem implementation, so it
remains experimental until this project has its own registration and current authorization proof.

Anthropic's current Agent SDK policy says third-party products may not offer `claude.ai` login or
subscription rate limits without prior approval. Rivumi therefore keeps its own loop on the native
Anthropic API-key adapter. For local/private experiments only, the installed official Claude Code
CLI can edit a disposable clone with only `Read`, `Glob`, `Grep`, and `Edit`; Rivumi runs the final
check separately:

```bash
rivumi backend claude-code \
  --repo /path/to/trusted/repo \
  --task 'Fix the failing test.' \
  --allowed-path 'src/**' \
  --check 'pytest -q' \
  --experimental-subscription \
  --allow-external-modify \
  --unsafe-local-exec
```

That command uses the official CLI's own login (and therefore permits that official child to read
its own auth state), but Rivumi never reads or copies the login. Bash, Write, WebFetch, WebSearch,
MCP, subagent tools, and session persistence are not enabled. This is not evidence for Rivumi's own
agent loop, and a hosted Claude subscription proxy is intentionally not implemented.

The separately installed official Codex CLI can be used through the same outer harness. It owns
its ChatGPT login and agent loop, runs ephemeral with user config/rules ignored, and receives only
the disposable clone through Codex's `workspace-write` sandbox:

```bash
rivumi backend codex-cli \
  --repo /path/to/trusted/repo \
  --task 'Fix the failing test.' \
  --allowed-path 'src/**' \
  --check 'pytest -q' \
  --experimental-subscription \
  --allow-external-modify \
  --unsafe-local-exec
```

The two explicit acknowledgements are separate: one permits the external CLI to modify only the
clone; the other permits Rivumi to execute repository verification code on the host. New untracked
files are rejected in this initial external-coding milestone; use Rivumi's own `apply_patch` path for
reviewable create/delete work. The source repository must start clean. Rivumi removes Git metadata
from the child working tree, rejects index/config mutation, hashes all source entries outside
`.git` (including ignored files), and rechecks both source and patch after final verification.

More installed coding CLIs can sit in the same harness — `opencode`, `pi`, and `omp`. Each is a
sibling external runtime, never a `ModelProvider`: it edits only the pinned disposable clone, owns
its own login and provider credentials, and Rivumi independently audits the path-bounded patch and
runs the exact final verification. Rivumi never reads or copies `~/.opencode`, `~/.omp`, Pi's auth
store, or any other CLI credential file.

```bash
rivumi backend opencode \
  --repo /path/to/trusted/repo \
  --task 'Fix the failing test.' \
  --allowed-path 'src/**' \
  --check 'pytest -q' \
  --model 'ollama/gemma4' \
  --allow-external-modify \
  --unsafe-local-exec
```

All five runtimes share the same closed-loop boundary:

- `claude-code` — official Claude Code CLI; limited to `Read`, `Glob`, `Grep`, `Edit` inside the
  disposable clone.
- `codex-cli` — official Codex CLI, ephemeral with its `workspace-write` sandbox.
- `opencode` — OpenCode CLI via `opencode run --format json`; `--model` takes a provider/model id
  from `opencode models` (for example `ollama/gemma4`).
- `pi` — Pi coding agent with its own auth store.
- `omp` — OMP coding agent with its own auth store.

`opencode`, `pi`, and `omp` are local-only and experimental. Streaming output is normalized into
Rivumi's event journal (proofs live in `tests/test_external_runner_integration.py` with recorded
real captures under `tests/fixtures/m13/`). The current OpenCode headless build still expects an
interactive approve step before editing, so autonomous edits require OpenCode to run with its own
autonomous flag; the audit pipeline itself is exercised end-to-end by those recorded streams.

## Provider credentials

`rivumi auth` manages the API keys/secrets that Rivumi's own agent loop stores locally, separate
from any external CLI's login. Credentials are written only to this application's store and are
never forwarded to repository checks.

```bash
rivumi auth set-key openai-compatible   # prompts for the key, then verifies it live
rivumi auth set-key anthropic
rivumi auth set-key gemini
rivumi auth set-key workers-ai          # prompts for account_id and api_token
rivumi auth list                        # local state only, no network
rivumi auth list --verify               # calls each configured provider's API once
rivumi auth clear-key anthropic         # remove a stored credential
```

Supported providers: `anthropic`, `gemini`, `openai-compatible`, `workers-ai` (Ollama needs no
key). `set-key` saves the key and immediately verifies it against the provider; if verification is
unavailable (offline, provider outage) the key is still saved so you are never locked out, and
`auth list --verify` re-runs the same check later. Verification covers OpenAI-compatible (`/models`)
and Anthropic (`/v1/models`) model lists, Gemini model names (stripping the `models/` prefix), and
Workers AI (`/ai/models/search`), which also confirms the account id matches the token.

## Local model gateway

`rivumi gateway` is the OMP-inspired model gateway boundary. It translates OpenAI Chat wire messages
into canonical contracts and dispatches through one configured provider; it is not arbitrary URL
passthrough.

```bash
uv run rivumi gateway --provider ollama --model qwen3:4b --port 8788
curl http://127.0.0.1:8788/v1/models
```

The MVP exposes `/healthz`, `/v1/models`, and non-streaming `/v1/chat/completions`, binds loopback
only, caps request bodies, and optionally requires `RIVUMI_GATEWAY_TOKEN`. SSE and remote binding are
deferred.

## Headless run

The run directory must be outside the target source repository.

```bash
export OPENAI_API_KEY='...'
export CODING_AGENT_MODEL='your-tool-capable-model'

rivumi exec 'Fix the bounded bug and keep the existing behavior.' \
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
build scripts, and other checks still execute repository code on the host unless `--sandbox-checks`
is enabled. On macOS, sandboxed checks use the platform sandbox wrapper; on Linux, set
`sandbox_backend` in the CLI config to `auto`, `bubblewrap`, or `landlock` to select bubblewrap or
the Landlock/seccomp backend. Treat this as verification-command containment, not a full hostile
repository guarantee.

| Provider | CLI value | Credential environment |
|---|---|---|
| OpenAI or compatible endpoint | `openai-compatible` | `OPENAI_API_KEY`, optional `OPENAI_BASE_URL` |
| Ollama | `ollama` | local: none; remote HTTPS: `OLLAMA_API_KEY`; optional `RIVUMI_API_URL` |
| ChatGPT/Codex subscription | `openai-codex` | app-owned OAuth via `rivumi auth login-codex` |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY`, optional `ANTHROPIC_BASE_URL` |
| Gemini | `gemini` | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |
| Cloudflare Workers AI | `workers-ai` | `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` |

For `rivumi exec` and its `rivumi run` compatibility alias, `--api-url` is the preferred spelling and
`--base-url` remains a compatibility alias. Remote endpoints require HTTPS and an explicit
provider credential; a local Ollama process never receives `OLLAMA_API_KEY` even when it exists in
the parent environment.

Model capabilities vary across every provider. Headless `exec`/`run` keeps tool calling fail-closed
until `--tool-calling` explicitly asserts support. Native Anthropic, Gemini, and Workers AI
credentials are restricted to their official HTTPS hosts; a custom native endpoint additionally
requires the explicit `--allow-custom-provider-endpoint` acknowledgement.

## Session and telemetry tooling

```bash
rivumi sessions             # list recent agent runs and saved conversations with token usage
rivumi sessions -n 50       # show up to 50 entries
rivumi export-otel <run-id> # write a run as OpenTelemetry GenAI OTLP-JSON to stdout
rivumi export-otel <run-id> -o run.otel.json
```

`rivumi sessions` reads run `session.json`/`result.json` and conversation state directly from disk
and prints an id/status/model/tokens/time table (no network). `rivumi export-otel` renders a run's
events into OTel GenAI OTLP-JSON aligned with the `gen_ai.usage.*` semantics so it can be sent to a
span ingest pipeline. See [docs/session-format.md](docs/session-format.md) for the event and session
schema and the usage metrics.

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
Model text alone cannot mark a run successful. A run can also stop because the accumulated usage
exceeded an optional `max_total_tokens` budget (terminal reason `token_budget_exceeded`) before any
verification gate was reached.

## Boundaries

The local runtime does not provide an OS/container sandbox. The disposable clone and Python policy
layer protect the source worktree and narrow tool behavior, but they are not a substitute for
process isolation. Codex CLI adds its own `workspace-write` sandbox; the local Claude Code path has
an exact file-tool allowlist and post-run patch enforcement, but the official child still receives
the user's `HOME` for its own authentication and is not filesystem-isolated by Rivumi. Do not use
these local backends on hostile repositories. The separate `cloudflare/` service now packages the
project-owned Python runtime behind a thin Worker and Cloudflare Sandbox with a run-scoped model
capability. Runs now start asynchronously and expose durable status, SSE/NDJSON events, approvals,
cancel, and artifact routes; consumer subscription logins are not relayed there. See
[cloudflare/README.md](cloudflare/README.md) for its exact API and deployment boundary.

See [docs/progress.md](docs/progress.md) for current acceptance criteria and
[docs/stages](docs/stages/README.md) for milestone evidence.
