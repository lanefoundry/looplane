<div align="center">

# looplane

**AI agent 值得信賴的 Python-first 編碼代理。**

[![CI](https://github.com/vincentxuu/looplane/actions/workflows/python-ci.yml/badge.svg)](https://github.com/vincentxuu/looplane/actions/workflows/python-ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Status](https://img.shields.io/badge/status-early_preview-orange.svg)

[快速開始](#quick-start) · [日常使用](#daily-cli) · [雲端控制平面](#cloudflare-control-plane) · [文件](#documentation)

[English](README.md) · [繁體中文](README.zh-TW.md)

</div>

looplane 是給本機 repository 使用的 Python-first coding agent。它把模型能碰到的工具面縮小，所有修改都先進到固定 commit 的 disposable clone，留下可稽核的事件與 artifact，最後以實際 verification 結果作為是否完成的依據。

looplane 可當作互動式的日常 CLI 使用，同時保持有界、可稽核的無頭模式供 CI 與未來的 Cloudflare 執行使用。它有兩條並行的執行路徑：本專案自己實現的原生 harness，擁有 loop、approval、sessions、tools、verification 與 model API adapters；以及明確選定的外部 coding CLI runtime（Claude Code、Codex CLI、OpenCode、Pi、OMP），它們各自擁有 agent loop，但共享 looplane 的對話 UI、workspace safety、patch audit 與 verification boundary。兩條路徑從不互相偽裝。

> [!IMPORTANT]
> looplane 目前是早期預覽版（`0.1.0`），工具契約與部署行為仍可能調整。目標中的 OSS V1 Stable Release 是 operator-hosted open-source product。looplane 不是 CAPTCHA solver，也不保證繞過所有反爬機制。

專案提供 provider-neutral 的 `ModelProvider` 契約，包含規範化的訊息、工具呼叫、能力、使用量與分類錯誤。它支援 OpenAI-compatible APIs、Ollama、Anthropic、Gemini、Cloudflare Workers AI，以及明確標成 experimental 的 app-owned ChatGPT/Codex OAuth。外部 runtime 是明確 opt-in 的本機 delegation——它們擁有自己的登入與 agent loop，但 looplane 仍提供對話 UI、disposable clone、patch audit 與 final checks。

## What Works Today / 目前能力

- 全螢幕 `looplane` TUI，支援 runtime/model 選擇、inline slash commands、approval、streaming tool activity、transcript scrollback、`/new`、`/resume`、`/history`、`/usage`、`/context` 與 cooperative stop。
- Headless `looplane exec` / `looplane -p` 支援 path allowlists、精確 check commands、deterministic run bundles，以及 `looplane resume` 驗證 non-terminal runs。
- Provider-neutral 原生 loop，支援 OpenAI-compatible APIs、Ollama、Anthropic、Gemini、Cloudflare Workers AI，以及明確標成 experimental 的 app-owned ChatGPT/Codex OAuth。
- 首次運行的 provider 設定、本地 Ollama 發現、provider credential 儲存、即時 credential 驗證，以及支援動態模型列表。
- 外部 runtime adapters 支援官方 Claude Code、官方 Codex CLI、OpenCode、Pi、OMP，都在 disposable clones 內運行。
- 儲存庫本地的 `.looplane/skills/*.md`、opt-in blocking hooks、plugin manifests、IDE/LSP snapshots，以及 `editors/vscode` 下的 VS Code bridge 雛形。
- 原生 MCP client 支援 looplane 擁有的 OAuth grants 與 MCP tools/resources 的 approval 分類。
- 程式化 subagent 派遣與原生有界 `dispatch_subagents` fan-out，適用於 scout、analyst 與 reviewer 子工作空間。
- 對話持久化、WebSocket attach、deterministic replay/fork helpers、SDK facade、session usage summaries、cost estimates 與 OpenTelemetry GenAI export。
- `cloudflare/` 下的 Cloudflare Worker/Sandbox 控制平面，支援非同步、text-source-map 遠端執行，提供 durable status、event、approval、cancel 與 artifact routes。

中文摘要：

- `looplane` 會開全螢幕 TUI，支援 runtime/model 選擇、slash commands、approval、tool stream、scrollback、resume/history、usage/context 與可控停止。
- `looplane exec` 和 `looplane -p` 可做 headless run，保留 path allowlist、精準 check command、run bundle 與可恢復的 non-terminal session。
- 原生 loop 支援 OpenAI-compatible、Ollama、Anthropic、Gemini、Cloudflare Workers AI，以及明確標成 experimental 的 app-owned ChatGPT/Codex OAuth。
- 外部 runtime 可接 Claude Code、Codex CLI、OpenCode、Pi、OMP；它們只改 disposable clone，looplane 仍負責 patch audit 和 final checks。
- 目前也有 repository-local skills/hooks/plugins、IDE/LSP snapshot、VS Code bridge、MCP client、subagents、conversation persistence、SDK、usage/cost、OTel export，以及 `cloudflare/` remote control plane。

## Quick start

需求：Python 3.11+、uv 與 Git。

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```

安裝或更新可編輯的每日指令：

```bash
scripts/install-dev-cli
looplane --help
```

可編輯指令會立即讀取來源變更，但獨立的工具環境不會在依賴項變更時自動更新。變更 `pyproject.toml` 或 `uv.lock` 後請重新跑 `scripts/install-dev-cli`。

沒有 `requirements.txt`；依賴項聲明在 `pyproject.toml` 並鎖定在 `uv.lock`。

開發環境使用 `uv`。`.venv/` 由 `uv` 管理；如果改了 `pyproject.toml` 或 `uv.lock`，重新跑 `scripts/install-dev-cli`，讓全域 `looplane` 指令同步到目前 lock 檔。

## Daily CLI / 日常使用

```bash
# 在目前 Git repository 開啟全螢幕對話。
looplane

# 從命令列提問或執行任務。
looplane "Explain the failing test."
looplane "Fix the failing test without changing its intent." --check "pytest -q"
looplane -C /path/to/repo "Explain and fix the failure."

# 非互動模式，輸出 JSON。
looplane -p "Summarize this repository."

# Headless coding run，明確指定驗證命令。
looplane exec "Fix the bounded bug and keep existing behavior." \
  -C /absolute/path/to/repo \
  --allowed-path "src/**" \
  --allowed-path "tests/**" \
  --check "pytest -q" \
  --tool-calling \
  --unsafe-local-exec

# 給受限 terminal 或 SSH troubleshooting 使用的 plain mode。
looplane --plain
```

`looplane [PROMPT]` 是互動式的。`looplane -p [PROMPT]` 和 `looplane exec [PROMPT]` 是非互動模式。`looplane run`、`--task`、`--repo` 仍保留作相容 alias。`-p` 現在是 `--print`，provider 請用 `--provider` 或 `looplane config` 設定。

`looplane [PROMPT]` 走互動式流程；`looplane -p [PROMPT]` 和 `looplane exec [PROMPT]` 是非互動模式。`looplane run`、`--task`、`--repo` 仍保留作相容 alias。`-p` 現在是 `--print`，provider 請用 `--provider` 或 `looplane config` 設定。

常用指令：

```bash
looplane config --interactive
looplane sessions
looplane resume last
looplane export-otel <run-id> -o run.otel.json
looplane gateway --provider ollama --model qwen3:4b --port 8788
looplane conversation-server --help
looplane policy --help
```

## Runtime And Provider Setup / Runtime 與 Provider 設定

設定非 secret 預設值：

```bash
looplane config --provider ollama --model qwen3:4b
looplane config --provider openai-compatible --model your-model \
  --api-url https://gateway.example/v1
```

儲存 looplane 擁有的 API credentials：

```bash
looplane auth set-key openai-compatible
looplane auth set-key anthropic
looplane auth set-key gemini
looplane auth set-key workers-ai
looplane auth list
looplane auth list --verify
```

支援的原生 providers：

| Provider | CLI value | Credential source |
| --- | --- | --- |
| OpenAI or compatible endpoint | `openai-compatible` | `OPENAI_API_KEY`、儲存的金鑰、選用的 `OPENAI_BASE_URL` |
| Ollama | `ollama` | 本機 loopback 不需要金鑰；遠端 HTTPS 可使用 `OLLAMA_API_KEY` |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` 或儲存的金鑰 |
| Gemini | `gemini` | `GEMINI_API_KEY`、`GOOGLE_API_KEY` 或儲存的金鑰 |
| Cloudflare Workers AI | `workers-ai` | 帳戶 ID 和 API token |
| ChatGPT/Codex 訂閱 | `openai-codex` | 透過 `looplane auth login-codex` 的 app-owned OAuth |

ChatGPT/Codex 訂閱路徑是明確且實驗性的：

Provider/model/API URL 等非 secret 設定存在 looplane config；API key 和 OAuth grant 則由 looplane 自己的 auth store 或環境變數提供。ChatGPT/Codex subscription 路徑是 explicit experimental 功能，和官方 Codex CLI runtime 不是同一條路。

```bash
looplane auth login-codex
looplane auth status-codex
looplane --provider openai-codex --model <supported-codex-model> \
  --experimental-subscription
```

looplane 會為該路徑建立自己的 credential。它不會讀取 `~/.codex`、Claude Code、Pi、OpenCode、OMP 或其他 CLI 的 credential 檔案。

## External Coding Runtimes / 外部 Coding Runtime

外部 runtime 是明確 opt-in 的本機 delegation。子 CLI 只編輯 disposable clone；looplane 獨立審計 path-bounded patch 並執行宣告的 final verification。

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

外部 runtime 支援是為可信任的本地儲存庫設計的。它不是 hostile-code sandbox。Codex CLI 增加自己的 `workspace-write` sandbox；Claude Code 路徑限制啟用的文件工具，但官方子 CLI 仍使用自己的本地認證環境。

外部 runtime 是明確 opt-in 的本機 delegation。Claude Code、Codex CLI、OpenCode、Pi、OMP 各自擁有登入與 agent loop；looplane 只交給它們 disposable clone，並在結束後重新檢查 path-bounded patch 與 verification。這是 trusted local repo 的工作流，不是 hostile-code sandbox。

## Safety Boundary / 安全邊界

looplane 的預設本地邊界是 disposable Git workspace 加上 Python policy checks：

- 原始 worktrees 不會直接被編輯；
- 變更的文件必須符合 allowed path 策略；
- 修改與執行在互動模式下需要 approval；
- verification 命令是 exact argv，不是 shell string；
- run bundles 包含 request、events、checkpoint、session、verification、patch、test log、result 與 workspace artifacts；
- provider credentials 留在 coordinator process 中，不會轉交給 repository checks。

`--unsafe-local-exec` 允許在 host 上執行可信任 repository 的檢查。如果有可用的 `--sandbox-checks`，可提供額外的 verification-command 隔離。在 macOS 上使用平台 sandbox wrapper；在 Linux 上，`sandbox_backend` 可選擇 `auto`、`bubblewrap` 或 `landlock`。

本機預設安全邊界是 disposable Git workspace 加上 Python policy checks。互動模式下修改與執行需要 approval；verification command 是 exact argv，不是 shell string；provider credential 留在 coordinator process，不會轉交給 repository checks。`--unsafe-local-exec` 表示你同意在 host 上跑 trusted repo 的檢查。

## Cloudflare Control Plane / Cloudflare 控制平面

`cloudflare/` 在 Worker 和 Cloudflare Sandbox 背後封裝 Python runtime。Worker 擁有 HTTP auth 與 provider credentials，階段化一個有界的純文字原始碼樹，開始一個非同步 run，暴露 durable status 與 events，處理 approvals/cancel，並提供有界 artifacts。

它不接受 Git URLs、archives、shell strings、provider credentials、consumer subscription tokens、自訂的 caller-selected upstreams 或任意 model IDs。有關精確的 API 與證據邊界，請見 [cloudflare/README.md](cloudflare/README.md) 與 [docs/stages/m6-cloudflare-sandbox-service.md](docs/stages/m6-cloudflare-sandbox-service.md)。

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

一般檢查：

```bash
uv run pytest
uv run ruff check .
git diff --check
```

離線 loop 證明：

```bash
uv run python scripts/demo_fixture.py
```

可重複的真 provider 評估：

```bash
eval_root=$(mktemp -d /tmp/looplane-live-eval.XXXXXX)
uv run python scripts/eval_live_provider.py \
  --provider ollama \
  --model qwen3:4b \
  --output-dir "$eval_root/ollama-qwen3-4b"
```

TUI 佈局變更需要專注的幾何測試與渲染檢視圖像：

```bash
uv run pytest tests/test_tui.py -q
uv run python scripts/render_tui_screenshot.py --width 120 --height 36 --name wide
uv run python scripts/render_tui_screenshot.py --width 60 --height 22 --name narrow
uv run python scripts/render_tui_screenshot.py --state thinking --name loading
```

在將 TUI 變更視為完成之前，請審查生成的 `.artifacts/tui/*.png` 圖像。

TUI 改動除了測試，也要產生寬版、窄版與 loading 狀態截圖，實際看過 `.artifacts/tui/*.png` 後才算完成。

## Documentation Map / 文件地圖

- [docs/progress.md](docs/progress.md)：里程碑狀態、acceptance criteria 與專案邊界。
- [docs/stages](docs/stages/README.md)：可重複的里程碑記錄與驗證證據。
- [docs/sdk.md](docs/sdk.md)：SDK facade、WebSocket attach、replay/fork API、role lanes 與 policy boundaries。
- [docs/session-format.md](docs/session-format.md)：run events、session schema 與使用量指標。
- [docs/startup-performance-playbook.md](docs/startup-performance-playbook.md)：啟動預算與 lazy-import 指引。
- [docs/agent-diff-report.md](docs/agent-diff-report.md)：對參考 coding-agent 架構的目前能力差距/backlog。

`docs/agent-diff-report.md` 中的 backlog 項目不是實作證據。在宣稱某個能力已完成之前，請先檢查實際程式路徑、測試結果，以及目前 stage/progress record。

`docs/agent-diff-report.md` 是 backlog，不是完成證據。要宣稱某個能力已完成，請先檢查實際程式路徑、測試結果，以及目前 stage/progress record。

## Documentation / 文件

- [設定](docs/configuration.md)
- [架構](docs/architecture.md)
- [Cloudflare 部署](cloudflare/README.md)
- [開源技術基礎](docs/open-source-foundations.md)
- [Reader benchmark](docs/research/reader-benchmark.md)
- [Parser benchmark](docs/research/parser-benchmark.md)
- [研究封存](docs/research/README.md)

## Contributing and support

一般 bug 與功能提案請使用 [GitHub Issues](https://github.com/vincentxuu/looplane/issues)。送出 pull request 前請閱讀 [CONTRIBUTING.md](CONTRIBUTING.md) 與[行為準則](CODE_OF_CONDUCT.md)。安全漏洞請依 [SECURITY.md](SECURITY.md) 私下通報。

## 授權

looplane 使用 [Apache License 2.0](LICENSE) 授權。