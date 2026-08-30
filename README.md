# Rivumi

Rivumi is a Python-first coding agent for local repositories. It gives the model
a bounded tool surface, runs work in disposable committed-HEAD clones, records an
auditable event bundle, and treats final verification as the source of truth.

Rivumi 是給本機 repository 使用的 Python-first coding agent。它把模型能碰到的
工具面縮小，所有修改都先進到固定 commit 的 disposable clone，留下可稽核的
事件與 artifact，最後以實際 verification 結果作為是否完成的依據。

The project has two execution paths:

- **Rivumi Agent**: this repository's provider-neutral Python loop, tool policy,
  approvals, sessions, model adapters, and verification gate.
- **External coding runtimes**: installed CLIs such as Claude Code, Codex CLI,
  OpenCode, Pi, and OMP. They own their own login and agent loop, but Rivumi
  still provides the conversation UI, disposable clone, patch audit, and final
  checks.

Those paths are intentionally separate. Rivumi does not read another CLI's
credential store, and external CLIs are never treated as `ModelProvider`
implementations.

這兩條路徑刻意分開：Rivumi Agent 是本專案自己的 agent loop；外部 coding CLI
則由各自官方工具負責登入與推理。Rivumi 不讀其他 CLI 的 credential store，也不
把外部 CLI 包裝成隱藏的 `ModelProvider`。

## What Works Today / 目前能力

- Full-screen `rivumi` TUI with runtime/model selection, inline slash commands,
  approvals, streaming tool activity, transcript scrollback, `/new`, `/resume`,
  `/history`, `/usage`, `/context`, and cooperative stop.
- Headless `rivumi exec` / `rivumi -p` runs with path allowlists, exact check
  commands, deterministic run bundles, and `rivumi resume` for validated
  non-terminal runs.
- Provider-neutral native loop for OpenAI-compatible APIs, Ollama, Anthropic,
  Gemini, Cloudflare Workers AI, and the explicit experimental app-owned
  ChatGPT/Codex OAuth transport.
- First-run provider setup, local Ollama discovery, provider credential storage,
  live credential verification, and dynamic model listing where supported.
- External runtime adapters for official Claude Code, official Codex CLI,
  OpenCode, Pi, and OMP, all operating inside disposable clones.
- Repository-local `.rivumi/skills/*.md`, opt-in blocking hooks, plugin
  manifests, IDE/LSP snapshots, and a VS Code bridge scaffold under
  `editors/vscode`.
- Native MCP client support with Rivumi-owned OAuth grants and approval
  classification for MCP tools/resources.
- Programmatic subagent dispatch and native bounded `dispatch_subagents` fan-out
  for scout, analyst, and reviewer child workspaces.
- Conversation persistence, WebSocket attach, deterministic replay/fork helpers,
  SDK facade, session usage summaries, cost estimates, and OpenTelemetry GenAI
  export.
- Cloudflare Worker/Sandbox control plane under `cloudflare/` for asynchronous,
  text-source-map remote runs with durable status, event, approval, cancel, and
  artifact routes.

中文摘要：

- `rivumi` 會開全螢幕 TUI，支援 runtime/model 選擇、slash commands、approval、
  tool stream、scrollback、resume/history、usage/context 與可控停止。
- `rivumi exec` 和 `rivumi -p` 可做 headless run，保留 path allowlist、精確
  check command、run bundle 與可恢復的 non-terminal session。
- 原生 loop 支援 OpenAI-compatible、Ollama、Anthropic、Gemini、Cloudflare
  Workers AI，以及明確標成 experimental 的 app-owned ChatGPT/Codex OAuth。
- 外部 runtime 可接 Claude Code、Codex CLI、OpenCode、Pi、OMP；它們只改
  disposable clone，Rivumi 仍負責 patch audit 和 final checks。
- 目前也有 repository-local skills/hooks/plugins、IDE/LSP snapshot、VS Code
  bridge、MCP client、subagents、conversation persistence、SDK、usage/cost、
  OTel export，以及 `cloudflare/` remote control plane。

## Install For Development / 開發安裝

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```

Install or refresh the editable daily command:

```bash
scripts/install-dev-cli
rivumi --help
```

The editable command reads source changes immediately, but its isolated tool
environment does not automatically update when dependencies change. Run
`scripts/install-dev-cli` again after changing `pyproject.toml` or `uv.lock`.

There is no `requirements.txt`; dependencies are declared in `pyproject.toml`
and locked in `uv.lock`.

開發環境使用 `uv`。`.venv/` 由 `uv` 管理；如果改了 `pyproject.toml` 或
`uv.lock`，重新跑 `scripts/install-dev-cli`，讓全域 `rivumi` 指令同步到目前
lock 檔。

## Daily CLI / 日常使用

```bash
# Open the full-screen conversation in the current Git repository.
# 在目前 Git repository 開啟全螢幕對話。
rivumi

# Ask or act from the command line.
# 從命令列提問或執行任務。
rivumi "Explain the failing test."
rivumi "Fix the failing test without changing its intent." --check "pytest -q"
rivumi -C /path/to/repo "Explain and fix the failure."

# Non-interactive JSON output.
# 非互動模式，輸出 JSON。
rivumi -p "Summarize this repository."

# Headless coding run with explicit verification.
# Headless coding run，明確指定驗證命令。
rivumi exec "Fix the bounded bug and keep existing behavior." \
  -C /absolute/path/to/repo \
  --allowed-path "src/**" \
  --allowed-path "tests/**" \
  --check "pytest -q" \
  --tool-calling \
  --unsafe-local-exec

# Fallback for limited terminals and SSH troubleshooting.
# 給受限 terminal 或 SSH troubleshooting 使用的 plain mode。
rivumi --plain
```

`rivumi [PROMPT]` is interactive. `rivumi -p [PROMPT]` and
`rivumi exec [PROMPT]` are non-interactive. `rivumi run`, `--task`, and `--repo`
remain compatibility aliases. `-p` means `--print`; use `--provider` or
`rivumi config` to choose the provider.

`rivumi [PROMPT]` 走互動式流程；`rivumi -p [PROMPT]` 和
`rivumi exec [PROMPT]` 是非互動模式。`rivumi run`、`--task`、`--repo`
仍保留作相容 alias。`-p` 現在是 `--print`，provider 請用 `--provider` 或
`rivumi config` 設定。

Useful commands:

```bash
rivumi config --interactive
rivumi sessions
rivumi resume last
rivumi export-otel <run-id> -o run.otel.json
rivumi gateway --provider ollama --model qwen3:4b --port 8788
rivumi conversation-server --help
rivumi policy --help
```

## Runtime And Provider Setup / Runtime 與 Provider 設定

Configure non-secret defaults:

```bash
rivumi config --provider ollama --model qwen3:4b
rivumi config --provider openai-compatible --model your-model \
  --api-url https://gateway.example/v1
```

Store Rivumi-owned API credentials:

```bash
rivumi auth set-key openai-compatible
rivumi auth set-key anthropic
rivumi auth set-key gemini
rivumi auth set-key workers-ai
rivumi auth list
rivumi auth list --verify
```

Supported native providers:

| Provider | CLI value | Credential source |
| --- | --- | --- |
| OpenAI or compatible endpoint | `openai-compatible` | `OPENAI_API_KEY`, stored key, optional `OPENAI_BASE_URL` |
| Ollama | `ollama` | local loopback needs no key; remote HTTPS can use `OLLAMA_API_KEY` |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` or stored key |
| Gemini | `gemini` | `GEMINI_API_KEY`, `GOOGLE_API_KEY`, or stored key |
| Cloudflare Workers AI | `workers-ai` | account id and API token |
| ChatGPT/Codex subscription | `openai-codex` | app-owned OAuth via `rivumi auth login-codex` |

The ChatGPT/Codex subscription path is explicit and experimental:

Provider/model/API URL 等非 secret 設定存在 Rivumi config；API key 和 OAuth grant
則由 Rivumi 自己的 auth store 或環境變數提供。ChatGPT/Codex subscription 路徑
是 explicit experimental 功能，和官方 Codex CLI runtime 不是同一條路。

```bash
rivumi auth login-codex
rivumi auth status-codex
rivumi --provider openai-codex --model <supported-codex-model> \
  --experimental-subscription
```

Rivumi creates its own credential for that path. It does not read `~/.codex`,
Claude Code, Pi, OpenCode, OMP, or other CLI credential files.

## External Coding Runtimes / 外部 Coding Runtime

External runtimes are opt-in local delegation. The child CLI edits only the
disposable clone; Rivumi independently audits the path-bounded patch and runs
the declared final verification.

```bash
rivumi backend codex-cli \
  --repo /path/to/trusted/repo \
  --task "Fix the failing test." \
  --allowed-path "src/**" \
  --check "pytest -q" \
  --experimental-subscription \
  --allow-external-modify \
  --unsafe-local-exec

rivumi backend claude-code \
  --repo /path/to/trusted/repo \
  --task "Fix the failing test." \
  --allowed-path "src/**" \
  --check "pytest -q" \
  --experimental-subscription \
  --allow-external-modify \
  --unsafe-local-exec

rivumi backend opencode \
  --repo /path/to/trusted/repo \
  --task "Fix the failing test." \
  --allowed-path "src/**" \
  --check "pytest -q" \
  --model "ollama/gemma4" \
  --allow-external-modify \
  --unsafe-local-exec
```

External runtime support is designed for trusted local repositories. It is not a
hostile-code sandbox. Codex CLI adds its own `workspace-write` sandbox; the
Claude Code path limits enabled file tools, but the official child still uses
its own local authentication environment.

外部 runtime 是明確 opt-in 的本機 delegation。Claude Code、Codex CLI、
OpenCode、Pi、OMP 各自擁有登入與 agent loop；Rivumi 只交給它們 disposable
clone，並在結束後重新檢查 path-bounded patch 與 verification。這是 trusted
local repo 的工作流，不是 hostile-code sandbox。

## Safety Boundary / 安全邊界

Rivumi's default local boundary is a disposable Git workspace plus Python policy
checks:

- source worktrees are not edited directly;
- changed files must match the allowed path policy;
- modify and execute actions require approval in interactive mode;
- verification commands are exact argv, not shell strings;
- run bundles contain request, events, checkpoint, session, verification,
  patch, test log, result, and workspace artifacts;
- provider credentials stay in the coordinator process and are not forwarded to
  repository checks.

`--unsafe-local-exec` allows trusted repository checks to run on the host. Use
`--sandbox-checks` where available for additional verification-command
containment. On macOS this uses the platform sandbox wrapper; on Linux,
`sandbox_backend` can select `auto`, `bubblewrap`, or `landlock`.

本機預設安全邊界是 disposable Git workspace 加上 Python policy checks。互動模式
下修改與執行需要 approval；verification command 是 exact argv，不是 shell
string；provider credential 留在 coordinator process，不會轉交給 repository
checks。`--unsafe-local-exec` 表示你同意在 host 上跑 trusted repo 的檢查。

## Cloudflare Control Plane / Cloudflare 控制平面

`cloudflare/` packages the Python runtime behind a Worker and Cloudflare
Sandbox. The Worker owns HTTP auth and provider credentials, stages a bounded
text-only source tree, starts an asynchronous run, exposes durable status and
events, handles approvals/cancel, and serves bounded artifacts.

It deliberately does not accept Git URLs, archives, shell strings, provider
credentials, consumer subscription tokens, custom caller-selected upstreams, or
arbitrary model IDs. See [cloudflare/README.md](cloudflare/README.md) and
[docs/stages/m6-cloudflare-sandbox-service.md](docs/stages/m6-cloudflare-sandbox-service.md)
for the exact API and evidence boundary.

`cloudflare/` 是遠端 Worker/Sandbox control plane。Worker 負責 HTTP auth 與
provider credential，Sandbox 收到的是 bounded text-source-map 和 run-scoped
capability。它不接受 Git URL、archive、shell string、caller provider credential、
subscription token、任意 upstream 或任意 model ID。

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
eval_root=$(mktemp -d /tmp/rivumi-live-eval.XXXXXX)
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

Review the generated `.artifacts/tui/*.png` images before treating a TUI change
as complete.

TUI 改動除了測試，也要產生寬版、窄版與 loading 狀態截圖，實際看過
`.artifacts/tui/*.png` 後才算完成。

## Documentation Map / 文件地圖

- [docs/progress.md](docs/progress.md): milestone status, acceptance criteria,
  and project boundaries.
- [docs/stages](docs/stages/README.md): reproducible milestone records and
  verification evidence.
- [docs/sdk.md](docs/sdk.md): SDK facade, WebSocket attach, replay/fork API,
  role lanes, and policy boundaries.
- [docs/session-format.md](docs/session-format.md): run events, session schema,
  and usage metrics.
- [docs/startup-performance-playbook.md](docs/startup-performance-playbook.md):
  startup budget and lazy-import guidance.
- [docs/agent-diff-report.md](docs/agent-diff-report.md): current capability
  gap/backlog against reference coding-agent architectures.

Backlog items in `docs/agent-diff-report.md` are not implementation proof.
Before claiming a capability is done, verify the code path, tests, and current
stage/progress record.

`docs/agent-diff-report.md` 是 backlog，不是完成證據。要宣稱某個能力已完成，
請先檢查實際程式路徑、測試結果，以及目前 stage/progress record。
