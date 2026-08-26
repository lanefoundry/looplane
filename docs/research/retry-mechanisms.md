# Coding Agent LLM API Retry 機制比較

> 日期：2026-08-26　範圍：Claude Code、pi/omp、opencode、codex、rivumi 現況
> 證據路徑前綴：CC = `claude-code-source/src/`、oc = `opencode/packages/`、cx = `codex/codex-rs/`

## 一、總覽表

| | 分類方式 | 次數上限 | Backoff | Retry-After |
|---|---|---|---|---|
| **CC** | status 白名單 + 訊息字串（`shouldRetry`） | 10（env 可調）；529 連 3 次觸發 model fallback | `min(500×2^(n-1), 32s) + 0~25% jitter` | **尊重且 bypass maxDelay**；persistent 模式另讀 `anthropic-ratelimit-unified-reset` |
| **pi** | provider 層 status（408/409/429/5xx）+ agent 層訊息 regex；`x-should-retry` header | provider 層預設 0（交外層）；agent 層 3 | provider：`min(0.5×2^n, 8s)×(1−rand×0.25)`；agent：純指數 2s 起**無 jitter** | provider 層尊重（`retry-after-ms` → `retry-after`），>60s 直接失敗 |
| **omp** | 標準化 `AIError` 分類（`isTransientStatus`：408/429/5xx） | transport 5~6；turn 層 **10**；oneshot 3 | transport `500ms×2^n` cap 60s；turn 層 cap 8s + 75–100% jitter；Copilot 特例固定 400ms×8 | 最全面：5 種 header + **從錯誤 body 文字萃取**（"retry in 250ms"、"try again in ~158 min"）；hint 視為權威但超過 maxDelay 直接失敗 |
| **opencode** | session 層訊息 regex + provider 層 reason tag；OpenAI 404 也算 retryable | session 5；provider executor 2；AI SDK 歸 0（防三層疊加） | session：`2s×2^(n-1)` + 0~25% jitter，無 header 時 cap 30s；provider：500ms±20% cap 10s | 尊重三格式（ms/秒/HTTP-date）；無 header 時才套 30s cap |
| **codex** | 二層：transport `RetryOn{retry_429,retry_5xx,retry_transport}` + 語意層 `CodexErr::is_retryable()` | request 4 / stream 5（可設定，硬上限 100） | `200ms×2^(n-1)` ±10% jitter；無 max delay（除無限重試路徑 cap 60s） | **HTTP header 不尊重**（原始碼留 TODO）；但解析錯誤訊息文字 "try again in Ns" 當作 delay |
| **rivumi** | `ProviderErrorKind`（RETRYABLE/RATE_LIMIT/AUTH/INVALID_REQUEST） | 3 | 固定表 `(1s, 2s, 4s)` 純指數**無 jitter** | 尊重 `retry-after` header（`models.py:172`） |

## 二、Retry 判斷的共識邊界

**會重試**（五家一致）：429、5xx、529/overloaded、network/connection error、timeout、stream 提早斷線。

**絕不重試**（五家一致）：
1. **Billing/quota 耗盡**——CC 歸 `billing_error`、pi 黑名單 `insufficient_quota`、oc `QuotaExceededReason`、cx `UsageLimitReached`（含 `resets_at` 直接終止 turn）
2. **Context overflow**——一律不 retry，交給 compaction 處理（pi `agent-session.ts:2770-2774`、cx `ContextWindowExceeded`、oc `ContextOverflowError`、CC prompt-too-long 歸 invalid_request）
3. **Invalid request / auth**（400/401/403）——例外：CC 對 401 清 key cache 後重試、403 OAuth revoked 強制 refresh 後重試（`withRetry.ts:773-781`）
4. **使用者 abort**——永遠不重試
5. **Content filter**——omp 明確列入（`oneshot-retry.ts:104`）

**微妙案例**：
- CC：429 對訂閱戶不重試、Enterprise/PAYG 重試（`withRetry.ts:767-769`）；max_tokens 溢位的 400 會解析後**調降 max_tokens 重試**（`withRetry.ts:384-427`）
- cx：**429 在 transport 層刻意關閉**（`retry_429: false`），改由語意層處理——usage-limit 類 429 直接終止，真正的 rate limit 走 stream retry
- oc：OpenAI 回 404 有時是模型實際可用，視為 retryable（`provider/error.ts:23-28`）
- omp：LiteLLM concurrency 429 故意不在 transport 重試，上拋給 turn 層做 model fallback（`openai-http.ts:39-62`）
- CC：背景任務（摘要/標題/classifier）遇到 529 直接放棄，避免放大雪崩（`withRetry.ts:57-89`）

## 三、分層架構：內建 retry 一律歸零

所有多層專案都**刻意把 SDK 內建 retry 關成 0**，避免多層指數疊加：

| 專案 | 分層 | 最壞情況 |
|---|---|---|
| oc | AI SDK(0) × provider executor(1+2) × session(1+5) | **18 次 HTTP 嘗試** |
| pi | provider SDK(0，改自家 helper) × agent turn(1+3) | 4 次模型請求 |
| cx | transport request(1+4) × stream sampling(1+5) | 兩層獨立遙測（`layer="http"` vs `layer="stream"`） |
| omp | transport(6) × auth-retry(≤64) × turn-recovery(10) | 另有 credential/model fallback 拿全新 budget |

## 四、Streaming 中途失敗：replay 安全光譜

重試「已串出一半的回應」有雙重執行風險，各家成熟度不同：

1. **pi（最簡單）**：整回合重來，partial text 已進 UI 也照發——沒有 replay 檢查（`agent-session.ts:2811-2863`）
2. **opencode**：整個 turn 從頭重發（`session/processor.ts:640-674`）；mid-stream 錯誤明確標 retryable（ECONNRESET/ZlibError/WS 1006，`message-v2.ts:606-734`）
3. **CC**：streaming 失敗 → non-streaming fallback 帶完整 withRetry；**但明文註解承認 partial stream 已開始的 tool 可能被重跑一次**（inc-4258，`claude.ts:2464-2502`），提供 `CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK` 逃生口
4. **omp（最嚴謹）**：buffer-and-replay——只緩衝第一個事件，出現 replay-unsafe 事件就停止緩衝；**replay-veto**：已 commit 非 whitespace 文字/image/tool call 即拒絕重放，thinking-only/空白視為安全可丟（`turn-recovery.ts:1096-1104`、`stream.ts:1472-1528`）

## 五、UI 事件

- **CC**：`Retrying in {N} seconds… (attempt {X}/{Y})` 每秒 tick（`SystemAPIErrorMessage.tsx:106`）；SDK/headless 吐 `api_retry` 事件含 attempt/max_retries/retry_delay_ms/error_status（`QueryEngine.ts:943-954`）
- **pi/omp**：`auto_retry_start/end` session 事件 → TUI「Retrying (N/M) in Xs… (esc to cancel)」，Esc 可取消 retry（`status-indicator.ts:47`、`event-controller.ts:1998-2005`）
- **oc**：status event `{type:"retry", attempt, message, next}` → `[retrying in 5s attempt #3]` 前端 1 秒 interval 倒數（`tui/prompt/index.tsx:1548-1573`）——截圖中 rivumi 顯示的格式即此家族
- **cx**：`"Reconnecting... {n}/{max}"` 通知 UI（`responses_retry.rs`）

## 六、可設定性

| 專案 | 可調 | 寫死 |
|---|---|---|
| CC | `CLAUDE_CODE_MAX_RETRIES`、`API_TIMEOUT_MS` | base 500ms、jitter、32s cap |
| cx | per-provider `request_max_retries`/`stream_max_retries`/`stream_idle_timeout_ms`（config.toml） | base 200ms、jitter ±10% |
| pi | settings `retry.{enabled,maxRetries,baseDelayMs}` + per-provider `timeoutMs/maxRetries/maxRetryDelayMs` | — |
| omp | 最細：`retry.modelFallback`、`fallbackChains`、`usageAwareFallback` 等 | jitter 公式 |
| oc | 無使用者設定 | 全部 |

## 七、值得抄進 rivumi 的清單（依 CP 值排序）

rivumi 現況（`loop.py:694-728`、`models.py`）：3 次、固定表 (1,2,4)s、尊重 retry-after、`model.retry` 事件已有、取消可中斷等待——骨架正確，缺：

1. **Jitter**（所有家都有）：`(1,2,4)` 改 `base×2^n × U(0.9,1.1)` 或加 0~25% 正向 jitter——多 client 同打 provider 時避免同步震盪
2. **5xx/overloaded 訊息字串分類**：rivumi 目前靠 status + kind，可補 `overloaded`/`server_is_overloaded` body 關鍵字（參考 oc `retry.ts:33-41` 白名單）
3. **`x-should-retry` header**：pi/CC 都尊重，一行成本
4. **Context overflow 不重試的明確分類**：確認 `INVALID_REQUEST` 涵蓋 prompt-too-long 並直接 fail（交給未來 compaction）
5. **連續 N 次 529/overloaded → model fallback**（CC `MAX_529_RETRIES=3`）：rivumi 多 provider 目錄是現成 fallback 來源
6. **次數上限提高到 5~10 + max delay cap 30~32s**：3 次 × 4s 上限在 upstream rate limit 場景偏短（截圖場景就是這個）
7. 若未來加 streaming：**直接採 omp 的 replay-veto 語義**（tool call 已發出就不重放），別走 CC 已認列 bug 的路

## 證據索引

- CC：`services/api/withRetry.ts`（shouldRetry L696-787、getRetryDelay L530-548、529 fallback L335-363）、`services/api/claude.ts`（streaming fallback L2504-2569）
- pi：`packages/ai/src/utils/provider-retry.ts`（L22-67）、`utils/retry.ts`（白名單 L26-90、黑名單 L7-24）、`coding-agent/src/core/agent-session.ts`（L2770-2863）
- omp：`packages/ai/src/error/retryable.ts`、`utils/fetch-retry.ts`（L350-388）、`utils/retry-after.ts`、`coding-agent/src/session/turn-recovery.ts`（replay-veto L1096-1104）、`docs/non-compaction-retry-policy.md`
- oc：`opencode/src/session/retry.ts`（L26-98,193）、`llm/src/route/executor.ts`（L36-38,91-148）、`session/processor.ts`（L640-674）
- cx：`codex-client/src/retry.rs`（L22-48,80）、`core/src/util.rs`（L86-91）、`core/src/responses_retry.rs`、`model-provider-info/src/lib.rs`（L27-35,313-319）、`core/tests/suite/retry_after.rs`（Retry-After TODO L236-239）
