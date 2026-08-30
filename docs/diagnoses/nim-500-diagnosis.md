# nvidia-nim 500 診斷報告

日期：2026-08-24 · 狀態：最小修復已實作（見文末〈實作摘要〉）

## TL;DR

根因是 **(c) NIM 伺服器端對 `nvidia/nemotron-3-ultra-550b-a55b` 間歇性回 5xx（500/503 overloaded），而 looplane 的 orchestration 層完全沒有 retry**：`retryable` 只是被記到 event log 的欄位，沒有任何消費者。一次 5xx 就把整個 run 標成 FAILED。模型 ID 有效、payload 相容（已用真實程式路徑重放驗證），排除 (a)、(b)。

## 根因與證據

### 1. 錯誤字串產生點

- `nvidia-nim request failed: Error code: 500 – {...}`
  - `src/looplane/models.py:411-426` — `OpenAICompatibleModel.complete()` 捕捉 openai SDK 的 `APIStatusError`，組出 `f"{self.provider_name} request failed: {exc}"`（`provider_name="nvidia-nim"`，由 `cli.py:950-956` 傳入）。
  - 分類：`models.py:140-149 _error_kind()` — `status_code >= 500` → `ProviderErrorKind.RETRYABLE`。
  - 實際 run artifact 證據：`~/.local/state/python-coding-agent/runs/ce5582a02d0b4c4ab4825020cce292bf/events.jsonl`
    `model.failed {"error": "nvidia-nim request failed: Error code: 500 - {'error': ...}", "kind": "retryable", "provider": "nvidia-nim", "retryable": true}`

- `Run failed` / `Error: provider retryable`
  - `src/looplane/loop.py:1011-1023` — AgentRunner 捕捉 `ProviderError` 後**直接** `_finish(FAILED, terminal_reason=f"provider_{exc.kind.value}")` = `"provider_retryable"`，summary = 原始錯誤字串。
  - TUI 顯示鏈：
    - summary 以 Agent turn 寫出 → 使用者看到原始 500 訊息（`tui.py:3040-3044`）。
    - FAILED → timeline 「Run failed」（`tui.py:3058-3063`）→ `_failure_detail()`（`tui.py:3412-3413`）：`Error: {result.error or result.terminal_reason.replace('_',' ')}`。`_finish()`（`loop.py:662-705`）從不填 `RunResult.error`（artifact `result.json` 中 `"error": null`），所以顯示的是把 terminal_reason 底線換空白後的 **「provider retryable」**——這只是分類名稱，不是給人看的訊息。

### 2. Retry 邏輯：名義上有、實際上沒有

- `models.py:59-61 ProviderError.retryable` property 定義了可 retry 分類；`loop.py:1016` 只把它寫進 `model.failed` event。
- 全 repo grep `retry/attempt`：`src/looplane/` 內**沒有任何**以 `exc.retryable` 為條件的迴圈或 backoff。唯一的重試是 openai SDK 內建 `DEFAULT_MAX_RETRIES = 2`（已驗證安裝版本 openai 3.3.1）。SDK 用盡 2 次內部重試後 raise，looplane 立即放棄整個 run。

### 3. 排除模型 ID / payload 問題（假設 a、b 不成立）

- 模型 ID 有效：`GET https://integrate.api.nvidia.com/v1/models` 回傳清單含 `nvidia/nemotron-3-ultra-550b-a55b`（無需 key 即可列出）。使用者 config（`~/.config/looplane/config.json`）`"model": "nvidia/nemotron-3-ultra-550b-a55b", "provider": "nvidia-nim"`；`cli.py:291-295` 正確拆成 provider=`nvidia-nim` + model=`nemotron-3-ultra-550b-a55b`；base URL 來自 `provider_catalog.py:25` = `https://integrate.api.nvidia.com/v1`。
- Payload 相容：用 looplane 自己的程式路徑（`OpenAICompatibleModel` + `ToolExecutor._tool_definitions()` 共 7 個 tools + `CODING_AGENT_SYSTEM_PROMPT`）打真實 API：
  - curl 純文字 → 200
  - curl 帶 tools+max_tokens → 200（模型正常回覆並輸出 `reasoning_content`）
  - looplane 程式路徑重放 → 曾連續得到 `404 page not found`（gateway 瞬斷）、httpx 直打得到 `503 {"message":"Service temporarily overloaded"}`、之後連續 5 次 200 —— 直接重現「同一 payload 忽好忽壞」的伺服器端不穩定。

## 為何使用者覺得「再也沒有回覆」

- Session **沒有**被標記死掉：失敗走 `except ProviderError` → run FAILED；TUI `finally`（`tui.py:3094-3105`）一定 `_set_running(False)`、focus 回輸入框。artifact 也證明失敗後下一則訊息（21:02「嗨」）有開新 run 且 completed。
- 真正的體感問題：(1) NIM 對此模型當下持續間歇 5xx，每則新訊息都各自開新 run、各自再撞一次 5xx、各自再失敗一次——看起來就是「都不回」；(2) 失敗呈現極差：原始 traceback 式訊息當 Agent 回覆貼出，加上一行無意義的「Error: provider retryable」，且 `retry_after_seconds` 有解析卻沒人用。

## 最小修復建議（未實作）

1. **在 looplane 層加有限次 retry（核心修復）**
   - 位置建議：`AgentRunner.run()` 的模型呼叫處包一層 helper，或 `OpenAICompatibleModel.complete()` 內。
   - 條件：`exc.retryable is True`；最多 3 次、指數 backoff（如 1s/2s/4s），有 `exc.retry_after_seconds` 就取 max(backoff, retry_after)；每次重試發 `model.retry` event。放棄後才走現有的 FAILED 路徑。
2. **修錯誤呈現**
   - `loop.py:_finish()` 增加 `error` 參數，ProviderError 路徑傳入人類可讀訊息（例如「nvidia-nim 連續 3 次回應 500/503（服務暫時不可用），請稍後重試或換模型」）；`_failure_detail()` 即會顯示它而非「provider retryable」。保留 `terminal_reason="provider_retryable"` 作為機器可讀欄位即可。
3. （選配）NIM 免費額度本就不穩，可在 `runtime_registry` / 文件標註，或在連續 provider 失敗時於 status 列提示切換 provider。

## 附帶確認：run failed 後 session 是否死亡？

否。相關路徑：`tui.py:3058-3063`（只寫 timeline）、`tui.py:3094-3105 finally`（恢復輸入、處理 queued prompts）。Ask-mode 的 conversation turn 也有正確收尾（`tui.py:3033-3034` → `_finish_conversation_turn` 記錄 `TURN_FAILED` 並 reset turn id，`tui.py:3212-3249`）。唯一小瑕疵：`tui.py:3009-3016` 已針對「persistent controller 在失敗 turn 後自我關閉」做了清理，但 looplane-agent runtime（`runtime_registry.py:181-186`，`native_session=None`）不走該路徑，不受影響。

## 實作摘要（2026-08-24）

依上述建議完成最小修復；`terminal_reason` 既有值未動、無新依賴。

### 改動檔案

- `src/looplane/loop.py`
  - 新增模組常數 `MODEL_ATTEMPTS = 3`、`RETRY_BACKOFF_SECONDS = (1.0, 2.0, 4.0)`。
  - `AgentRunner.__init__` 新增可注入的 `self.model_retry_backoff`（測試可覆寫為 0）。
  - 新增 `_complete_model_with_retry(deadline)`：每個模型 step 最多 3 次嘗試；僅對 `exc.retryable`（RETRYABLE / RATE_LIMIT）重試，AUTH / INVALID_REQUEST / PROVIDER 直接 raise。每次重試前發 `model.retry` event（帶 `attempt`、`provider`、`error`、`delay_seconds`）；delay = `max(backoff[attempt-1], exc.retry_after_seconds or 0)`。step 開始時 reset `_provider_failure_codes`，記錄每次 retryable 失敗的 status code。
  - 新增 `_backoff_sleep(delay)`：以 `asyncio.wait` 等 backoff，使用者取消會提前醒來，下一次嘗試立即觀察到取消訊號（run 走原本的 CANCELLED 路徑）。
  - run loop 的模型呼叫點改為 `_complete_model_with_retry(deadline)`（原 `_complete_model_or_cancel(remaining)`；取消/逾時語意不變）。
  - `_finish()` 新增 `error: str | None = None` 參數並傳入 `RunResult.error`（contracts 本來就有此欄位與驗證）。
  - `except ProviderError`：retryable 錯誤在重試用盡後組出人類可讀訊息（「nvidia-nim failed 3 consecutive model requests (500, 503, 500); the service is temporarily unavailable. Retry shortly or switch to another provider/model.」）傳給 `_finish(error=...)`；non-retryable 維持 `error=None`（TUI fallback 回 terminal_reason，行為不變）。`terminal_reason=f"provider_{kind}"` 完全保留。
  - TUI 呈現鏈自動生效：`tui.py:_failure_detail()` 現在顯示人類可讀訊息而非「Error: provider retryable」（已用真實 RunResult 驗證輸出）。
- `tests/test_loop_e2e.py`
  - `test_retryable_provider_errors_are_retried_until_success`：(a) 兩次 500/503 後成功 → COMPLETED、呼叫 3 次、兩個 `model.retry` events（attempt 1/2、provider、delay_seconds）。
  - `test_exhausted_retryable_provider_errors_fail_with_readable_error`：(b) 三連失敗 → FAILED、`terminal_reason="provider_retryable"`、`result.error` 含 provider、次數與狀態碼、events 含 attempt [1,2] 與 `model.failed(retryable=True)`。
  - `test_non_retryable_provider_errors_fail_without_retrying`：(c) 401 AUTH → 只呼叫 1 次、無 `model.retry`、FAILED、`terminal_reason="provider_auth"`、`error is None`。

### 驗證

- `uv run pytest tests/test_loop_e2e.py` — 19 passed
- `uv run pytest`（全套件）— 全數通過
- `uv run ruff check src/looplane/loop.py tests/test_loop_e2e.py` — All checks passed
  （`ruff format --check` 對本 repo 既有未格式化行本就會報差異，非本次改動引入，未動那些行。）
