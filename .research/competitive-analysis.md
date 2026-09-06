# Looplane vs. 主流 Coding Agent 工具能力對比

> 基於 2026-09 原始碼實際盤點，修正先前截圖分析的幾處誤判。

---

## 已具備的核心能力

| 能力 | 工具 | 說明 |
|---|---|---|
| 文件讀寫 | `read_file` / `create_file` / `replace_text` / `apply_patch` | 覆蓋增刪改查，`read_file` 支援 offset/limit 定點讀取 |
| 搜索 | `search_text` + `shell`(grep) | 字面量和正則，底層用 ripgrep，尊重 .gitignore |
| Shell 執行 | `shell` | 可執行構建、測試、git 等；危險指令會被拒絕或要求審批 |
| 批量只讀編排 | `tool_program` | 一次 tool call 最多 8 步只讀操作，含 `repeat` / `if_contains` 控制流 |
| 原子修改事務 | `tool_transaction` | 修改 + 驗證捆綁，失敗自動回滾 — **主流 agent 均無此能力** |
| 並行調查 | `dispatch_subagents` | DAG 調度（`depends_on` 形成有向無環圖），按波次排程 |
| 版本差異查看 | `git_diff` | 隨時審查當前未提交變更 |
| MCP 工具集成 | `mcp_bridge.py` | 已實作 MCP client，支援 tool / resource / prompt discovery 和 execution |
| Skill 載入 | `invoke_skill` | 按需載入技能指令（`agent/skill_dispatch.py`） |

---

## 相對主流 Agent 的缺失

| 缺失能力 | 說明 | 影響場景 |
|---|---|---|
| Web 瀏覽 | 無法訪問網頁文檔、API 參考 | 需要查閱線上文檔或 changelog 時 |
| HTTP 請求 | 無獨立 HTTP tool，只能透過 `shell` + `curl` 間接實現 | 調用外部 REST API、webhook |
| LSP / 語義感知 | 沒有 go-to-definition、find-references，依賴 `search_text` 近似實現 | 大型 codebase 跨模組重構 |
| 圖像 / 二進制查看 | 不能渲染圖片、查看 PDF、預覽 Web 頁面 | UI 開發、設計稿對照 |
| 交互式終端 | `shell` 是單次執行，不支持 REPL（`python`、`vim`） | 需要交互式除錯時 |
| 持續後台進程 | 無法啟動 dev server 後持續觀察 | 前端開發、整合測試 |
| 結構化項目配置 | 沒有 `.cursorrules` / `claude.md` 式自動載入（`invoke_skill` 部分覆蓋） | 團隊共享項目規範 |

---

## 差異化優勢（主流 Agent 不具備）

### 1. `tool_transaction` — 原子修改事務

修改 + 檢查綁為一個單元，任一步驟失敗時自動回滾所有被 `create_file` / `replace_text` / `apply_patch` 修改的檔案。

- Claude Code：改壞了需手動 revert
- Cursor：改壞了需 Ctrl+Z 或 git checkout
- Devin：改壞了靠 git reset

這是 Looplane 最獨特的能力，保證「改了就能跑，跑不過就復原」。

### 2. `tool_program` — 批量只讀編排

一次 tool call 跑最多 8 步讀取操作，含 `repeat` 和 `if_contains` 控制流。

- 省 token：不需要每步返回給模型再發下一步
- 省延遲：一次往返完成多步探索
- 主流 agent 都是逐步 tool call

### 3. Subagent DAG 調度

`dispatch_subagents` 支援 `depends_on` 形成有向無環圖，按波次排程。

- Claude Code 的 Agent tool 可平行，但沒有 DAG 依賴概念
- Cursor / Copilot 無原生 subagent 機制

---

## 競品橫向比較

| 能力維度 | Looplane | Claude Code | Cursor | Devin |
|---|---|---|---|---|
| **核心定位** | 純代碼修改 agent | 通用 CLI agent | IDE 內嵌 agent | 全自動 SWE agent |
| 原子事務 | ✅ | ❌ | ❌ | ❌ |
| 批量讀取 | ✅ tool_program | ❌ 逐步 | ❌ 逐步 | ❌ |
| MCP 集成 | ✅ bridge | ✅ 完整 | ❌ | ❌ |
| Subagent | ✅ DAG | ✅ 平行 | ❌ | ✅ |
| Web / HTTP | ❌ | ✅ | ✅ 瀏覽器 | ✅ |
| LSP | ❌ | ❌ | ✅ 原生 | ❌ |
| 後台進程 | ❌ | ✅ Monitor | ✅ | ✅ |
| 圖像查看 | ❌ | ✅ | ✅ | ✅ |
| 交互式終端 | ❌ | ❌ | ✅ | ✅ |

---

## 總體評價

Looplane 在 **Git workspace 內的純代碼修改場景**中工具完整且高效。`tool_transaction`（原子事務）和 `tool_program`（批量編排）是真正的差異化護城河，其他主流 agent 均無此設計。

最大短板是**外部交互能力**（Web、HTTP、LSP）和**長時運行能力**（後台進程、交互式終端），這些會影響：
- 需要查文檔 / 調 API 的場景
- 大型 codebase 的語義級重構
- 前端開發等需要持續觀察的工作流

**優先補強建議**：Web 瀏覽（對查文檔場景價值最高）> LSP 整合（大 codebase 體驗躍升）> 後台進程（解鎖前端開發流）。
