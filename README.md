<div align="center">

# looplane

**A Python-first coding agent that produces verified patches in disposable workspaces.**

[![CI](https://github.com/lanefoundry/looplane/actions/workflows/python-ci.yml/badge.svg)](https://github.com/lanefoundry/looplane/actions/workflows/python-ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/looplane.svg)](https://pypi.org/project/looplane/)
![Status](https://img.shields.io/badge/status-early_preview-orange.svg)

[Tools](#tool-surface) · [Install](#install) · [Quick start](#quick-start) · [Usage](#daily-cli) · [Cloudflare](#cloudflare-control-plane) · [Docs](#documentation)

[English](README.md) · [繁體中文](README.zh-TW.md)

</div>

looplane is a Python-first coding agent for local repositories. It gives the model a bounded tool surface, runs work in disposable committed-HEAD clones, records an auditable event bundle, and treats final verification as the source of truth.

looplane is usable as an interactive daily CLI while keeping a bounded, auditable headless mode for CI and future Cloudflare execution. It has two parallel runtime paths: its independently implemented native harness, which owns the loop, approvals, sessions, tools, verification, and model API adapters; and explicitly selected external coding CLI runtimes (Claude Code, Codex CLI, OpenCode, Pi, and OMP), which own their own agent loops while sharing looplane's conversation UI, workspace safety, patch audit, and verification boundary. One path is never disguised as the other.

> [!IMPORTANT]
> looplane is an early preview (`0.1.0`). Tool contracts and deployment behavior may change. The target OSS V1 Stable Release is an operator-hosted open-source product. looplane is not a CAPTCHA solver or a universal anti-bot bypass.

The project provides a provider-neutral `ModelProvider` contract with canonical messages, tool calls, capabilities, usage, and classified errors. It supports OpenAI-compatible APIs, Ollama, Anthropic, Gemini, Cloudflare Workers AI, and the explicit experimental app-owned ChatGPT/Codex OAuth transport. External runtimes are opt-in local delegation — they own their own login and agent loop, but looplane still provides the conversation UI, disposable clone, patch audit, and final checks.

## What Works Today

- Full-screen `looplane` TUI with runtime/model selection, inline slash commands, approvals, streaming tool activity, transcript scrollback, `/new`, `/resume`, `/history`, `/usage`, `/context`, and cooperative stop. The native `looplane-agent` runtime carries conversation history and the same disposable workspace across follow-up turns within one session, falling back to a fresh run if the model/provider changes or the prior workspace is gone.
- Headless `looplane exec` / `looplane -p` runs with path allowlists, exact check commands, deterministic run bundles, and `looplane resume` for validated non-terminal runs.
- Provider-neutral native loop for OpenAI-compatible APIs, Ollama, Anthropic, Gemini, Cloudflare Workers AI, and the explicit experimental app-owned ChatGPT/Codex OAuth transport.
- First-run provider setup, local Ollama discovery, provider credential storage, live credential verification, and dynamic model listing where supported.
- External runtime adapters for official Claude Code, official Codex CLI, OpenCode, Pi, and OMP, all operating inside disposable clones.
- Repository-local `.looplane/skills/*.md`, opt-in blocking hooks, plugin manifests, IDE/LSP snapshots, and a VS Code bridge scaffold under `editors/vscode`.
- Native MCP client support with looplane-owned OAuth grants and approval classification for MCP tools/resources.
- Programmatic subagent dispatch and native bounded `dispatch_subagents` fan-out for general and coder child workspaces.
- Cross-session memory with `save_memory` and `recall_memory` tools, automatic memory extraction from conversations, and memory-aware prompt assembly via `agent/context.py`.
- Conversation persistence, WebSocket attach with multi-session tab support (per-tab isolated controllers, shared read-only workspace context, session resume on reconnect), deterministic replay/fork helpers, SDK facade, session usage summaries, cost estimates, and OpenTelemetry GenAI export.
- Cloudflare Worker/Sandbox control plane under `cloudflare/` for asynchronous, text-source-map remote runs with durable status, event, approval, cancel, and artifact routes.

## Tool Surface

The native `looplane-agent` runtime exposes a bounded tool surface to the model. Core workspace tools (`read_file`, `create_file`, `replace_text`, `apply_patch`, `search_text`, `shell`, `git_diff`) are always available. Additional tool families are registered conditionally:

| Category | Tools | Requires |
| --- | --- | --- |
| **Batch orchestration** | `tool_program`, `tool_transaction` | — (built-in) |
| **Subagents** | `dispatch_subagents` | — (built-in) |
| **Web & HTTP** | `web_fetch`, `web_search`, `http_request` | `trafilatura`, `duckduckgo-search` (optional) |
| **LSP semantics** | `lsp_symbols`, `lsp_references`, `lsp_definition`, `lsp_diagnostics` | LSP server configured in `looplane.toml` |
| **Background processes** | `start_process`, `read_process`, `stop_process`, `list_processes`, `wait_for_output` | `enable_background` flag |
| **Media** | `view_image`, `take_screenshot` | `Pillow`; `playwright` for screenshots |
| **Interactive terminal** | `start_session`, `send_input`, `read_session`, `end_session` | `enable_pty` flag |

`tool_transaction` bundles edits and checks into an atomic unit — if any step fails, touched files are rolled back automatically. `tool_program` batches up to 8 read-only steps in a single model call with `repeat` and `if_contains` control flow. These two are unique to looplane; mainstream coding agents do not offer atomic rollback or batched read orchestration.

### Optional dependencies

```bash
pip install looplane[web]      # web_fetch + web_search (trafilatura, duckduckgo-search)
pip install looplane[media]    # view_image + take_screenshot (Pillow, playwright)
pip install looplane[all]      # all optional dependencies
```

### Project configuration

Place a `looplane.toml` at the repository root to configure LSP servers, verification tools, web domain policies, and auto-approve rules. See `.research/capability-roadmap.md` for the full schema.

```toml
[project]
instructions = "docs/agent-instructions.md"

[lsp.python]
command = ["pyright-langserver", "--stdio"]

[tools.verification]
lint = "ruff check ."
test = "pytest -x -q"
```

## Install

### One-liner (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/lanefoundry/looplane/main/scripts/install.sh | sh
```

This installs uv and Python automatically if needed, then installs looplane as an isolated tool.

### With uv

```bash
uv tool install looplane          # install
uvx looplane                      # or run without installing
```

### With pipx

```bash
pipx install looplane
```

### With Homebrew (macOS)

```bash
brew install lanefoundry/tap/looplane
```

### With pip

```bash
pip install looplane
```

### Self-update

```bash
looplane update
```

## Quick start (contributors)

For development and contributing, clone the repo and use `uv`:

```bash
git clone https://github.com/lanefoundry/looplane.git
cd looplane
uv sync --extra dev
uv run pytest
uv run ruff check .
```

Install or refresh the editable daily command:

```bash
scripts/install-dev-cli
looplane --help
```

The editable command reads source changes immediately, but its isolated tool environment does not automatically update when dependencies change. Run `scripts/install-dev-cli` again after changing `pyproject.toml` or `uv.lock`.

There is no `requirements.txt`; dependencies are declared in `pyproject.toml` and locked in `uv.lock`.

## Daily CLI

```bash
# Open the full-screen conversation in the current Git repository.
looplane

# Ask or act from the command line.
looplane "Explain the failing test."
looplane "Fix the failing test without changing its intent." --check "pytest -q"
looplane -C /path/to/repo "Explain and fix the failure."

# Non-interactive JSON output.
looplane -p "Summarize this repository."

# Headless coding run with explicit verification.
looplane exec "Fix the bounded bug and keep existing behavior." \
  -C /absolute/path/to/repo \
  --allowed-path "src/**" \
  --allowed-path "tests/**" \
  --check "pytest -q" \
  --tool-calling \
  --unsafe-local-exec

# Fallback for limited terminals and SSH troubleshooting.
looplane --plain

# Keep the interactive UI in the normal terminal buffer so native scrollback
# remains available. The default remains the alternate-screen UI.
looplane --no-alt-screen

# Edit this repository's real working tree directly instead of a disposable
# clone (native `looplane-agent` runtime only; see Safety Boundary below).
looplane --edit-real-repo "Fix the failing test."
```

`looplane [PROMPT]` is interactive. `looplane -p [PROMPT]` and `looplane exec [PROMPT]` are non-interactive. `looplane run`, `--task`, and `--repo` remain compatibility aliases. `-p` means `--print`; use `--provider` or `looplane config` to choose the provider.

Useful commands:

```bash
looplane config --interactive
looplane sessions
looplane resume last
looplane export-otel <run-id> -o run.otel.json
looplane gateway --provider ollama --model qwen3:4b --port 8788
looplane conversation-server --help
looplane policy --help
```

## Conversation Server

`looplane conversation-server` exposes a native conversation runtime through a WebSocket attach protocol. Multiple tabs or clients can connect simultaneously — each gets an independent session with isolated state.

```bash
# Start the server on the current repository.
looplane conversation-server

# Specify runtime, model, and port.
looplane conversation-server --runtime codex-cli --model auto --port 8788
```

Clients connect via WebSocket at `/v1/conversation/attach`. Each connection can supply a `conversation_id` query parameter to identify its session:

```
ws://localhost:8788/v1/conversation/attach?conversation_id=my-tab-1
```

- **No `conversation_id`** — the server generates one automatically.
- **New `conversation_id`** — a fresh session is created.
- **Existing `conversation_id` (disconnected)** — the session is resumed with full history intact.
- **Existing `conversation_id` (still connected)** — the second connection is rejected (code 1008).

On connect the server sends a `session_context` message with the shared repository snapshot:

```json
{
  "type": "session_context",
  "conversation_id": "my-tab-1",
  "resumed": false,
  "base_sha": "a1b2c3...",
  "source_was_dirty": true,
  "context_version": "sha256...",
  "source_snapshot_warning": "The source repository had uncommitted changes..."
}
```

Idle sessions are kept alive for 5 minutes after disconnect, then evicted. Each session operates in its own disposable workspace clone; the shared context (HEAD commit, dirty status) is read-only and computed once at server startup.

### SDK usage

```python
from looplane.sdk import ConversationWebSocketApp, SharedWorkspaceContext

# Multi-session mode: each conversation_id gets its own session.
shared_ctx = await SharedWorkspaceContext.create(repo_path)
app = ConversationWebSocketApp(
    session_factory=my_session_factory,
    shared_context=shared_ctx,
)

# Single-session mode (backward compatible).
app = ConversationWebSocketApp(my_session)
```

## Runtime And Provider Setup

Configure non-secret defaults:

```bash
looplane config --provider ollama --model qwen3:4b
looplane config --provider openai-compatible --model your-model \
  --api-url https://gateway.example/v1
```

Store looplane-owned API credentials:

```bash
looplane auth set-key openai-compatible
looplane auth set-key anthropic
looplane auth set-key gemini
looplane auth set-key workers-ai
looplane auth list
looplane auth list --verify
```

Supported native providers:

| Provider | CLI value | Credential source |
| --- | --- | --- |
| OpenAI or compatible endpoint | `openai-compatible` | `OPENAI_API_KEY`, stored key, optional `OPENAI_BASE_URL` |
| Ollama | `ollama` | local loopback needs no key; remote HTTPS can use `OLLAMA_API_KEY` |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` or stored key |
| Gemini | `gemini` | `GEMINI_API_KEY`, `GOOGLE_API_KEY`, or stored key |
| Cloudflare Workers AI | `workers-ai` | account id and API token |
| ChatGPT/Codex subscription | `openai-codex` | app-owned OAuth via `looplane auth login-codex` |

The ChatGPT/Codex subscription path is explicit and experimental:

```bash
looplane auth login-codex
looplane auth status-codex
looplane --provider openai-codex --model <supported-codex-model> \
  --experimental-subscription
```

looplane creates its own credential for that path. It does not read `~/.codex`, Claude Code, Pi, OpenCode, OMP, or other CLI credential files.

## External Coding Runtimes

External runtimes are opt-in local delegation. The child CLI edits only the disposable clone; looplane independently audits the path-bounded patch and runs the declared final verification.

```bash
looplane backend codex-cli \
  --repo /path/to/trusted/repo \
  --task "Fix the failing test." \
  --allowed-path "src/**" \
  --check "pytest -q" \
  --experimental-subscription \
  --allow-external-modify \
  --unsafe-local-exec

looplane backend claude-code \
  --repo /path/to/trusted/repo \
  --task "Fix the failing test." \
  --allowed-path "src/**" \
  --check "pytest -q" \
  --experimental-subscription \
  --allow-external-modify \
  --unsafe-local-exec

looplane backend opencode \
  --repo /path/to/trusted/repo \
  --task "Fix the failing test." \
  --allowed-path "src/**" \
  --check "pytest -q" \
  --model "ollama/gemma4" \
  --allow-external-modify \
  --unsafe-local-exec
```

External runtime support is designed for trusted local repositories. It is not a hostile-code sandbox. Codex CLI adds its own `workspace-write` sandbox; the Claude Code path limits enabled file tools, but the official child still uses its own local authentication environment.

## Safety Boundary

looplane's default local boundary is a disposable Git workspace plus Python policy checks:

- source worktrees are not edited directly;
- changed files must match the allowed path policy;
- modify and execute actions require approval in interactive mode;
- verification commands are exact argv, not shell strings;
- run bundles contain request, events, checkpoint, session, verification, patch, test log, result, and workspace artifacts;
- provider credentials stay in the coordinator process and are not forwarded to repository checks.

`--unsafe-local-exec` allows trusted repository checks to run on the host. Use `--sandbox-checks` where available for additional verification-command containment. On macOS this uses the platform sandbox wrapper; on Linux, `sandbox_backend` can select `auto`, `bubblewrap`, or `landlock`.

`--edit-real-repo` (native `looplane-agent` runtime only) is an explicit opt-in that turns off the disposable clone and lets the agent edit this repository's real working tree directly, so changes are visible in `git status`/`git diff` immediately instead of requiring a manual `git apply` of the run's `changes.patch` artifact afterward. Approvals still show a diff before every file change; a pre-existing dirty repository is left alone (its files are excluded from the reported patch and never checked against the allowed-path policy) and a warning is injected into the model's context. Combining `--edit-real-repo` with `--dangerous` requires one extra one-time interactive acknowledgment (or `LOOPLANE_ACCEPT_DANGEROUS_MODE=1`), separate from `--dangerous`'s own acknowledgment. External runtimes (Claude Code, Codex CLI, OpenCode, Pi, OMP) and the Cloudflare remote sandbox are unaffected; they keep the disposable-clone/patch-audit boundary described above regardless of this flag.

`--dangerous` auto-approves read and modify tool calls without prompting — the equivalent of Claude Code's `--dangerously-skip-permissions`. The first invocation requires an interactive confirmation dialog; acceptance is recorded to `~/.local/state/looplane/dangerous-mode-accepted` and not asked again. Non-interactive environments can set `LOOPLANE_ACCEPT_DANGEROUS_MODE=1` to skip the dialog. Guardrails that remain active under `--dangerous`:

- **Deny rules are authoritative.** `--deny-tool` rules (e.g. `--deny-tool 'shell(rm *)'`) and forbidden-operation patterns are evaluated before the auto-approve branch and cannot be overridden.
- **EXECUTE-tier tools still require approval.** Only READ and MODIFY tiers are auto-approved; EXECUTE-tier calls are always prompted.
- **Root/sudo is refused.** `--dangerous` exits immediately when run as root unless inside a sandbox (`LOOPLANE_SANDBOX=1`).

```bash
# Auto-approve read/modify actions
looplane chat --dangerous "refactor the auth module"

# Combine with direct repo editing (extra one-time confirmation required)
looplane chat --dangerous --edit-real-repo "fix the failing tests"

# Non-interactive / CI
LOOPLANE_ACCEPT_DANGEROUS_MODE=1 looplane chat --dangerous -p "update deps"
```

## Cloudflare Control Plane

`cloudflare/` packages the Python runtime behind a Worker and Cloudflare Sandbox. The Worker owns HTTP auth and provider credentials, stages a bounded text-only source tree, starts an asynchronous run, exposes durable status and events, handles approvals/cancel, and serves bounded artifacts.

It deliberately does not accept Git URLs, archives, shell strings, provider credentials, consumer subscription tokens, custom caller-selected upstreams, or arbitrary model IDs. See [cloudflare/README.md](cloudflare/README.md) and [docs/stages/m6-cloudflare-sandbox-service.md](docs/stages/m6-cloudflare-sandbox-service.md) for the exact API and evidence boundary.

### Hosted provider setup

The hosted control plane uses operator-managed profiles. Users select a `modelProfile` when starting a run; they cannot specify endpoints, API keys, or arbitrary models. Operators configure all providers at once with a manifest and a secrets file, without answering prompts or running multiple `wrangler secret put` commands.

Install Cloudflare sub-project dependencies first; building the Sandbox image also requires a working Docker runtime, and applying secrets requires Wrangler authentication:

```bash
npm --prefix cloudflare ci
(cd cloudflare && npx wrangler whoami)
cp cloudflare/providers.example.json cloudflare/providers.json
```

`cloudflare/providers.json` is a trackable, non-secret configuration. Known providers only need `provider` and `model`:

```json
{
  "default": "openrouter-primary",
  "profiles": {
    "openrouter-primary": {
      "provider": "openrouter",
      "model": "your-openrouter-model-id"
    },
    "groq-fast": {
      "provider": "groq",
      "model": "your-groq-model-id"
    }
  }
}
```

Collect all keys in `cloudflare/.env.cloudflare` (already in `.gitignore`):

```dotenv
# Required for first-time Worker setup; existing deployments can omit these.
CONTROL_PLANE_TOKEN=replace-with-at-least-16-bytes
RUN_TOKEN_SECRET=replace-with-at-least-32-bytes

OPENROUTER_API_KEY=replace-me
GROQ_API_KEY=replace-me
```

Restrict file permissions, dry-run first, then apply with the same manifest:

```bash
chmod 600 cloudflare/.env.cloudflare
uv run looplane cloudflare providers apply cloudflare/providers.json \
  --secrets-env cloudflare/.env.cloudflare \
  --dry-run
uv run looplane cloudflare providers apply cloudflare/providers.json \
  --secrets-env cloudflare/.env.cloudflare
```

`apply` validates the manifest and all required keys first, then pipes a single `wrangler secret bulk` via stdin, builds the runtime, and deploys the profile catalog. Secrets never appear in the manifest, process arguments, or temporary files. When multiple keys are missing, all are listed at once without making partial remote changes. `--dry-run` still reads and validates all provider keys in the manifest but does not send them to Cloudflare.

Built-in shorthand providers: `openrouter`, `deepseek`, `groq`, `moonshotai`, `zai`, `xai`, `nvidia-nim`, `opencode-zen`, `ollama-cloud` — endpoint and Worker binding are derived automatically. Custom OpenAI-compatible endpoints must supply full routing fields and pass `--allow-custom-endpoint`. Hosted phase 1 supports OpenAI-compatible Chat Completions only; Anthropic Messages, Gemini native API, and Responses API still require individual protocol adapters.

| `provider` | dotenv key |
| --- | --- |
| `openrouter` | `OPENROUTER_API_KEY` |
| `deepseek` | `DEEPSEEK_API_KEY` |
| `groq` | `GROQ_API_KEY` |
| `moonshotai` | `MOONSHOT_API_KEY` |
| `zai` | `ZAI_API_KEY` |
| `xai` | `XAI_API_KEY` |
| `nvidia-nim` | `NVIDIA_API_KEY` |
| `opencode-zen` | `OPENCODE_ZEN_API_KEY` |
| `ollama-cloud` | `OLLAMA_CLOUD_API_KEY` |

After deploying, call the authenticated `GET /v1/model-profiles` endpoint and confirm the selected profile shows `ready: true`. This only means the secret binding is non-empty; you still need to send an actual `/v1/runs` smoke run to confirm the API key, model ID, and provider endpoint are functional.

See [cloudflare/README.md](cloudflare/README.md) for the full API, named Wrangler environments, and security boundary.

## Development Checks

General checks:

```bash
uv run pytest
uv run ruff check .
git diff --check
```

Offline loop proof:

```bash
uv run python scripts/demo_fixture.py
```

Repeatable real-provider eval:

```bash
eval_root=$(mktemp -d /tmp/looplane-live-eval.XXXXXX)
uv run python scripts/eval_live_provider.py \
  --provider ollama \
  --model qwen3:4b \
  --output-dir "$eval_root/ollama-qwen3-4b"
```

TUI layout changes need focused geometry tests and rendered review images:

```bash
uv run pytest tests/test_tui.py -q
uv run python scripts/render_tui_screenshot.py --width 120 --height 36 --name wide
uv run python scripts/render_tui_screenshot.py --width 60 --height 22 --name narrow
uv run python scripts/render_tui_screenshot.py --state thinking --name loading
```

Review the generated `.artifacts/tui/*.png` images before treating a TUI change as complete.

## Documentation Map

- [docs/progress.md](docs/progress.md): milestone status, acceptance criteria, and project boundaries.
- [docs/stages](docs/stages/README.md): reproducible milestone records and verification evidence.
- [docs/sdk.md](docs/sdk.md): SDK facade, WebSocket attach, replay/fork API, role lanes, and policy boundaries.
- [docs/session-format.md](docs/session-format.md): run events, session schema, and usage metrics.
- [docs/startup-performance-playbook.md](docs/startup-performance-playbook.md): startup budget and lazy-import guidance.
- [docs/agent-diff-report.md](docs/agent-diff-report.md): current capability gap/backlog against reference coding-agent architectures.

Backlog items in `docs/agent-diff-report.md` are not implementation proof. Before claiming a capability is done, verify the code path, tests, and current stage/progress record.

## Documentation

- [Cloudflare deployment](cloudflare/README.md)

## Contributing and support

Use [GitHub Issues](https://github.com/lanefoundry/looplane/issues) for bugs and feature proposals.

## License

looplane is licensed under the [Apache License 2.0](LICENSE).
