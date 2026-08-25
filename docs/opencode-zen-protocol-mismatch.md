# OpenCode Zen 協定不匹配事件分析：同一顆模型，為什麼 omp 能用而 pi 500？

> 日期：2026-08-24
> 類型：技術調查 / post-mortem
> 狀態：**已解決** — rivumi 已內建 `ResponsesModel` adapter（見 §8）

## TL;DR

- `muse-spark-1.2-contributor-free` 是 Zen 上**唯一**走 OpenAI **Responses API** 的免費模型
- 它的 `/chat/completions` 轉發層壞了（回 `500`），但 `/responses` 正常
- omp / opencode 的內建 catalog 正確標記此模型為 `openai-responses`，走對端點 → 能用
- pi 的 catalog 沒標，預設當 completions → 打到壞掉的端點 → 500 ×3 後放棄
- 同一秒實測：`/responses` → 200、`/chat/completions` → 500，鐵證
- 根因是「模型→協定」對照資料在每個工具各自維護一份，沒有單一真相來源

---

## 1. 症狀

pi 使用 `opencode-zen/muse-spark-1.2-contributor-free` 時，任何請求都失敗：

```
opencode-zen request failed: Error code: 500 - {'type': 'error', 'error': {'type': 'error', 'message': 'Internal server error'}}

Error: opencode-zen failed 3 consecutive model requests (500, 500, 500);
the service is temporarily unavailable. Retry shortly or switch to another provider/model.
```

但同一時間，omp 用「同一把 key、同一顆模型」卻正常回應。

## 2. 調查過程與證據

### 2.1 排除法

| 假設 | 驗證方式 | 結果 |
|---|---|---|
| Zen 整體掛了 | 打 `/zen/v1/models` | 200、0.5s、64 個模型 → 閘道活著 |
| key 不同 | 比對 pi / opencode / omp 的憑證 | 三邊同一把 key |
| 浮動式故障（flapping） | curl 連打 18 次 | 18/18 全 500 → 不是機率問題 |
| 請求形狀差異 | 測 max_tokens / stream / tools / system 各種組合 | 全 500 |
| HTTP client 差異（TLS 指紋） | 用 bun fetch 重放 | 也 500 → 不是客戶端指紋 |
| 特殊標頭 | 測 User-Agent `opencode/1.18.21`、`x-opencode-*` | 全 500 |

**同一時刻對照實驗**：curl 22/22 失敗，omp 卻在 10 秒內成功——統計上不可能是同一條請求路徑。

### 2.2 決定性證據：讀 omp 的 model catalog

omp 是 bun script，直接 grep 二進位內嵌的 catalog：

```
"muse-spark-1.2-contributor-free": {
  api: "openai-responses",          ← Zen 免費模型中唯一
  baseUrl: "https://opencode.ai/zen/v1", ...
}
"hy3-free"、其餘全部 free 模型: { api: "openai-completions", ... }
```

opencode CLI 的 catalog 則標 `provider:{npm:"@ai-sdk/openai"}`——AI SDK 對此標記同樣走 Responses API。

### 2.3 同一秒對打驗證

```bash
# Responses API 格式
POST https://opencode.ai/zen/v1/responses
→ 200 OK（正常回應，status:"incomplete" 因 max_output_tokens）

# 同一秒、同一把 key
POST https://opencode.ai/zen/v1/chat/completions
→ 500 {"type":"error","error":{"type":"error","message":"Internal server error"}}
```

**結論：這顆模型的 `/chat/completions` 轉發層壞了，`/responses` 是通的。**

### 2.4 當下 Zen 免費模型健康度（2026-08-24 實測）

| 模型 | 狀態 |
|---|---|
| `hy3-free` | ✅ 200 |
| `nemotron-3-ultra-free` | ✅ 200 |
| `nemotron-3.5-lightning-free` | ✅ 200 |
| `laguna-s-2.1-free` | ✅ 200 |
| `muse-spark-1.2-contributor-free` | ❌ 500（completions 路徑；responses 路徑正常） |
| `x-preview-f-free` | ❌ 503 Endpoint unavailable |
| `deepseek-v4-flash-free` | ❌ 400 Model is unavailable |
| `mimo-v2.5-free` | ⚠️ 429 FreeUsageLimitError |

## 3. 根因（三層）

### 3.1 模型元資料沒有單一真相來源（主因）

「這顆模型講哪種協定」不在 API 裡——`/zen/v1/models` 只回 model ID 和 context length。每個工具各自維護一份 catalog 硬編，同一份知識複製三份，一份過時就出事。

### 3.2 Zen 閘道不做協定轉譯，還回錯錯誤碼

上游 muse-spark 只講 Responses API，Zen 的 `/chat/completions` 轉發層沒做轉換，直接回 `500`。協定不匹配應回 `4xx`（如 422「此模型請用 /responses」），回 500 讓客戶端誤判為暫時性故障而白白重試。

### 3.3 協定碎片化是背景條件

主流 wire protocol 有三種（chat completions / responses / anthropic-messages），供應商各自選邊、agent 支援度參差，「同一顆模型在不同工具能不能用」變成賭誰的 catalog 抄對了。

## 4. 為什麼三個工具表現不同

三個工具**都內建了協定轉譯層**（相當於各自自幹了一份 gateway），差別只在 catalog 資料：

| 工具 | 轉譯層 | 協定由誰決定 | 這次的 catalog |
|---|---|---|---|
| omp | 內嵌 model catalog | `api:` 欄位 | `openai-responses` ✅ |
| opencode | Vercel AI SDK | `provider.npm` 欄位 | `@ai-sdk/openai` → responses ✅ |
| pi | `pi-ai` provider classes（含 `openai-responses`） | 內建 catalog 的 `api` 標記 | 未標記 → 預設 completions ❌ |

- omp / opencode 是 Zen 同門（opencode.ai 自家工具），catalog 對 Zen 模型是第一手權威
- pi 是第三方，catalog 依賴人工/定期同步，這顆 2026-08-05 新出的模型沒跟上
- 所以不是「誰有機制誰沒有」，而是**誰的資料剛好是對的**。opencode CLI 那次也回過 `Unexpected server error`——三邊都會踩雷，只是機率不同

## 5. 相關事件：OpenRouter shared pool 429

同期另一張截圖：`stealth/ox-alpha` 走 OpenRouter 時回 429，`limit_source: "upstream_provider_shared_pool"`。免費 stealth 模型的上游 pool 為所有使用者共享，他人用量大時即觸發限流。與本事件同源：**免費模型無 SLA，故障與限流是日常**，但性質不同——本事件是協定不匹配（結構性 bug），429 是共享容量限制。

## 6. 解法

| 方案 | 內容 | 成本 | 效果 |
|---|---|---|---|
| A. 各別修 | pi `~/.pi/agent/models.json` 覆寫 provider，將該模型標為 `"api": "openai-responses"`（注意：`modelOverrides` 不支援 `api` 欄位，需覆寫整個 provider models 清單） | 5 分鐘 | 只修 pi，其他工具下次照踩 |
| B. 抽共用：本地統一閘道 | LiteLLM proxy（內建 completions ↔ responses 雙向轉譯），三工具全指向 `localhost:4000` | 半小時 | catalog 只維護一份；附帶統一 fallback/重試/usage |
| C. 治本：上游回報 | pi catalog 補標；回報 Zen completions passthrough 應回 4xx 或做轉譯 | 等 | 根除但不可控 |
| D. 自幹閘道 | Bun `Bun.serve` + fetch streaming，零依賴 ~300 行 | 半天 | 同 B 但完全可控、無依賴 |

### 方案 B/D 架構

```
pi / omp / opencode          gateway (localhost:4000)              Zen 上游
      │ completions                │ MODEL_MAP: model → 協定
      └──────────────→ 統一入口 ──┬─ passthrough ──→ /chat/completions（hy3 等）
                                  └─ 轉譯 ──→ /responses（muse-spark）
```

### 自幹（方案 D）工作量評估

| 段 | 內容 | 難度 |
|---|---|---|
| 轉請求 | messages→input、max_tokens→max_output_tokens、tools 攤平 | 簡單 ~50 行 |
| 轉非串流回應 | output[] → choices/message/tool_calls、usage 對映 | 簡單 ~40 行 |
| 轉串流 SSE | `response.output_text.delta` → `delta:{content}` 等事件重組 | 主要工作量 ~150 行 |
| 多輪工具迴圈 | completions 的 `tool_calls`+`tool` role → responses 的 `function_call`+`function_call_output` 成對轉換 | **最容易出錯**：單輪測試會過，agent 第二三輪才爆 |

建議：直接拿 pi 真實 session 的多輪工具請求錄製為測試案例。

### 立即可用的繞法

pi 上暫時避開 `muse-spark-1.2-contributor-free`，改用實測正常的：

```bash
pi --provider opencode-zen --model nemotron-3.5-lightning-free
pi --provider opencode-zen --model hy3-free
```

## 7. 教訓

1. **500 不一定是暫時性故障**——pi 的「retry 3 次」對結構性 bug 無效，反而浪費時間；閘道回對錯誤碼（4xx）比客戶端重試策略更重要
2. **「同一把 key、同一個 baseURL」不代表同一條路**——中間還有一層 catalog 決定協定
3. **免費模型的 catalog 會說謊/過時**——目錄列著 ≠ 能用，能用 ≠ 每個客戶端都能用
4. 複製三份的知識遲早不一致——這是抽共用閘道最核心的價值

## 8. 解決紀錄（2026-08-24）

最終採用「方案 A 的 rivumi 內建版」：在 rivumi 實作 `ResponsesModel` adapter，而非外部閘道。

### 改動

| 檔案 | 內容 |
|---|---|
| `contracts.py` | `ModelProtocol` 新增 `OPENAI_RESPONSES = "openai_responses"` |
| `models.py` | `ResponsesModel` adapter（`_HttpModel` 系，non-streaming，含多輪工具轉譯）+ `_responses_input` / `_responses_tools` / `_responses_tool_call` helpers |
| `provider_catalog.py` | `RESPONSES_PROTOCOL_MODELS` 對照表 + `uses_responses_protocol()` —— 本事件的「單一真相來源」 |
| `cli.py` | `_model_from_env` 在 SIMPLE_API_KEY 分流：命中對照表的模型改走 `ResponsesModel` |
| `tests/test_models.py` | 4 個新測試：roundtrip、請求形狀（instructions/tools/多輪 function_call_output）、incomplete→length、錯誤碼正規化 |

### 驗證

- `pytest tests/test_models.py` 50 全綠；全套測試除既有失敗外無新增失敗
- 真實 Zen API 端到端：muse-spark 單輪文字、tool call 轉譯、多輪工具迴圈（`function_call_output` 送回後正確續答）全通

### 設計筆記

- 轉譯邏輯沿用 `codex_oauth.OpenAICodexResponsesModel` 的既有模式（該 adapter 早已證明 responses 協定在 rivumi 可行），差異僅在 Codex 走 SSE、本 adapter 走 JSON——符合 rivumi non-streaming 邊界
- pi 仍無法直接用 muse-spark（pi 的 catalog 問題在 pi 側）；外部工具若需要，可走 rivumi 的 `ModelGateway`（non-streaming）或 zen-gateway 實驗專案（streaming）
- 後續若上游新增 responses-only 模型，只需更新 `provider_catalog.RESPONSES_PROTOCOL_MODELS`

## 附錄：調查指令速查

```bash
# Zen 免費清單
curl -sS https://opencode.ai/zen/v1/models | jq -r '.data[] | select(.id | test("free";"i")) | .id'

# 測 completions 路徑
curl -sS https://opencode.ai/zen/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"muse-spark-1.2-contributor-free","max_tokens":20,"messages":[{"role":"user","content":"hi"}]}'

# 測 responses 路徑
curl -sS https://opencode.ai/zen/v1/responses \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"muse-spark-1.2-contributor-free","max_output_tokens":50,"input":"hi"}'

# 挖工具內嵌 catalog
strings ~/.bun/bin/omp | rg '"muse-spark-1.2-contributor-free"'
strings /opt/homebrew/bin/opencode | rg '"muse-spark-1.2-contributor-free"'
```
