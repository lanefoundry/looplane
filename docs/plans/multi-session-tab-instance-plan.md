# Multi-session tabs in one Looplane instance (proposal)

日期：2026-09-05

## 目標

- 目標：同一個 Looplane process 同時支援多個 session；每個 tab 對應一個 session。
- 狀態隔離：每個 session 的 agent state、對話生命週期與審計記錄獨立。
- Workspace：共用一份 workspace context（例如同一個 repo context 資訊），但對可寫工作區需明確定義範圍與邊界。

## 現況結論（目前代碼）

### 已有基礎

- `ConversationStore` 有 per conversation 的 durable store、writer lease、JSONL 事件、resume/clear/fork 等能力，代表「多個 conversation 並存」基礎是存在的。
- `ConversationController` + `ConversationRuntimeSession` 已有「一組會話控制器」抽象，`ConversationRuntimeSession` 及其 `_workspace` 封裝（如 `IsolatedCodexConversation`/`IsolatedClaudeConversation`）各自持有自己的生命週期。
- `conversation-runtime` 路徑下有清楚的 per-runtime contract，便於用不同 session 組合。

### 尚缺 (不符合題目直接要求)

- `conversation-server` CLI 入口會建立一個 `session = session_cls(...)`，再包成單一 `ConversationWebSocketApp(session, ...)`。
  - 這代表目前**同 process 多 tab 連線共用同一個 `ConversationController`/session**（取決於 server 佈署與連線層行為），不自動保證「每個 tab 一個 controller/session 狀態」。
- `Isolated*Conversation.start()` 建立的是每次會話各自的 `ConversationWorkspace`（`ConversationWorkspace.create(...)`）。
  - 這是「每 session workspace 寫入隔離」，不符合「shared workspace context」的「可共享可寫」想像。

## 路徑判斷

在現在代碼下，最貼近目標的落地方式是：

1. **共用 workspace context（推薦）**：共享「只讀 context」而非可寫 workspace。
   - 例如 repo metadata、git baseline 指標、允許路徑、警告/摘要訊息等。
   - 實際 side effects 仍保留在各自的 disposable workspace（或有明確共享策略）。

2. **完整共享 writable workspace（高風險）**：若每 tab 要寫同一可寫工作目錄，需額外解決衝突、檔案鎖、事件歸屬與回滾。
   - 目前架構對 dirty source/workspace 安全性高度偏向隔離，不建議直接共用可寫 workspace。

結論：在「共用 workspace context 且 agent state 独立」的工程上，**應採只讀 context 共享 + writable workspace 獨立**，否則會重寫大量安全與一致性邏輯。

## 建議實作切片（文件化）

### Slice A：每 tab/session 有獨立 controller + runtime session

- 建立 `ConversationServerCoordinator`（或在 `ConversationWebSocketApp` 加入工廠）
  - 為每條 WebSocket 握手分配一個 `conversation_id`。
  - 每個對話產生獨立 `ConversationRuntimeSession` 與 `ConversationController` 實例。
- `GET /v1/conversation/attach` 行為維持不變；只在 server 端改為 session-factory。

涉及檔案：

- `src/looplane/conversation_websocket.py`
- `src/looplane/cli.py`（`conversation-server` 路由）
- `src/looplane/conversation.py`（若需要建立/解析每-tab conversation id 與 lease）

### Slice B：定義「共享 context」邊界

- 新增一個 `SharedWorkspaceContext`（可快取）供多個 session 注入：
  - source snapshot metadata（提交、脏文件警告摘要）
  - project context providers（watcher/skills/instruction）
  - runtime context injection
- 不共享 `ConversationWorkspace` 寫入目錄。

涉及檔案：

- `src/looplane/conversation_controller.py`（context injection 前後資料來源拆分）
- `src/looplane/loop.py`（若需要 run/iteration 共享快取參考）
- `src/looplane/runtime.py` 或 `src/looplane/codex_conversation.py`（如需 context 來源重構）

### Slice C：會話間隔離（硬保證）

- 保持 per-conversation writer lease（`ConversationStore`）與 event 流獨立。
- 每個 controller 狀態（in-flight turn、pending injected context、compaction history）限定在 session 實例。
- 在前端/客戶端以 `conversation_id` 做 UI tab 標識，不再依賴 runtime session id。

涉及檔案：

- `src/looplane/conversation.py`
- `src/looplane/conversation_controller.py`
- `src/looplane/tui.py`（如需多 conversation tab 展示對齊）

### Slice D：共享工作空間上下文的行為規範

- 規範「context snapshot」更新時機：首次 attach、定期失效、on-demand。
- 規範上下文版本欄位：`workspace_context_version`（便於比對 stale context）。
- 對話開始時寫入 `source_snapshot_warning` 到事件流，避免 tab 之間誤用。

## 證據門檻（文件要求）

- 需求文件：本文件作為提案。
- 需要實作後再補：
  1. two-tab 同時 attach：各自 `conversation_id` 不串話
  2. 一個 tab 的 interruption/關閉不影響其他 session
  3. context 注入可共享、但 run-level side effects 無 cross-session 汙染
  4. 每個 conversation 可 resume 且不誤吃其他 conversation 事件

## 風險清單

- WebSocket lifecycle 多連線下的資源回收：離線 tab 未釋放 controller
- Session 共享 workspace 的誤解：若未明定 read-only，實作會與既有隔離模型衝突
- context 與 writer state 的邊界不清，容易造成「共享但實際可變」的灰色態

## 建議決策

- 當前可先採「共用只讀 workspace context + 每 session 寫入 workspace 獨立 + per-tab session/controller」
- 一旦上述完成，若要做真正共享可寫工作區，再另開「Wave X」處理檔案衝突與交易一致性。

## 目前代碼可直接參考位置

- 多會話基礎：`src/looplane/conversation.py`
- 會話控制：`src/looplane/conversation_controller.py`
- WebSocket attach 入口：`src/looplane/conversation_websocket.py`、`src/looplane/cli.py` (`serve_conversation_server`)
- native 會話實作與隔離 workspace：`src/looplane/codex_conversation.py`、`src/looplane/claude_conversation.py`
- workspace clone 基礎：`src/looplane/conversation_workspace.py`
