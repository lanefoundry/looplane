# Rivumi Session Format

Rivumi 的每個 run 都持久化到 `runs/<run_id>/`，格式設計目標：**crash-safe、
可 resume、可被第三方工具解析**（對標 pi 的 session JSONL 與 codex 的 rollout 格式，
見 `docs/research/2026-08-25-agent-usage-packages.md`）。

## 目錄結構

```
runs/<run_id>/
├── request.json        # 任務契約（instruction、mode、runtime、provider、model）
├── events.jsonl        # 事件審計流（append-only，一行一 JSON）
├── session.json        # SessionManifest（可 resume 的完整狀態，schema_version 1）
├── checkpoint.json     # Checkpoint（狀態變更步驟後的最小可恢復快照）
├── result.json         # RunResult（終態：status、summary、changed_files、usage）
├── changes.patch       # 最終 patch
├── verification.json   # 驗證結果
├── test.log            # 檢查輸出
└── workspace/          # 隔離的 git workspace（committed-HEAD pinned）
```

## events.jsonl

一行一個 `RunEvent`（`src/rivumi/events.py:19-30`），欄位固定、`extra="forbid"`：

```json
{
  "event_type": "run.created",
  "run_id": "d767509d14594541b8224ac9e9450321",
  "task_id": "tiny-python-bug-demo",
  "sequence": 0,
  "data": {
    "base_sha": "2136c937…",
    "model": "scripted",
    "prompt_version": "m3-exact-edit-v1",
    "provider": "scripted"
  },
  "event_id": "7070d7026b00464c87d2ae6f2958f157",
  "created_at": "2026-08-22T09:43:37.548880Z"
}
```

- `sequence` 單調遞增，從 0 開始（`loop.py:245-269`）
- `event_type` 命名：`run.created`、`run.<status>`（completed/failed/cancelled）、
  `model.requested`、`workspace.prepared`、`run.error`
- 首行 `run.created` 的 `data` 固定含 `provider`、`model`、`prompt_version`、`base_sha`——
  第三方 parser 應以此行識別 run 的 provider 歸屬

### 長壽對話（ask 模式）

互動對話另存於 `$XDG_STATE_HOME/rivumi/conversations/<id>/events.jsonl`
（`src/rivumi/conversation.py:312-314`），同為 append-only JSONL，`/resume` 由此重放。

## session.json — SessionManifest

`src/rivumi/session.py:72-103`，`schema_version: 1`：

| 欄位 | 說明 |
|------|------|
| `run_id` / `task_id` | 識別 |
| `provider_name` / `model_id` / `protocol` | provider 歸屬 |
| `prompt_version` | system prompt 版本（如 `m3-exact-edit-v3`） |
| `base_sha` | 40 hex，工作區基準 commit |
| `phase` / `step` / `terminal` | 生命週期狀態 |
| `messages` | 完整對話（`ConversationItem` tuple） |
| `usage` | 累計 token（見下） |
| `approval_history` | 審批稽核（request + decision + 時間） |
| `active_wall_time_seconds` | 累計活躍牆鐘時間 |
| `last_event_sequence` | 對齊 events.jsonl 的最後 sequence |
| `active_writer_token` | 單寫者 fencing token |

寫入策略：每次事件後 atomic rewrite（`_save_manifest`），配合 writer token 防多進程寫入。

## usage 物件（三處共用）

`src/rivumi/contracts.py:152-167`：

```json
{
  "input_tokens": 0,
  "output_tokens": 0,
  "cached_input_tokens": 0,
  "reasoning_tokens": 0,
  "provider_total_tokens": null
}
```

語義：**cached ⊆ input、reasoning ⊆ output**；`provider_total_tokens` 為 provider
自報總量（可為 null）。context% 的正確算法是
`input_tokens / context_window`（input 已含 cached；對標 claude-code，
見 `docs/research/2026-08-25-status-line-state-display.md`）。

## checkpoint.json — Checkpoint

`src/rivumi/contracts.py:232-246`：`run_id`、`task_id`、`status`、`step`、`messages`、
`tool_call_count`、`usage`、`active_writer_token`、`last_action_fingerprint`、`metadata`。
每個狀態變更步驟後原子重寫（`loop.py:486-501`）。

## result.json — RunResult

`src/rivumi/contracts.py:248-268`：`status`（completed/failed/cancelled）、`summary`、
`changed_files`、`verification`、`usage`、`terminal_reason`、`error`、`artifacts`。

## 給生態系工具的指引

- 解析 usage 統計：讀 `result.json.usage`（終態）或 `session.json.usage`（含進行中）
- 歸屬 provider/model：讀 `events.jsonl` 首行 `data.provider` / `data.model`
- 事件流重放：按 `sequence` 排序，`run.<status>` 為終止事件
- schema 變更會 bump `schema_version`；v1 欄位只增不減
