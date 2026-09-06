# Looplane 記憶機制缺口分析

基於 Claude Code、Oh My Pi、Codex、OpenCode 四個參考專案原始碼的 code-level 對比。

## 截圖診斷：從 Run Failed 看到的兩層問題

**Error: max steps exceeded** — Agent 在 12 步內讀了無關檔案（file.txt、README.zh-TW.md）、碰到 `PathPolicyError: absolute paths are forbidden` 然後卡住，最終 0 changed files、30.6k tokens 白費。

這不只是「步數不夠」，而是兩個結構性問題的疊加：

### 1. 跨 session 零記憶 — 每次從頭開始

Agent 不認識這個 repo，不知道上次做了什麼、哪些檔案重要。唯一的跨 session 機制是手動 `/remember`（`memory.py`），但 JSONL 檔從未被建立過——`context.py:201` 的 `render_known_context()` 永遠回傳空字串。

### 2. 壓縮摘要即用即丟 — 學到的東西不持久

`runner.py:868` 的 `history_summary_fallback` 產生摘要後只留在 session 內。Session 結束，摘要消失，下次重來。

---

## 其他 Coding Agent 怎麼做記憶

### Claude Code

兩套獨立機制，一套管 session 內，一套管跨 session：

#### SessionMemory（session 內背景萃取）

- 註冊為 `postSamplingHook`，當 token 數和 tool call 數超過門檻時觸發
- Spawn 一個 **forked subagent**（共享 prompt cache），萃取 8 個結構化區塊（Current State、Files、Errors、Learnings…），各 2000 token 上限
- 寫到 session memory markdown

**Source:** `src/services/SessionMemory/sessionMemory.ts:168-325`

#### extractMemories（跨 session 持久記憶）

- 每次 query loop 結束時觸發 `runForkedAgent`
- 掃描既有記憶（`scanMemoryFiles`）避免重複
- 寫 YAML frontmatter + Markdown 到 `~/.claude/projects/<key>/memory/`
- 最多 5 turns、用 cursor（`lastMemoryMessageUuid`）追蹤「上次處理到哪」

**Source:** `src/services/extractMemories/extractMemories.ts:121-329`

#### findRelevantMemories（session 開始召回）

- 掃描記憶目錄的 frontmatter（description, type, mtime），上限 200 檔案
- 用 **Sonnet sideQuery** 根據用戶問題選出最多 5 條相關記憶，全文注入 context

**Source:** `src/memdir/findRelevantMemories.ts:39-75`

#### Compaction 用 SessionMemory 替代 LLM 摘要

壓縮時如果已有 session memory，直接用它替代重新生成的摘要，節省一次 LLM 呼叫。

**Source:** `src/services/compact/sessionMemoryCompact.ts:437-503`

---

### Oh My Pi

最完整的記憶架構，兩階段 pipeline + 四種召回通道。

#### Phase 1 — 逐 session 萃取（背景啟動）

- Session 開始時掃描過去的 JSONL 對話檔
- 用 SQLite lease 認領未處理的 session，Low reasoning effort 萃取 `raw_memory` + `rollout_summary`
- 最多 8 concurrent，跳過 <12h 和 >30d 的 session

**Source:** `packages/coding-agent/src/memories/index.ts:345-378`

#### Phase 2 — 全域彙整

- 把所有 per-session 萃取交給 consolidation agent
- 產出 `MEMORY.md`（可搜尋索引）、`memory_summary.md`（精簡全域摘要）、`skills/` playbook
- 用 heartbeat lock 防止重複執行

**Source:** `packages/coding-agent/src/memories/index.ts:476+`

#### Mnemopi 後端 — SQLite + Embedding + FTS

- 四聲道召回：vector search、graph traversal、fact-table lookup、temporal proximity
- 用 reciprocal rank fusion 合併結果
- Agent 有 `recall`、`retain`、`reflect`、`memory_edit` 四個工具主動操作記憶

**Source:** `docs/mnemosyne-memory-backend.md`

#### 注入預算控制

`memory_summary.md` 截斷到 `summaryInjectionTokenLimit`（預設 5000 tokens），避免記憶塞爆 context。

**Source:** `packages/coding-agent/src/memories/index.ts:277`

---

### Codex

同樣有兩階段 pipeline，且帶有 citation protocol：

```
// Phase 1: per-rollout extraction (Rust)
memories/write/src/phase1.rs:66   — claim jobs, send to model (effort: Low)
memories/write/src/phase2.rs:47   — git-backed workspace, consolidation agent (effort: Medium)

// Citation protocol: traceable provenance
codex-rs/protocol/src/memory_citation.rs:6-43
format: path:line_start-line_end|note=[...]

// External import: reads Claude Code's memdir layout
external-agent-migration/src/memory_import.rs:53-90
```

---

### OpenCode

沒有跨 session 記憶。SQLite + Drizzle ORM 儲存 session 歷史，resume 從最近的 compaction row 載入。靜態 `AGENTS.md`（類似 CLAUDE.md）是唯一的持久 context。

---

## 對比矩陣

| 機制 | Claude Code | Oh My Pi | Codex | Looplane |
|---|---|---|---|---|
| 自動萃取記憶 | forked agent | 2-phase pipeline | Rust pipeline | **缺** |
| 跨 session 持久儲存 | Markdown + YAML frontmatter | SQLite + embedding + Markdown | Git-backed Markdown | JSONL（手動 /remember，未使用） |
| Session 開始召回 | Sonnet sideQuery | vector + FTS | MEMORY.md inject | **空** |
| Agent 主動操作記憶 | 無 | 4 tools (recall/retain/reflect/edit) | 無 | **無** |
| Compaction 摘要持久化 | SessionMemory 替代 | Phase 1 擷取 | Phase 1 擷取 | **即用即丟** |
| 注入預算控制 | 最多 5 條記憶 | 5000 tokens 上限 | MEMORY.md 固定長度 | 20 條 JSONL |
| Cursor / 增量處理 | lastMemoryMessageUuid | SQLite lease | rollout claim | **無** |

---

## 改進路線

### P0 — Session 結束自動萃取 → 持久記憶

參考 Claude Code 的 `extractMemories`：在 `runner._finish()` 觸發一個輕量 prompt，從對話中萃取關鍵事實，寫到 `~/.looplane/memory/` 下的 Markdown 檔。下次 session 由 `context.py` 載入注入 system prompt。不需要 embedding，不需要 SQLite，用現有 JSONL 或升級為 Markdown 即可。

**改動範圍:** `runner.py` (_finish) · `memory.py` · `context.py` (build_initial_messages)

### P1 — Compaction 摘要持久化

參考 Claude Code 的 SessionMemory：`history_summary_fallback` 產生的摘要不只留在 messages 陣列裡，同時寫到 session 對應的摘要檔。下次 resume 或新 session 可參考。

**改動範圍:** `runner.py` (_maybe_apply_history_summary_fallback) · `conversation.py`

### P2 — Agent 記憶工具（retain / recall）

參考 Oh My Pi 的 mnemopi：給 agent `save_memory` 和 `recall_memory` tool，讓它在對話中主動存取記憶，而不是只靠用戶手動 `/remember`。

**改動範圍:** `tooling/definitions.py` · `memory.py` · `agent/memory_dispatch.py` (new)

### P3 — 語意召回（取代全量注入）

參考 Claude Code 的 `findRelevantMemories`：當記憶超過 20 條，用一個 sideQuery 根據當前任務選出最相關的 5 條注入，而不是全部塞進 system prompt。

**改動範圍:** `context.py` (build_initial_messages) · `memory.py`
