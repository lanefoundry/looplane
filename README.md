<div align="center">

# looplane

**A Python-first coding agent that produces verified patches in disposable workspaces.**

[![CI](https://github.com/lanefoundry/looplane/actions/workflows/python-ci.yml/badge.svg)](https://github.com/lanefoundry/looplane/actions/workflows/python-ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/looplane.svg)](https://pypi.org/project/looplane/)
![Status](https://img.shields.io/badge/status-early_preview-orange.svg)

[Install](#install) · [Quick start](#quick-start) · [Usage](#daily-cli) · [Cloudflare](#cloudflare-control-plane) · [Docs](#documentation)

[English](README.md) · [繁體中文](README.zh-TW.md)

</div>

looplane is a Python-first coding agent for local repositories. It gives the model a bounded tool surface, runs work in disposable committed-HEAD clones, records an auditable event bundle, and treats final verification as the source of truth.

looplane is usable as an interactive daily CLI while keeping a bounded, auditable headless mode for CI and future Cloudflare execution. It has two parallel runtime paths: its independently implemented native harness, which owns the loop, approvals, sessions, tools, verification, and model API adapters; and explicitly selected external coding CLI runtimes (Claude Code, Codex CLI, OpenCode, Pi, and OMP), which own their own agent loops while sharing looplane's conversation UI, workspace safety, patch audit, and verification boundary. One path is never disguised as the other.

> [!IMPORTANT]
> looplane is an early preview (`0.1.0`). Tool contracts and deployment behavior may change. The target OSS V1 Stable Release is an operator-hosted open-source product. looplane is not a CAPTCHA solver or a universal anti-bot bypass.

The project provides a provider-neutral `ModelProvider` contract with canonical messages, tool calls, capabilities, usage, and classified errors. It supports OpenAI-compatible APIs, Ollama, Anthropic, Gemini, Cloudflare Workers AI, and the explicit experimental app-owned ChatGPT/Codex OAuth transport. External runtimes are opt-in local delegation — they own their own login and agent loop, but looplane still provides the conversation UI, disposable clone, patch audit, and final checks.

## What Works Today / 目前能力

- Full-screen `looplane` TUI with runtime/model selection, inline slash commands, approvals, streaming tool activity, transcript scrollback, `/new`, `/resume`, `/history`, `/usage`, `/context`, and cooperative stop. The native `looplane-agent` runtime carries conversation history and the same disposable workspace across follow-up turns within one session, falling back to a fresh run if the model/provider changes or the prior workspace is gone.
- Headless `looplane exec` / `looplane -p` runs with path allowlists, exact check commands, deterministic run bundles, and `looplane resume` for validated non-terminal runs.
- Provider-neutral native loop for OpenAI-compatible APIs, Ollama, Anthropic, Gemini, Cloudflare Workers AI, and the explicit experimental app-owned ChatGPT/Codex OAuth transport.
- First-run provider setup, local Ollama discovery, provider credential storage, live credential verification, and dynamic model listing where supported.
- External runtime adapters for official Claude Code, official Codex CLI, OpenCode, Pi, and OMP, all operating inside disposable clones.
- Repository-local `.looplane/skills/*.md`, opt-in blocking hooks, plugin manifests, IDE/LSP snapshots, and a VS Code bridge scaffold under `editors/vscode`.
- Native MCP client support with looplane-owned OAuth grants and approval classification for MCP tools/resources.
- Programmatic subagent dispatch and native bounded `dispatch_subagents` fan-out for scout, analyst, and reviewer child workspaces.
- Conversation persistence, WebSocket attach with multi-session tab support (per-tab isolated controllers, shared read-only workspace context, session resume on reconnect), deterministic replay/fork helpers, SDK facade, session usage summaries, cost estimates, and OpenTelemetry GenAI export.
- Cloudflare Worker/Sandbox control plane under `cloudflare/` for asynchronous, text-source-map remote runs with durable status, event, approval, cancel, and artifact routes.

中文摘要：

- `looplane` 會開全螢幕 TUI，支援 runtime/model 選擇、slash commands、approval、tool stream、scrollback、resume/history、usage/context 與可控停止。native `looplane-agent` runtime 在同一個 session 裡的後續訊息會延續對話歷史與同一份 disposable workspace；若 model/provider 換了或前一份 workspace 不在了，會自動 fallback 成全新的一輪。
- `looplane exec` 和 `looplane -p` 可做 headless run，保留 path allowlist、精準 check command、run bundle 與可恢復的 non-terminal session。
- 原生 loop 支援 OpenAI-compatible、Ollama、Anthropic、Gemini、Cloudflare Workers AI，以及明確標成 experimental 的 app-owned ChatGPT/Codex OAuth。
- 外部 runtime 可接 Claude Code、Codex CLI、OpenCode、Pi、OMP；它們只改 disposable clone，looplane 仍負責 patch audit 和 final checks。
- 目前也有 repository-local skills/hooks/plugins、IDE/LSP snapshot、VS Code bridge、MCP client、subagents、conversation persistence（含多 tab 獨立 session、共享唯讀 workspace context、斷線 resume）、SDK、usage/cost、OTel export，以及 `cloudflare/` remote control plane。

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

安裝方式擇一即可。推薦 `uv tool install`（自動隔離、不汙染系統 Python）。macOS 使用者也可用 `brew install`。`looplane update` 會自動偵測安裝管道並升級。

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

開發環境使用 `uv`。`.venv/` 由 `uv` 管理；如果改了 `pyproject.toml` 或 `uv.lock`，重新跑 `scripts/install-dev-cli`，讓全域 `looplane` 指令同步到目前 lock 檔。

## Daily CLI / 日常使用

```bash
# Open the full-screen conversation in the current Git repository.
# 在目前 Git repository 開啟全螢幕對話。
looplane

# Ask or act from the command line.
# 從命令列提問或執行任務。
looplane "Explain the failing test."
looplane "Fix the failing test without changing its intent." --check "pytest -q"
looplane -C /path/to/repo "Explain and fix the failure."

# Non-interactive JSON output.
# 非互動模式，輸出 JSON。
looplane -p "Summarize this repository."

# Headless coding run with explicit verification.
# Headless coding run，明確指定驗證命令。
looplane exec "Fix the bounded bug and keep existing behavior." \
  -C /absolute/path/to/repo \
  --allowed-path "src/**" \
  --allowed-path "tests/**" \
  --check "pytest -q" \
  --tool-calling \
  --unsafe-local-exec

# Fallback for limited terminals and SSH troubleshooting.
# 給受限 terminal 或 SSH troubleshooting 使用的 plain mode。
looplane --plain

# Keep the interactive UI in the normal terminal buffer so native scrollback
# remains available. The default remains the alternate-screen UI.
# 把互動 UI 留在 terminal 的 normal buffer，保留原生 scrollback；預設仍是
# alternate-screen UI。
looplane --no-alt-screen

# Edit this repository's real working tree directly instead of a disposable
# clone (native `looplane-agent` runtime only; see Safety Boundary below).
# 直接編輯這個 repo 的真實 working tree，而不是 disposable clone（只影響
# native `looplane-agent` runtime；見下方 Safety Boundary）。
looplane --edit-real-repo "Fix the failing test."
```

`looplane [PROMPT]` is interactive. `looplane -p [PROMPT]` and `looplane exec [PROMPT]` are non-interactive. `looplane run`, `--task`, and `--repo` remain compatibility aliases. `-p` means `--print`; use `--provider` or `looplane config` to choose the provider.

`looplane [PROMPT]` 走互動式流程；`looplane -p [PROMPT]` 和 `looplane exec [PROMPT]` 是非互動模式。`looplane run`、`--task`、`--repo` 仍保留作相容 alias。`-p` 現在是 `--print`，provider 請用 `--provider` 或 `looplane config` 設定。

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

## Runtime And Provider Setup / Runtime 與 Provider 設定

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

Provider/model/API URL 等非 secret 設定存在 looplane config；API key 和 OAuth grant 則由 looplane 自己的 auth store 或環境變數提供。ChatGPT/Codex subscription 路徑是 explicit experimental 功能，和官方 Codex CLI runtime 不是同一條路。

```bash
looplane auth login-codex
looplane auth status-codex
looplane --provider openai-codex --model <supported-codex-model> \
  --experimental-subscription
```

looplane creates its own credential for that path. It does not read `~/.codex`, Claude Code, Pi, OpenCode, OMP, or other CLI credential files.

## External Coding Runtimes / 外部 Coding Runtime

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

外部 runtime 是明確 opt-in 的本機 delegation。Claude Code、Codex CLI、OpenCode、Pi、OMP 各自擁有登入與 agent loop；looplane 只交給它們 disposable clone，並在結束後重新檢查 path-bounded patch 與 verification。這是 trusted local repo 的工作流，不是 hostile-code sandbox。

## Safety Boundary / 安全邊界

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

本機預設安全邊界是 disposable Git workspace 加上 Python policy checks。互動模式下修改與執行需要 approval；verification command 是 exact argv，不是 shell string；provider credential 留在 coordinator process，不會轉交給 repository checks。`--unsafe-local-exec` 表示你同意在 host 上跑 trusted repo 的檢查。

`--edit-real-repo`（只影響 native `looplane-agent` runtime）是明確的 opt-in：關掉 disposable clone，讓 agent 直接改這個 repo 的真實 working tree，改完立刻反映在 `git status`/`git diff`，不用再手動 `git apply` run 結束後的 `changes.patch`。每次檔案變更前仍會顯示 diff 給你核准；repo 原本就有的未提交變更會被排除在回報的 patch 之外、也不會被 allowed-path policy 卡住，並會在丟給 model 的 context 裡加一段警告。`--edit-real-repo` 跟 `--dangerous` 疊加使用時，需要額外一次獨立的互動確認（或設定 `LOOPLANE_ACCEPT_DANGEROUS_MODE=1`）。外部 runtime（Claude Code、Codex CLI、OpenCode、Pi、OMP）與 Cloudflare 遠端 sandbox 不受這個 flag 影響，仍維持上述 disposable clone／patch audit 邊界。

`--dangerous` 自動核准 read 與 modify 等級的工具呼叫，不再逐一詢問——等同 Claude Code 的 `--dangerously-skip-permissions`。首次使用需要互動確認對話框，接受後記錄到 `~/.local/state/looplane/dangerous-mode-accepted`，之後不再詢問。非互動環境可設定 `LOOPLANE_ACCEPT_DANGEROUS_MODE=1` 跳過對話框。`--dangerous` 下仍然生效的防護：

- **Deny rules 是權威層。** `--deny-tool` 規則（如 `--deny-tool 'shell(rm *)'`）與 forbidden-operation pattern 在 auto-approve 分支之前被評估，無法被覆蓋。
- **EXECUTE 等級仍需核准。** 只有 READ 與 MODIFY 等級被自動核准；EXECUTE 等級一律詢問。
- **Root/sudo 直接拒絕。** 以 root 身份執行 `--dangerous` 會直接退出，除非在 sandbox 內（`LOOPLANE_SANDBOX=1`）。

```bash
# 自動核准 read/modify 動作
looplane chat --dangerous "重構 auth 模組"

# 搭配直接編輯 repo（需額外一次確認）
looplane chat --dangerous --edit-real-repo "修復失敗的測試"

# 非互動 / CI
LOOPLANE_ACCEPT_DANGEROUS_MODE=1 looplane chat --dangerous -p "更新依賴"
```

## Cloudflare Control Plane / Cloudflare 控制平面

`cloudflare/` packages the Python runtime behind a Worker and Cloudflare Sandbox. The Worker owns HTTP auth and provider credentials, stages a bounded text-only source tree, starts an asynchronous run, exposes durable status and events, handles approvals/cancel, and serves bounded artifacts.

It deliberately does not accept Git URLs, archives, shell strings, provider credentials, consumer subscription tokens, custom caller-selected upstreams, or arbitrary model IDs. See [cloudflare/README.md](cloudflare/README.md) and [docs/stages/m6-cloudflare-sandbox-service.md](docs/stages/m6-cloudflare-sandbox-service.md) for the exact API and evidence boundary.

`cloudflare/` 是遠端 Worker/Sandbox control plane。Worker 負責 HTTP auth 與 provider credential，Sandbox 收到的是 bounded text-source-map 和 run-scoped capability。它不接受 Git URL、archive、shell string、caller provider credential、subscription token、任意 upstream 或任意 model ID。

### Hosted provider 快速設定

Hosted control plane 使用 operator-managed profiles。使用者呼叫 run 時只能選 `modelProfile`，不能指定 endpoint、API key 或任意 model。管理者則可以用一份 manifest 和一份 secrets 檔，一次設定全部 providers，不必逐筆回答問題或執行多次 `wrangler secret put`。

先安裝 Cloudflare 子專案依賴；建置 Sandbox image 時也需要可用的 Docker runtime，正式套用前則要先完成 Wrangler authentication：

```bash
npm --prefix cloudflare ci
(cd cloudflare && npx wrangler whoami)
cp cloudflare/providers.example.json cloudflare/providers.json
```

`cloudflare/providers.json` 是可追蹤的非機密設定。已知 provider 只需填 `provider` 和 `model`：

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

把所有 keys 集中放進已由 `.gitignore` 排除的 `cloudflare/.env.cloudflare`：

```dotenv
# 新 Worker 第一次設定時一併提供；既有部署可以省略這兩項。
CONTROL_PLANE_TOKEN=replace-with-at-least-16-bytes
RUN_TOKEN_SECRET=replace-with-at-least-32-bytes

OPENROUTER_API_KEY=replace-me
GROQ_API_KEY=replace-me
```

限制檔案權限後，先 dry-run，再用相同 manifest 一次套用：

```bash
chmod 600 cloudflare/.env.cloudflare
uv run looplane cloudflare providers apply cloudflare/providers.json \
  --secrets-env cloudflare/.env.cloudflare \
  --dry-run
uv run looplane cloudflare providers apply cloudflare/providers.json \
  --secrets-env cloudflare/.env.cloudflare
```

`apply` 會先完整驗證 manifest 與所有必要 keys，再透過 stdin 執行一次 `wrangler secret bulk`，接著建置 runtime 並部署 profile catalog。Secret 不會寫進 manifest、process arguments 或暫存檔。缺少多個 keys 時會一次列出全部缺項，而且不會先做部分遠端修改。`--dry-run` 仍會讀取並檢查 manifest 中所有 provider keys，但不會把它們送到 Cloudflare。

內建快速格式支援 `openrouter`、`deepseek`、`groq`、`moonshotai`、`zai`、`xai`、`nvidia-nim`、`opencode-zen`、`ollama-cloud`；endpoint 與 Worker binding 會由 looplane 固定推導。自訂 OpenAI-compatible endpoint 必須提供完整 routing 欄位，並明確加上 `--allow-custom-endpoint`。Hosted phase 1 僅支援 OpenAI-compatible Chat Completions；Anthropic Messages、Gemini native API 和 Responses API 仍需要個別 protocol adapter。

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

部署後先呼叫 authenticated `GET /v1/model-profiles`，確認選用的 profile 顯示 `ready: true`。這只代表 secret binding 非空；仍需再送一個實際 `/v1/runs` smoke run，才能確認 API key、model ID 與 provider endpoint 確實可用。

完整 API、named Wrangler environment 與安全邊界見 [cloudflare/README.md](cloudflare/README.md)。

## Development Checks / 開發檢查

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

TUI 改動除了測試，也要產生寬版、窄版與 loading 狀態截圖，實際看過 `.artifacts/tui/*.png` 後才算完成。

## Documentation Map / 文件地圖

- [docs/progress.md](docs/progress.md): milestone status, acceptance criteria, and project boundaries.
- [docs/stages](docs/stages/README.md): reproducible milestone records and verification evidence.
- [docs/sdk.md](docs/sdk.md): SDK facade, WebSocket attach, replay/fork API, role lanes, and policy boundaries.
- [docs/session-format.md](docs/session-format.md): run events, session schema, and usage metrics.
- [docs/startup-performance-playbook.md](docs/startup-performance-playbook.md): startup budget and lazy-import guidance.
- [docs/agent-diff-report.md](docs/agent-diff-report.md): current capability gap/backlog against reference coding-agent architectures.

Backlog items in `docs/agent-diff-report.md` are not implementation proof. Before claiming a capability is done, verify the code path, tests, and current stage/progress record.

`docs/agent-diff-report.md` 是 backlog，不是完成證據。要宣稱某個能力已完成，請先檢查實際程式路徑、測試結果，以及目前 stage/progress record。

## Documentation / 文件

- [Configuration](docs/configuration.md)
- [Architecture](docs/architecture.md)
- [Cloudflare deployment](cloudflare/README.md)
- [Open-source foundations](docs/open-source-foundations.md)
- [Reader benchmark](docs/research/reader-benchmark.md)
- [Parser benchmark](docs/research/parser-benchmark.md)
- [Research archive](docs/research/README.md)

## Contributing and support

Use [GitHub Issues](https://github.com/lanefoundry/looplane/issues) for bugs and feature proposals. Read [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md) before opening a pull request. Report security vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## License

looplane is licensed under the [Apache License 2.0](LICENSE).
