# Status Line / Agent 狀態顯示設計研究

日期：2026-08-25
參考庫：`~/Projects/coding-agent-reference/`（pi-mono、oh-my-pi、opencode、codex、claude-code-source）

## 問題

rivumi 的 TUI footer 只有 `status · terminal_reason · N changed file(s)`（`src/rivumi/tui.py` `_result_status`）。
token 用量、模型名、耗時都已被追蹤並持久化（`RunResult.usage`、`checkpoint.json`、
`session.json` 的 `active_wall_time_seconds`、`ContextUsageUpdatedEvent`），但 UI 只能透過
`/context`、`/status` 手動查詢，平時看不到。

## 各家做法

### pi-mono（footer 最自足，版面參考來源）

- Footer 兩行：上 `cwd (branch) · session`，下 `↑input ↓output R cacheRead $cost context% model`。
  `pi-mono/packages/coding-agent/src/modes/interactive/components/footer.ts:84-244`
- token/cost 由 footer 遍歷 session entries 累加（`footer.ts:91-104`）；
  context% 由 `AgentSession.getContextUsage()` 計算（`agent-session.ts:3299`）。
- context% 顏色分級 >70% warning / >90% error（`footer.ts:154-160`）。

### oh-my-pi（pi fork，同構再加強）

- `oh-my-pi/packages/coding-agent/src/modes/components/footer.ts:101-267`；
  context 門檻抽成 `status-line/context-thresholds.ts` 共用模組。

### opencode（狀態拆三處）

- footer 極簡（目錄+權限數）：`opencode/packages/tui/src/routes/session/footer.tsx:52-90`
- token/context/cost 在 sidebar plugin：前端自己算「最後一條 assistant 的 tokens 加總 ÷ model.limit.context」
  （`tui/src/feature-plugins/sidebar/context.tsx:20-34`）——加總法較不準。

### codex（最完整、可設定）

- 工作中上緣：spinner + elapsed + esc to interrupt（`codex-rs/tui/src/status_indicator_widget.rs:44-78`）。
- idle 下緣 footer：模式制渲染 + `N% context left`（`codex-rs/tui/src/bottom_pane/footer.rs:999-1011`），
  寬度不足的降級規則文件化在 `footer.rs:1-43` 模組註解。
- 可設定 status line：`StatusLineItem` 列舉約 25 種項目
  （`codex-rs/tui/src/bottom_pane/status_line_setup.rs:56-155`）。
- context% 扣掉固定底線 `BASELINE_TOKENS=12000` 再算剩餘（`codex-rs/tui/src/token_usage.rs:9,43-53`）。

### claude-code-source（演算法最正確 + 架構最乾淨）

- **context% 演算法**：`calculateContextPercentages`
  （`src/utils/context.ts:118-144`）＝
  `(input_tokens + cache_creation + cache_read) / context_window`，
  只取**最後一條有 usage 的 message**（`src/utils/tokens.ts:138-157` `getCurrentUsage`），
  不做歷史加總、不算 output——因為 API 回傳的 input_tokens 就是當前 context 佔用。
- **資料打包 + 外部渲染**：`StatusLine.tsx:36-127` 把狀態打包成 JSON
  （model / workspace / cost / context_window / rate_limits / vim / worktree）餵給
  使用者自設的 shell command，stdout 即顯示文字。
- **效能紀律**：debounce 300ms（`StatusLine.tsx:230`）、只在
  lastAssistantMessageId / permissionMode / model 變更時重算（`:237-246`）。
- **集中式 cost tracker**：`src/cost-tracker.ts:50-51` 集中累計
  totalCost / totalDuration / totalAPIDuration / linesAdded / linesRemoved。

### 持久化共識

pi/codex 用 JSONL 事件流且 usage 內嵌（pi 內嵌 `message.usage`、codex 有 `TokenCount` event），
resume 後可重算 context%；opencode 用 SQLite，session 層級 tokens/cost 是正規化欄位增量加總
（`core/src/session/projector.ts:62-67`）。rivumi 現行的 `events.jsonl` + `session.json` 已符合此共識。

## rivumi 決策（2026-08-25 實作）

採 claude-code 演算法 + pi 版面，最小實作：

1. 新增 `RuntimeMetrics(Static)` widget（`src/rivumi/tui.py`），掛在 `#status-row`
   的 `#status` 與 `#new-items` 之間，narrow 模式隱藏。
2. 顯示格式：`model · ↑in ↓out · ctx N% · Ns`（turn 結束後才顯示 elapsed）。
3. context% = `telemetry.input_tokens / telemetry.context_window`——
   rivumi 的 `ContextTelemetry.input_tokens` 已含 cached（subset 語義，
   `runtime_semantics.py:39-49` validator 保證），等價於 claude-code 公式。
4. 顏色分級：>70% yellow、>90% bold red（抄 pi/omp 門檻）。
5. 更新時機（對應 claude-code 的效能紀律）：只在
   `ContextUsageUpdatedEvent` / `RuntimeModelUpdatedEvent` / turn 開始結束時重算，
   不逐事件重繪。
6. `_result_status` 追加 `· N tokens`，讓結束摘要行也帶用量紀錄。

## 未做（未來擴充）

- codex 式可設定 `StatusLineItem` 列舉或 claude-code 式外部 command hook
- cost 顯示（rivumi 的 Usage 尚無 cost 欄位）

## 上緣「工作中」狀態設計（2026-08-25 調查）

### 各家對照

| | 擺放位置 | 狀態文字 | elapsed | 即時 token | esc 提示 |
|---|---|---|---|---|---|
| codex | composer 上方固定一行 | `"Working"` shimmer（2s 週期）+ ≤3 行 `└ details` | ✅ `(1m 05s • esc to interrupt)`，審批等待時計時凍結 | ❌（在 footer） | ✅ 同行括號內 |
| claude-code | 訊息流末端 | ~188 個隨機趣味動詞 + glimmer；停滯數秒 glyph 漸變紅色 | ✅ | ✅ `↓N tokens`（chars/4，30 秒後顯示，平滑追趕） | ✅ 在輸入框 footer |
| pi | editor 上方一行 | `"Working..."` + braille spinner 80ms | ❌ | ❌ | retry/compaction 版 `(esc to cancel)` |
| oh-my-pi | HUD 下、editor 上 | `"Working… [esc]"` shimmer + session accent 色 | ❌ | ❌ | ✅ 訊息尾 `[esc]` |
| opencode | 完全內嵌訊息流 | per-tool 文案 `~ Writing command...` 等 | ❌（事後 duration） | ❌ | ❌ |

### 關鍵證據

- codex：刻意壓一行避免垂直抖動（`codex-rs/tui/src/status_indicator_widget.rs:1-5`）、
  審批時 pause/resume 計時（`:159-190`）、單行結構與 compact elapsed 格式（`:238-297`）、
  32ms 幀排程（`:243-247`）
- claude-code：spinner 幀 `['·','✢','✳','✶','✻','*']` 往返、120ms 幀、50ms 共享時鐘
  （`src/components/Spinner/utils.ts:4-6`、`SpinnerAnimationRow.tsx:103,131`）；
  token 以 ref 累計串流字元數避免每 delta 重渲染（`REPL.tsx:1425-1443`）、
  平滑追趕動畫 gap<70 每次 +3（`SpinnerAnimationRow.tsx:141-159`）、
  顯示門檻 `SHOW_TOKENS_AFTER_MS = 30_000`；停滯變紅 `useStalledAnimation`
- oh-my-pi：cadence 分離——shimmer 開 30fps、關閉時只推 spinner 幀不重繪文字
  （`packages/tui/src/components/loader.ts:95-102`）；shimmer palette 以 WeakMap 快取

### rivumi 現況與缺口

已有：otter spinner 幀、glimmer 掃光、phase 文案、16 秒後 `(Ns)` elapsed（對標 pi，接近 claude-code）。

缺口（優先序）：
1. **esc to interrupt 提示**——codex/omp/claude 都有；建議與既有 elapsed 合併成 `(45s · esc to interrupt)`
2. **即時 token 估算**——僅 claude-code 有；可抄 chars/4（rivumi 串流文字已在 `_runtime_stream_text`）
3. 停滯變紅——claude-code 獨有，錦上添花
