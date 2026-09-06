# Looplane 能力補齊路線圖

> 目標：在保留現有差異化優勢（tool_transaction、tool_program、subagent DAG）的前提下，
> 補齊主流 coding agent 具備而 Looplane 缺少的能力。
>
> 排列依據：用戶價值 × 實作難度 × 架構影響面

---

## Phase 1 — 外部資訊取得（價值最高、風險最低）

### 1.1 `web_fetch` — Web 頁面抓取

**解決的問題**：agent 無法查閱線上文檔、API reference、changelog。

**設計**：

```
新檔案：tooling/web.py
新 tool：web_fetch
```

| 參數 | 類型 | 說明 |
|---|---|---|
| `url` | string | 目標 URL |
| `selector` | string? | CSS selector，只取頁面局部 |
| `max_chars` | int | 預設 16000，上限 32000 |

- 底層用 `httpx` + `readability-lxml`（或 `trafilatura`）提取正文
- 返回 markdown 格式，超長截斷並附 `[truncated]` 標記
- 加入 `tooling/definitions.py` 的 `tool_definitions()` 返回列表
- `executor.py` 新增 dispatch 分支，調用 `web.py`
- `read_only=True, concurrency_safe=True`

**安全考量**：
- 禁止 `file://`、`localhost`、private IP range（SSRF 防護）
- 請求 timeout 10s，響應 body 上限 2MB
- User-Agent 標明 Looplane bot

**依賴**：`httpx`（已有）、`readability-lxml` 或 `trafilatura`（新增）

**工作量**：~200 行，1 個 PR

---

### 1.2 `web_search` — Web 搜索

**解決的問題**：agent 不知道該查什麼 URL，需要先搜索。

**設計**：

```
擴充：tooling/web.py
新 tool：web_search
```

| 參數 | 類型 | 說明 |
|---|---|---|
| `query` | string | 搜索關鍵字 |
| `max_results` | int | 預設 5，上限 10 |

- 支援多 provider 後端，優先順序：
  1. 用戶配置的 API key（SerpAPI / Tavily / Brave）
  2. 無 key fallback：DuckDuckGo HTML scraping（`duckduckgo-search` 套件）
- 返回 `[{title, url, snippet}]` 結構化結果
- `read_only=True, concurrency_safe=True`

**工作量**：~150 行（在 1.1 基礎上），同 PR 或緊接

---

### 1.3 `http_request` — 通用 HTTP 請求

**解決的問題**：agent 無法調用 REST API、webhook、GraphQL。

**設計**：

```
擴充：tooling/web.py
新 tool：http_request
```

| 參數 | 類型 | 說明 |
|---|---|---|
| `method` | enum | GET / POST / PUT / PATCH / DELETE |
| `url` | string | 目標 URL |
| `headers` | object? | 自訂 header |
| `body` | string? | 請求 body |
| `timeout` | int | 預設 30s，上限 60s |

- 同樣禁止 private IP / localhost（除非 `--dangerous` 模式）
- 響應 body 截斷至 `max_output_chars`
- 需要 approval（非 read_only）因為可能有副作用
- Secret scan 檢查 header 和 body 中的 token

**工作量**：~100 行，同 PR

---

## Phase 2 — LSP 語義感知（價值高、架構已有基礎）

### 2.1 LSP 工具化 — `lsp_symbols` / `lsp_references` / `lsp_diagnostics`

**解決的問題**：大型 codebase 中 `search_text` 不夠精確，無法區分定義 vs 引用 vs 同名字串。

**現有基礎**：`lsp.py` 已有 `ManagedLspServer` 管理 LSP 進程，但目前只橋接 `publishDiagnostics` 給 IDE。

**設計**：

```
擴充：lsp.py — 新增 request/response 能力（目前只處理 notification）
新檔案：tooling/lsp_tools.py
新檔案：agent/lsp_dispatch.py（遵循 CLAUDE.md 邊界約束）
新 tools：lsp_symbols, lsp_references, lsp_definition, lsp_diagnostics
```

#### `lsp_symbols`

| 參數 | 類型 | 說明 |
|---|---|---|
| `query` | string | 符號名稱模式 |
| `path` | string? | 限定搜索範圍 |

- 底層調 `workspace/symbol` 或 `textDocument/documentSymbol`
- 返回 `[{name, kind, location}]`

#### `lsp_references`

| 參數 | 類型 | 說明 |
|---|---|---|
| `path` | string | 檔案路徑 |
| `line` | int | 行號 |
| `character` | int | 列號 |

- 底層調 `textDocument/references`
- 返回 `[{path, line, text}]`，上限 50 條

#### `lsp_definition`

- 同 `lsp_references` 參數
- 底層調 `textDocument/definition`

#### `lsp_diagnostics`

- 參數：`path`（可選）
- 返回當前 LSP 報告的 errors/warnings

**架構變更**：
- `lsp.py` 的 `ManagedLspServer` 新增 `async request(method, params)` — 發送 JSON-RPC request 並等待 response
- 需要維護 request id 和 pending response map
- `agent/lsp_dispatch.py` 遵循 ports.py Protocol 模式，由 `tool_scheduler.py` 路由

**LSP server 配置**：
- 專案級 `looplane.toml` 或 `looplane.json` 新增 `[lsp]` section
- 預設偵測：Python → `pylsp`/`pyright`，TypeScript → `typescript-language-server`，Go → `gopls`，Rust → `rust-analyzer`

**工作量**：~400 行（lsp.py 擴充 ~150 + lsp_tools.py ~150 + lsp_dispatch.py ~100），2 個 PR（先 lsp.py request 能力，再工具化）

---

## Phase 3 — 長時運行能力（解鎖前端開發流）

### 3.1 `background_process` — 背景進程管理

**解決的問題**：無法啟動 dev server、watch mode 等長時進程並持續觀察。

**設計**：

```
新檔案：tooling/background.py
新 tools：start_process, read_process, stop_process, list_processes
```

#### `start_process`

| 參數 | 類型 | 說明 |
|---|---|---|
| `command` | string | 要執行的命令 |
| `label` | string | 人類可讀標籤（如 "dev-server"） |
| `max_idle_minutes` | int | 無人讀取後自動終止，預設 30 |

- 啟動 subprocess，stdout/stderr 寫入 ring buffer（最近 2000 行）
- 返回 `process_id`
- 上限 3 個同時背景進程
- 同樣走 permissions 審批

#### `read_process`

| 參數 | 類型 | 說明 |
|---|---|---|
| `process_id` | string | 進程 ID |
| `lines` | int | 讀取最後 N 行，預設 50 |
| `pattern` | string? | grep 過濾 |

- 從 ring buffer 讀取，不阻塞

#### `stop_process`

| 參數 | 類型 | 說明 |
|---|---|---|
| `process_id` | string | 進程 ID |

- 發送 SIGTERM，5s 後 SIGKILL

#### `list_processes`

- 無參數，返回所有活躍背景進程狀態

**生命週期**：
- Agent session 結束時自動 cleanup 所有背景進程
- `tool_transaction` 中不允許 `start_process`（事務回滾無法撤銷進程啟動）
- `tool_program` 中可用 `read_process`（read_only）

**工作量**：~350 行，1 個 PR

---

### 3.2 `wait_for_output` — 條件等待

**解決的問題**：啟動 dev server 後需要等到 "ready on port 3000" 才能繼續。

**設計**：

```
擴充：tooling/background.py
新 tool：wait_for_output
```

| 參數 | 類型 | 說明 |
|---|---|---|
| `process_id` | string | 進程 ID |
| `pattern` | string | 等待匹配的文字或 regex |
| `timeout_seconds` | int | 上限 120s |

- 輪詢 ring buffer 直到匹配或 timeout
- 返回匹配行 + 上下文

**工作量**：~80 行，同 PR

---

## Phase 4 — 多媒體感知（解鎖 UI 開發）

### 4.1 `view_image` — 圖片查看

**解決的問題**：agent 無法查看截圖、設計稿、圖表。

**設計**：

```
新檔案：tooling/media.py
新 tool：view_image
```

| 參數 | 類型 | 說明 |
|---|---|---|
| `path` | string | workspace 內圖片路徑 |
| `max_dimension` | int | 縮放到最大邊長，預設 1024 |

- 支持 PNG / JPEG / GIF / WebP / SVG
- 讀取圖片，縮放後轉 base64，作為 multimodal content 返回給模型
- SVG 直接返回原始碼（文字）
- 依賴：`Pillow`（新增）

**前提**：使用的 LLM 須支持 vision（Claude、GPT-4V 等）。若不支持，返回圖片 metadata（尺寸、格式、大小）。

**工作量**：~120 行，1 個 PR

---

### 4.2 `take_screenshot` — 網頁截圖

**解決的問題**：前端開發後無法視覺驗證結果。

**設計**：

```
擴充：tooling/media.py
新 tool：take_screenshot
```

| 參數 | 類型 | 說明 |
|---|---|---|
| `url` | string | 截圖目標 URL（通常是 localhost dev server） |
| `selector` | string? | CSS selector，只截局部 |
| `width` | int | viewport 寬度，預設 1280 |
| `height` | int | viewport 高度，預設 720 |

- 底層用 Playwright（`playwright` 套件）headless browser
- 依賴可選：沒裝 Playwright 時 tool 不出現在定義列表中
- 和 `background_process` 配合：先 start dev server → wait_for_output → take_screenshot

**工作量**：~150 行，1 個 PR

---

## Phase 5 — 項目配置自動載入

### 5.1 `looplane.toml` — 結構化項目配置

**解決的問題**：沒有 `.cursorrules` / `claude.md` 式的自動載入機制。

**設計**：

```
新檔案：project_config.py
配置檔：looplane.toml（項目根目錄）
```

```toml
# looplane.toml 示例

[project]
instructions = "docs/agent-instructions.md"   # 自動載入為 system prompt

[lsp.python]
command = ["pyright-langserver", "--stdio"]

[lsp.typescript]
command = ["typescript-language-server", "--stdio"]

[tools.verification]
lint = "ruff check {path}"
test = "pytest {path} -x -q"
typecheck = "pyright {path}"

[tools.background]
max_processes = 3
auto_cleanup = true

[web]
allowed_domains = ["docs.python.org", "developer.mozilla.org"]
blocked_domains = ["*.internal.corp"]

[permissions]
auto_approve_read = true
auto_approve_shell = ["pytest", "ruff", "mypy"]
```

**載入時機**：
- `agent/context.py` 在組裝 prompt 時讀取 `looplane.toml`
- 向後相容：沒有 toml 檔就用現有 fallback 行為
- 支持 workspace-level 和 user-level（`~/.config/looplane/config.toml`）合併

**工作量**：~250 行，1 個 PR

---

## Phase 6 — 交互式終端（難度最高）

### 6.1 `interactive_shell` — 偽終端會話

**解決的問題**：`shell` 無法跑 Python REPL、`psql`、`redis-cli` 等交互式工具。

**設計**：

```
新檔案：tooling/pty_session.py
新 tools：start_session, send_input, read_session, end_session
```

#### `start_session`

| 參數 | 類型 | 說明 |
|---|---|---|
| `command` | string | 要啟動的交互式程式（如 `python3`） |
| `label` | string | 標籤 |

- 用 `pty.openpty()` + `asyncio.subprocess` 開啟偽終端
- 上限 2 個同時會話

#### `send_input`

| 參數 | 類型 | 說明 |
|---|---|---|
| `session_id` | string | 會話 ID |
| `text` | string | 要發送的輸入（自動附加 `\n`） |
| `wait_ms` | int | 等待輸出穩定的時間，預設 2000 |

- 寫入 pty，等待 `wait_ms` 後讀取新輸出
- 返回增量輸出

#### `read_session` / `end_session`

- 類似 background process 的 read/stop

**風險**：
- PTY 控制碼清理（ANSI escape sequences）
- 輸出時機不確定性（`wait_ms` 只是啟發式）
- 安全審計面更大

**工作量**：~400 行，1 個 PR。建議放最後，因為 `shell` + `background_process` 已覆蓋 80% 場景。

---

## 實作順序總覽

```
Phase 1（外部資訊）──→ Phase 2（LSP）──→ Phase 3（背景進程）
         │                                      │
         │                                      ▼
         │                              Phase 4（多媒體）
         │                                      │
         ▼                                      ▼
  Phase 5（項目配置）              Phase 6（交互式終端）
```

| Phase | 工具 | 新增行數 | PR 數 | 新依賴 |
|---|---|---|---|---|
| 1 | web_fetch, web_search, http_request | ~450 | 1-2 | trafilatura, duckduckgo-search |
| 2 | lsp_symbols, lsp_references, lsp_definition, lsp_diagnostics | ~400 | 2 | 無（LSP server 由用戶安裝） |
| 3 | start_process, read_process, stop_process, wait_for_output | ~430 | 1 | 無 |
| 4 | view_image, take_screenshot | ~270 | 1-2 | Pillow, playwright（可選） |
| 5 | looplane.toml 配置載入 | ~250 | 1 | tomli（Python <3.11）|
| 6 | interactive_shell sessions | ~400 | 1 | 無 |
| **合計** | **14 個新 tool** | **~2200** | **7-9** | **4 個** |

---

## 架構融合原則

1. **遵守 CLAUDE.md 邊界約束**：新 tool handler 放 `tooling/` 或 `agent/*_dispatch.py`，不進 `runner.py`。
2. **走 ports.py Protocol**：新增 `WebFetchPort`、`LspQueryPort`、`BackgroundProcessPort`，由 `tool_scheduler.py` 路由。
3. **tool_program 擴充**：Phase 1 的 read-only tools（`web_fetch`, `web_search`）可加入 `tool_program` 的 `op` enum，讓批量探索更強。
4. **tool_transaction 不擴充**：背景進程、交互式 shell 不可回滾，不進 transaction。
5. **漸進式可選**：每個 Phase 獨立，不裝 Playwright 就沒有 `take_screenshot`，不配 LSP 就沒有語義工具。已有工具集不受影響。
