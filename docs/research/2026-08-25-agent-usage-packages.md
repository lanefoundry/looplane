# Agent 操作資訊生態系套件調查（開發參考）

日期：2026-08-25（GitHub Search API 即時數據）
關聯：`2026-08-25-status-line-state-display.md`（looplane 狀態列設計）、
`.research/ccswitch-architecture`（已搬至 `docs/research/2026-08-22-ccswitch-architecture.md`）

## 生態系總覽

圍繞 coding agent 操作資訊的第三方套件共十大類。共通點：**幾乎全部靠解析 agent 的本地
JSONL 日誌或 statusline hook 運作**，沒有人碰 agent 內部 API——這驗證了 looplane
「log 格式即生態系入場券」的判斷。

## 1. Provider / 帳號切換器（體量最大，與 looplane 定位最接近）

| 套件 | Stars | 重點 |
|------|-------|------|
| [farion1231/cc-switch](https://github.com/farion1231/cc-switch) | **129k** | 跨平台桌面 All-in-One：Claude Code/Codex/OpenCode/OpenClaw/Grok/Hermes 的 provider 切換。looplane 的 `runtime_registry` + provider 表直接對標 |
| [jolehuit/clother](https://github.com/jolehuit/clother) | 430 | 多 provider profile 即時切換 CLI |
| [SakuraByteCore/codexmate](https://github.com/SakuraByteCore/codexmate) | 337 | 本地控制台：跨 Codex/Claude/Gemini/Pi/OpenCode 切 provider、管 session、編排任務——「local-first control plane」定位與 looplane 幾乎重疊 |
| [mcowger/plexus](https://github.com/mcowger/plexus) | 231 | 統一 API gateway + quota 追蹤 |
| [xjoker/codex-switch](https://github.com/xjoker/codex-switch) | 58 | Codex 多帳號 profile + usage dashboard TUI |

## 2. Usage / Cost 分析 CLI

| 套件 | Stars | 重點 |
|------|-------|------|
| [ccusage/ccusage](https://github.com/ccusage/ccusage) | 18.2k | `npx ccusage`；解析 `~/.claude` JSONL 出 daily/monthly/session/blocks 報表；原 TS 已重寫 Rust；生態事實標準 |
| [getagentseal/codeburn](https://github.com/getagentseal/codeburn) | 9.7k | 跨 37 工具，by model/project/task，menubar + web dashboard |
| [junhoyeo/tokscale](https://github.com/junhoyeo/tokscale) | 5.2k | 跨 agent 追蹤 + 全球 leaderboard |
| [phuryn/claude-usage](https://github.com/phuryn/claude-usage) | 2.2k | 本地 dashboard，Pro/Max 訂閱者進度條 |
| [alexgreensh/token-optimizer](https://github.com/alexgreensh/token-optimizer) | 2.0k | 「ghost tokens」診斷、compaction 存活、context 品質衰減 |
| [xiufengsun/TokenTracker](https://github.com/xiufengsun/TokenTracker) | 1.4k | 31 工具，原生 app，不讀 prompt 內容 |
| [Piebald-AI/splitrail](https://github.com/Piebald-AI/splitrail) | 217 | 即時 tracker，明列支援 **OpenCode/Pi Agent**——與 looplane runtime 表重疊 |
| [douglasmonsky/codex-usage-tracker](https://github.com/douglasmonsky/codex-usage-tracker) | 193 | Codex 用量 MCP tools + dashboard（credits/caching/thread patterns） |
| 其他 | | [aiusage](https://github.com/juliantanx/aiusage) 116★、[coding_agent_usage_tracker](https://github.com/Dicklesworthstone/coding_agent_usage_tracker) 80★（quota+rate limit+cost 一支 CLI）、[better-ccusage](https://github.com/cobra91/better-ccusage) 79★、[antigravity-usage](https://github.com/skainguyen1412/antigravity-usage) 374★、[gemistat](https://github.com/ryoppippi/gemistat) 21★ |

## 3. Statusline / HUD 外掛

| 套件 | Stars | 重點 |
|------|-------|------|
| [jarrodwatts/claude-hud](https://github.com/jarrodwatts/claude-hud) | 27.6k | Claude Code plugin：context usage、active tools、running agents、todo progress 四象限——looplane HUD 功能藍本 |
| [sirmalloc/ccstatusline](https://github.com/sirmalloc/ccstatusline) | 12.6k | 高度客製化 statusline + powerline 主題；吃 Claude Code statusLine stdin JSON |
| [GLaDO8/claude-context-visualizer](https://github.com/GLaDO8/claude-context-visualizer) | 7 | truecolor 分段條顯示 context 佔用——looplane footer ctx% 的視覺化升級方向 |

## 4. 即時監控 / Menu Bar / Limits

- [Javis603/token-monitor](https://github.com/Javis603/token-monitor)（1.7k★）桌面 widget，32+ 工具，多裝置同步
- [AThevon/TokenEater](https://github.com/AThevon/TokenEater)（480★）、[NoobyGains/claude-pulse](https://github.com/NoobyGains/claude-pulse)（451★）、[eddmann/ClaudeMeter](https://github.com/eddmann/ClaudeMeter)（137★）macOS 原生
- [babakarto/CodexBar-Win](https://github.com/babakarto/CodexBar-Win)（100★）Windows 系統列
- [domanski-ai/headroom](https://github.com/domanski-ai/headroom)（99★）跨帳號用量輪替 dashboard
- [oauramos/claude-usage-stick](https://github.com/oauramos/claude-usage-stick)（103★）ESP32 實體 monitor——生態熱情度的極端證明

## 5. Session 檢視器 / Resume

| 套件 | Stars | 重點 |
|------|-------|------|
| [HizTam/codex-history-viewer](https://github.com/HizTam/codex-history-viewer) | 26 | VS Code 擴充：瀏覽/搜尋/標籤 Codex+Claude session，直接 resume |
| [seastart/aicoder-session-viewer](https://github.com/seastart/aicoder-session-viewer) | 9 | 統一桌面 app：**Claude Code/Codex/Gemini/Antigravity/OpenCode** 的 session 瀏覽/恢復/匯出 |
| [tanghong123/claude-replay](https://github.com/tanghong123/claude-replay) | 2 | 唯讀 transcript replay（`claude --resume` 但唯讀） |

**對 looplane 的意義**：looplane 已有 `/resume`；跨 runtime 的統一 session 檢視是空白地帶，
而 looplane 的 runtime_registry 恰好握有所有 backend 的 session 位置。

## 6. DevTools / 控制台 / Observability 平台

| 套件 | Stars | 重點 |
|------|-------|------|
| [matt1398/claude-devtools](https://github.com/matt1398/claude-devtools) | 3.9k | 視覺化檢視 session logs、tool calls、token usage、subagents、context window |
| [builderz-labs/mission-control](https://github.com/builderz-labs/mission-control) | 6.1k | 自架 control plane：派發任務、審查 runs、追蹤 spend，支援 Claude Code/Codex/OpenClaw |
| [pydantic/logfire](https://github.com/pydantic/logfire) | 4.4k | 生產級 LLM/agent observability |
| [coze-dev/coze-loop](https://github.com/coze-dev/coze-loop) | 5.7k | Agent 全生命週期：開發/除錯/評估/監控 |
| [raga-ai-hub/RagaAI-Catalyst](https://github.com/raga-ai-hub/RagaAI-Catalyst) | 16.2k | Agent tracing + 自架 dashboard + 執行圖 |
| [tma1-ai/tma1](https://github.com/tma1-ai/tma1) | 116 | **local-first、agent 可讀回**的 observability——記錄每次 LLM call 經 hooks/MCP 餵回下一 turn，方向特殊值得追蹤 |

## 7. OpenTelemetry 遙測（新興標準化方向）

- [alibaba/loongsuite-pilot](https://github.com/alibaba/loongsuite-pilot)（143★）：本地遙測收集器，Claude Code/Codex/Cursor 統一成 OTel events（token/cost/traces/安全審計）
- [DEVtheOPS/opencode-plugin-otel](https://github.com/DEVtheOPS/opencode-plugin-otel)（121★）：OpenCode plugin 匯出 OTLP
- [KB1SLN-Labs/agent-observability](https://github.com/KB1SLN-Labs/agent-observability)（13★）：全自架 OTel → Prometheus + Loki → Grafana
- [RogerReed/agentlens](https://github.com/RogerReed/agentlens)（11★）：OTEL traces + 本地 log 的 VS Code 檢視

**對 looplane 的意義**：OTel 語義約定（GenAI events）正在成為 agent 遙測的交換標準；
looplane 的 `events.jsonl` 若對齊 OTel GenAI 語義，可免費接入整個 Grafana 生態。

## 8. Context Window 視覺化 / 診斷

- [h4ni0/claude-context-visualizer](https://github.com/h4ni0/claude-context-visualizer)（24★）視覺化 context 被什麼填滿
- [vibemafiaclub/show-me-the-context](https://github.com/vibemafiaclub/show-me-the-context)（5★）
- [unhealthy-outlander317/context-doctor](https://github.com/unhealthy-outlander317/context-doctor)（2★）context 診斷

## 9. 預算治理 / Spend 強制

- [RoninForge/budgetclaw](https://github.com/RoninForge/budgetclaw)（8★）：per-project/per-branch 成本 + **硬預算上限擋 agent**
- [Han-1413141/dsh-cost-meter](https://github.com/Han-1413141/dsh-cost-meter)（194★）：session/daily 成本 plugin、token heatmap、尖離峰定價
- [0xkaz/llm-governance-dashboard](https://github.com/0xkaz/llm-governance-dashboard)（3★）：團隊級 LiteLLM proxy + BigQuery 治理
- [revenium/openclaw-revenium](https://github.com/revenium/openclaw-revenium)（11★）：預算 guardrail skill

## 10. Marketplace / Awesome 清單（找新工具的入口）

| 清單 | Stars | 內容 |
|------|-------|------|
| [hesreallyhim/awesome-claude-code](https://github.com/hesreallyhim/awesome-claude-code) | 53k | 最权威總清單（skills/agents/statuslines/tooling/plugins） |
| [wshobson/agents](https://github.com/wshobson/agents) | 39.1k | 跨 harness plugin marketplace（Claude/Codex/Cursor/OpenCode/Copilot） |
| [multica-ai/andrej-karpathy-skills](https://github.com/multica-ai/andrej-karpathy-skills) | 207k | 單一 CLAUDE.md 行為優化（現象級案例） |
| [VoltAgent/awesome-claude-code-subagents](https://github.com/VoltAgent/awesome-claude-code-subagents) | 24.6k | 100+ 子代理 |
| [OthmanAdi/planning-with-files](https://github.com/OthmanAdi/planning-with-files) | 26.4k | 檔案式持久規劃，60+ agent 通用 |
| [thedotmack/claude-mem](https://github.com/thedotmack/claude-mem) | 91.8k | 跨 session 記憶壓縮回注（Claude/Codex/OpenCode 等） |
| [travisvn/awesome-claude-skills](https://github.com/travisvn/awesome-claude-skills) | 14.8k | Skills 清單 |
| 其他 | | [claude-code-plugins-plus-skills](https://github.com/jeremylongshore/claude-code-plugins-plus-skills) 2.7k★（471 plugins + ccpi 套件管理器）、[buildwithclaude](https://github.com/davepoon/buildwithclaude) 3.4k★、[anthropics/claude-plugins-community](https://github.com/anthropics/claude-plugins-community) 1.7k★、[awesome-claude-code-toolkit](https://github.com/rohitg00/awesome-claude-code-toolkit) 2.6k★、[superpowers-marketplace](https://github.com/obra/superpowers-marketplace) 1.2k★、[awesome-claude-code-plugins](https://github.com/ccplugins/awesome-claude-code-plugins) 922★ |

## 開發啟示（彙整）

1. **JSONL 內嵌 usage 是入場券**：第 2/4/5/8 類全部依賴解析本地日誌。looplane 的
   `events.jsonl` + `session.json` 已符合共識，應文件化 schema（對標 pi `docs/session-format.md`）
2. **cc-switch 129k★ 是最強市場信號**：provider/runtime 切換的需求遠大於 agent 本身——
   looplane 的 `runtime_registry` 定位踩在生態系最大痛點上，`codexmate`（337★）證明
   「local-first control plane」還有整併空間
3. **claude-hud 四象限**（context/tools/agents/todo）是 HUD 藍本；ccstatusline 證明
   「狀態 JSON → 外部渲染」契約需求真實
4. **OTel GenAI 語義**是新興交換標準（loongsuite-pilot/opencode-plugin-otel），
   looplane events 對齊後可免費接入 Grafana 生態
5. **跨 runtime 統一 session 檢視是空白地帶**：aicoder-session-viewer 只有 9★ 但方向正確，
   looplane 握有所有 backend 的 session 位置，是天然整合者
6. **預算硬上限**（budgetclaw）與 **agent 可讀回的 observability**（tma1）是新興差異化方向

## 行動計畫（2026-08-25 決策：全做）

已實作（見 git log）：

1. footer metrics（model/tokens/ctx%/elapsed）+ esc to interrupt + 串流 token 估算 + HUD 行
2. `/usage` 指令（session 用量彙整，zero context pollution）
3. `/context` 分段條視覺化
4. `docs/session-format.md`——生態入場券
5. `statusline_command` 設定（claude-code 式外部渲染契約）
6. `Limits.max_total_tokens` 預算硬上限（terminal_reason `token_budget_exceeded`）
7. `looplane sessions` 跨 run/conversation 清單
8. `looplane export-otel` OTel GenAI OTLP-JSON 匯出

後續外部動作（需人工）：

- 向 splitrail / codeburn / ccusage 提 looplane parser PR（附 `docs/session-format.md`）
- 對齊 loongsuite-pilot 的 OTel event 細節後提交整合
- 向 awesome-claude-code 提交 looplane 條目
