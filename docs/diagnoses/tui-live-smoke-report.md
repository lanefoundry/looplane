# TUI Live Smoke Report — nvidia-nim provider

日期：2026-08-24　結論：**通過**

## 做法

採用建議做法 1：Textual `App.run_test()` pilot 以程式驅動**真實的 `RivumiApp`**（`src/rivumi/tui.py`），在輸入框輸入「嗨」並按 Enter 送出。`make_runner` 完整比照 `cli.py chat` 的 rivumi-agent 路徑（`_credential_hint` 檢查 native credentials → `_model_from_env` 建構 `OpenAICompatibleModel` → 真實 `AgentRunner`），API key 取自 `~/.local/state/rivumi/auth/native-nvidia-nim.json`，base URL 用 provider_catalog 內建之 `https://integrate.api.nvidia.com/v1`。

註：`~/.config/rivumi/config.json` 目前實際是 `provider=anthropic`，因此腳本以 `CliConfig(provider="nvidia-nim", model="nvidia/nemotron-3-ultra-550b-a55b")` 覆寫（等同 CLI 帶 `--provider nvidia-nim --model …`）。未動 src/ 任何程式碼。

一次性腳本保留在 `.work/`：
- `tui_nim_smoke.py` — 真 TUI 對真 NIM 送「嗨」
- `tui_nim_retry_smoke.py` — 同一 TUI 路徑，注入 flaky httpx MockTransport（可控 503 次數）驗證 retry 與失敗路徑

## Run 1：真實 NIM 對話（TUI 本體）

Run dir：`~/.local/state/python-coding-agent/runs/c6e7dc04a26e434fad9d4ff21ce5d541/`

events.jsonl 的 model.* / 關鍵事件序列（共 4 步）：

```
run.created → workspace.prepared
model.requested(step=1) → model.completed(tool_calls=[list_files])
  tool.requested/approval.requested/approval.resolved(allow_once)/tool.started/tool.completed(ok)
model.requested(step=2) → model.completed(tool_calls=[run_check])   ← pilot 自動按「1」核准
model.requested(step=3) → model.completed(tool_calls=[git_diff])
model.requested(step=4) → model.completed(content="Hello! The repository is clean…")
verification.started → verification.completed(ok) → run.completed(terminal_reason=verified)
```

result.json：`status=completed`、`terminal_reason=verified`、`error=null`、summary 為模型自然語言回覆、usage 共 18,832 tokens。

無 `model.retry` —— 本次 NIM endpoint 沒有回 5xx/429（免費 endpoint 間歇性過載，沒踩到）。

## Run 2：retry 生效證明（注入 2 次 503）

Run dir：`…/runs/b4dd4ac864fc496c9db43b6521ebb1ff/`
transport 實際收到 **3** 個上游請求（503、503、200），events.jsonl：

```
model.requested(step=1)
model.retry  attempt=1  delay_seconds=1.0  provider=nvidia-nim
             error="nvidia-nim request failed: Error code: 503 - …simulated NIM overload"
model.retry  attempt=2  delay_seconds=2.0  provider=nvidia-nim
model.completed(step=1, content="RETRY_SMOKE_OK：我在兩次 503 後成功回覆了。")
run.completed(terminal_reason=verified)
```

→ `_complete_model_with_retry()` 確實重試最多 3 次、指數 backoff（1s→2s）、發出 `model.retry`，第三次成功後 run 正常完成。

觀察附記：`AsyncOpenAI` SDK 預設 `max_retries=2` 會先在 SDK 層把 503 吃掉（ProviderError 根本不會拋到 AgentRunner）；注入測試需 `max_retries=0` 才測得到 harness 層 retry。若希望 harness 層的 backoff/`model.retry` event 對真 NIM 生效，SDK 層重試等於讓總重試次數變成最多 3×3——這點值得後續評估是否在 `OpenAICompatibleModel` 統一設 `max_retries=0`。

## Run 3：3 次全失敗時的錯誤訊息

Run dir：`…/runs/0e9c986d82114bd2888633e7ff4e7166/`（SMOKE_FAIL_LIMIT=99，永遠回 503）

result.json：

```json
"status": "failed",
"error": "nvidia-nim failed 3 consecutive model requests (503, 503, 503); the service is temporarily unavailable. Retry shortly or switch to another provider/model."
```

→ 錯誤為人類可讀訊息（含狀態碼序列與建議），並非裸 "provider retryable"；`provider_retryable` 只作為內部 `terminal_reason`。

## TUI 畫面文字（由截圖 SVG `.work/tui_nim_smoke.svg` 轉錄，Run 1）

```
Rivumi · nvidia-nim · nvidia/nemotron-3-ultra-550b-a55b · rivumi
──────────────────────────────────────────────────────────────
█嗨
▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔
▶ Explored 1 item
✓ Run check-1   {"ok":true,"exit_code":0,…}
✓ Review changes  Permission granted
✓ Check check-1  Passed · exit 0
● Hello! The repository is clean with no uncommitted changes, and the
  verification check passes. Is there something specific you'd like me to
  help with in this codebase?
completed · verified · 0 changed file(s)
──────────────────────────────────────────────
Enter send · Shift+Enter newline · / commands · Ctrl+L model
```

## 驗收對照

| 標準 | 結果 |
|---|---|
| TUI 對 nvidia-nim 送訊並收到模型回覆 | ✅ Run 1（pilot 驅動真 RivumiApp，畫面見上） |
| events.jsonl 列出 model.* 序列 | ✅ Run 1 全序列如上 |
| model.retry 證明 retry 生效 | ✅ Run 2（2 次 retry + backoff 後成功） |
| 失敗時 result.json error 人類可讀 | ✅ Run 3 |

**結論：通過。**
