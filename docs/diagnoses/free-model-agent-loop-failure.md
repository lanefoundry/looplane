# 免費模型 Agent Loop 失敗診斷

## 日期

2026-08-30

## 症狀

使用 `minimax/minimax-m3:free`（OpenRouter 免費模型）執行任務時：

- Agent loop 耗盡 12 步上限，報錯 `Error: max steps exceeded`
- `No file changes were reported before failure`
- 消耗 144.6k tokens，耗時 153 秒
- 模型持續呼叫 read-only 工具（list_files、read_file、search_text）探索，但從未呼叫任何寫入工具（replace_text / apply_patch）

而同一個免費模型在 OMP、Pi、OpenCode 等其他 coding agent 上可以正常完成任務。

## 根因分析

與 OMP / Pi / OpenCode / Codex / Claude Code 五個 reference agent（`/Users/xiaoxu/Projects/coding-agent-reference`）比較後，找到三個 P0 根本差異：

### P0-1：缺少 In-band Dialect 系統（最關鍵）

**問題：** Looplane 只透過 OpenAI `chat.completions.create` 的原生 `tools` 參數進行 function calling。`minimax-m3:free` 等免費模型不支援或極差支援原生 function calling，導致 tool call 品質極低，模型不知道如何正確呼叫寫入工具。

**OMP 的做法：** OMP 有 11 種 model-specific dialect（anthropic、minimax、deepseek、harmony、qwen3、gemini、gemma、glm、hermes、kimi、xml）。對於 `supportsTools === false` 的模型，自動切換到 in-band dialect——在 system prompt 裡教模型用 XML 格式輸出 tool call，然後由 `InbandStreamProjector` + `MinimaxInbandScanner` 從 text output 解析出 tool call。

**相關檔案（OMP）：**

- `packages/ai/src/dialect/minimax.md` — minimax dialect 的 XML 格式定義
- `packages/ai/src/dialect/minimax.ts` — minimax 專用解析器
- `packages/ai/src/dialect/factory.ts` — dialect 工廠
- `packages/catalog/src/identity/dialect.ts` — 模型→dialect 映射
- `packages/coding-agent/src/sdk.ts` — `resolveDialect()` 自動偵測

**影響：** 這是「只探索不行動」的根本原因。模型可能根本不知道怎麼正確呼叫寫入工具。

### P0-2：硬性 12 步上限 + 暴力終止

**問題：** Looplane 在 `loop.py:2181` 用 `while self._step < max_steps` 控制迴圈，到 12 步直接 `FAILED: max_steps_exceeded`，不給模型收尾機會。

**其他 agent 的做法：**

| Agent | Step Limit | 到達上限時的行為 |
|-------|-----------|--------------|
| OMP | 無上限 | 只靠 wall-time deadline |
| Pi | 無上限 | 同上 |
| Codex | 無上限 | token-budget 控制 |
| OpenCode | 可配置 | 優雅收尾：注入 MAX_STEPS_PROMPT + 設 toolChoice:'none' 強制文字回覆 |
| Claude Code | optional maxTurns | 類似 |
| **Looplane** | **硬性 12 步** | **直接 FAILED** |

**相關檔案：**

- `src/looplane/contracts.py:49` — `max_steps: int = Field(default=12, ge=1)`
- `src/looplane/loop.py:2181` — main loop guard
- `src/looplane/loop.py:2486-2489` — 暴力終止邏輯

### P0-3：缺少 Empty/Unexpected Stop Recovery

**問題：** Looplane 只有 fingerprint 比對（`loop.py:895-911`，連續 3 次完全相同的 tool call SHA-256 才觸發 `repeated_action`）。模型讀不同檔案的「多樣化但無用探索」完全不會被偵測到。

**OMP 的三層恢復機制：**

1. `handleEmptyAssistantStop` — 模型空回覆時自動重試 + 注入提示
2. `handleUnexpectedAssistantStop` — 用小型分類器（LFM2-350M / Qwen 0.5B）判斷停止是否有意，無意就重試
3. Stream stall detection — regex 偵測生成卡住

**相關檔案（OMP）：**

- `packages/coding-agent/src/session/turn-recovery.ts`
- `packages/coding-agent/src/session/unexpected-stop-classifier.ts`

## 次要差異

| 維度 | Reference Agents | Looplane | 優先級 |
|------|-----------------|----------|--------|
| Streaming | 全部 5 個都 stream | 等完整 response（`models.py:579-590`） | P1 |
| System prompt | OpenCode 有 6+ 模型變體 | 所有模型同一份 prompt | P2 |
| Context 壓縮 | OMP 5+ 策略 + 預測性壓縮 | 85% 觸發一次性壓縮 | P2 |
| Conciseness | OpenCode:「4 行以內，一個字最好」 | 「Be concise」 | P2 |
| Per-request timeout | 各有設定 | 繼承 SDK 預設 600s（`models.py:579`） | P1 |

## 建議修法（優先順序）

1. **In-band tool calling dialect** — 最高 ROI，直接解決免費/弱模型的 function calling 問題
2. **Graceful step limit** — 到上限不暴力中斷，改注入 "tools disabled" 讓模型收尾
3. **Stall guard** — 連續 N 步只讀不寫就注入 nudge 或提前結束
4. **Streaming** — 改用 streaming API，改善延遲感知 + 可偵測 mid-generation stall
5. **Per-request timeout** — 對 OpenAICompatibleModel 加 60-120s timeout

## 狀態

- [x] 診斷完成
- [x] P0-1 In-band dialect 實作 — `src/looplane/dialect.py` + `models.py` / `cli.py` 整合
- [x] P0-2 Graceful step limit — wind-down summary call at max_steps
- [x] P0-3 Stall guard / recovery — read-only stall nudge after 4 consecutive read-only steps
- [ ] 實際用免費模型 smoke test 驗證
