# Pi 如何處理 Google subscription

研究日期：2026-08-22

## 結論

**目前 Pi 已不支援 Google Gemini CLI 或 Antigravity subscription OAuth。** Pi 在 2026-04-30 的 commit `fe66edd` 移除了 built-in Google Gemini CLI / Antigravity provider、model catalog、OAuth 與 exports。現在 Pi 的 Google provider 是標準 Gemini API key (`GEMINI_API_KEY`)；這條路使用 Gemini API quota／計費，不會使用 Google AI Pro/Ultra 的 Gemini CLI subscription quota。

Pi 過去確實提供過 subscription-backed Google transport，而且和 Claude 的 third-party extra usage 不同：Pi 自有 harness 直接以 Google OAuth token 呼叫 Cloud Code Assist endpoint，請求計入登入帳號的 Gemini CLI / Code Assist quota。這條整合後來被完整移除；移除 commit 沒有說明官方原因，因此不能把社群的封號或條款猜測寫成確定原因。Google 現行官方文件另已明確禁止第三方工具 piggyback Gemini CLI OAuth，並警告可能立即停權或終止帳號。

## 過去如何運作

```text
Pi agent loop / tools
        │
        ▼
Pi google-gemini-cli provider
        │ OAuth bearer + Google Cloud project ID
        ▼
cloudcode-pa.googleapis.com/v1internal:streamGenerateContent
        │
        ▼
Google Gemini CLI / Code Assist account quota
```

### 1. Pi 自己執行 Google OAuth

舊版 `google-gemini-cli` OAuth module：

- 使用 Google OAuth authorization-code flow 與 localhost callback；
- scope 包含 `cloud-platform`、email 與 profile；
- 取得 access token、refresh token；
- 自己刷新 access token；
- 將 credentials 與 project ID 交給 Pi auth store。

它不是 shell out 到官方 `gemini` CLI，也不是讀取 Gemini CLI 的既有 credential cache。

### 2. 探測或建立 Code Assist project

登入後，Pi 呼叫：

- `v1internal:loadCodeAssist`
- 必要時 `v1internal:onboardUser`

以取得或建立 `cloudaicompanionProject`。免費個人 tier 可由 Google provision managed project；付費／Workspace Code Assist 帳號可能需要 `GOOGLE_CLOUD_PROJECT`。

### 3. Pi 自有 harness 直接呼叫模型

Pi 將 `{ access token, projectId }` 組成 provider credential，向：

`https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse`

送出 Cloud Code Assist 格式的 request。Pi 自己負責 conversation、tool loop、工具執行與後續回合，因此這與 CCSwitch 的「保留 Claude Code/Codex CLI harness」不同。

### 4. 額度來源

這條 OAuth transport 使用 Google account / Gemini Code Assist quota，而不是 `GEMINI_API_KEY` quota。Google 官方目前列出的 Gemini CLI daily maximum 是：

| 登入方式 | 方案 | 每位使用者每天最多 requests |
|---|---|---:|
| Google Account | Code Assist Individual | 1,000 |
| Google Account | Google AI Pro | 1,500 |
| Google Account | Google AI Ultra | 2,000 |
| Gemini API key | Unpaid | 250 |
| Gemini API key / Vertex | Pay-as-you-go | 依 tier／quota |

一個使用者 prompt 可能造成多個 model requests；Gemini CLI 與 Code Assist agent mode 共用 quota，而且 Pro/Flash 等 model family 合併計算。

## 現況

Pi 目前的 provider 文件在 subscription 清單中只列 ChatGPT Codex、Claude、Copilot、xAI、OpenRouter、Radius；Google Gemini 位於 API-key table：

```text
Google Gemini → GEMINI_API_KEY → auth.json key "google"
```

移除 commit 明確刪除：

- `google-gemini-cli` / `google-antigravity` providers；
- Cloud Code Assist model catalog；
- Google OAuth login / refresh code；
- coding-agent `/login` 選項與相關文件；
- Pi AI package exports。

所以「Pi 可以透過 Google subscription 使用 Gemini」是歷史資訊，對目前版本已經過時。

## 對 Rivumi 的含義

技術上，舊 Pi 證明以下組合能運作：

```text
Rivumi harness
  → Google OAuth manager
  → Cloud Code Assist protocol adapter
  → Google account subscription quota
```

但不建議把它當成現在可直接照搬的正式 integration：

- endpoint 使用 `v1internal`，不是 Google 公開 Gemini API contract；
- 需要重用／模擬 Gemini CLI 的 OAuth client 與 Code Assist onboarding；
- Google 或 Pi 可隨時改變／移除此路徑；Pi 已實際移除；
- Google 現行 FAQ 與 terms 明確說第三方軟體直接使用 Gemini CLI OAuth 存取 backend 違反適用條款；
- 社群曾回報 account restriction，但沒有足夠一手證據證明這就是 Pi 移除的原因。

Rivumi 現階段較可靠的 Google 路線是：

1. **Coding CLI mode**：委派官方 Gemini CLI，讓 Google OAuth 與 subscription quota 留在官方 runtime。
2. **Model API mode**：Rivumi harness 使用 Gemini API key 或 Vertex AI credentials，依 API／Cloud quota 計費。
3. 不把舊 Pi `v1internal` transport 當成正式第三條路；若研究性保留，必須標成 unsupported/experimental 並隔離 credential 與 adapter。

## 事實交叉表

| 事實 | 證據 | 狀態 |
|---|---|---|
| Pi 曾自己做 Google OAuth 與 refresh | 舊版 `google-gemini-cli.ts` OAuth source | ✅ 一手 |
| Pi 曾直接呼叫 Cloud Code Assist internal endpoint | 舊版 provider source | ✅ 一手 |
| 舊 transport 使用 Google account Code Assist quota | 舊 Pi 文件 + Google 官方 Gemini CLI quota 文件 | ✅ 交叉確認 |
| Pi 於 2026-04-30 移除整套支援 | Pi commit `fe66edd` | ✅ 一手 |
| 目前 Pi Google provider 只列 API key | 目前 Pi provider 文件 | ✅ 一手 |
| Google 現行政策禁止第三方 piggyback Gemini CLI OAuth | Gemini CLI FAQ + ToS/privacy page | ✅ 一手 |
| 移除是因為 Google 封號／ToS | 移除 commit 未說明原因；只有社群案例 | ⚠️ 未證實，不作定論 |

## 來源閱讀清單

- ✅ 一手：[Pi 目前 provider 文件](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/providers.md)。完整讀取；顯示 subscription 清單不含 Google，Google 只在 API-key table。
- ✅ 一手：[移除 Google Gemini CLI / Antigravity 的 commit](https://github.com/earendil-works/pi/commit/fe66edd943691f8eac295fef68ce36930c35fa05)。完整讀取 commit metadata 與相關 patches；commit message 沒有解釋原因。
- ✅ 一手：[移除前 Google OAuth source](https://github.com/earendil-works/pi/blob/40c6eabb8f9b34ca0ffb652d6e3a7929b5e2eee5/packages/ai/src/utils/oauth/google-gemini-cli.ts)。完整抓取，針對 OAuth、project discovery/onboarding 與 token lifecycle 深讀。
- ✅ 一手：[移除前 Cloud Code Assist provider source](https://github.com/earendil-works/pi/blob/40c6eabb8f9b34ca0ffb652d6e3a7929b5e2eee5/packages/ai/src/providers/google-gemini-cli.ts)。完整抓取，針對 endpoint、request/auth 與 tool/streaming path 深讀。
- ✅ 一手：[移除前 Pi provider 文件](https://github.com/earendil-works/pi/blob/40c6eabb8f9b34ca0ffb652d6e3a7929b5e2eee5/packages/coding-agent/docs/providers.md)。完整讀取；當時明列 Gemini CLI / Antigravity OAuth。
- ✅ 一手：[Gemini CLI quota and pricing](https://github.com/google-gemini/gemini-cli/blob/main/docs/resources/quota-and-pricing.md)。完整讀取；區分 Google Account subscription 與 API key。
- ✅ 一手：[Google Code Assist quotas](https://developers.google.com/gemini-code-assist/resources/quotas)。完整讀取；頁面最後更新 2026-08-11。
- ✅ 一手：[Gemini CLI FAQ](https://geminicli.com/docs/resources/faq/) 與 [terms/privacy](https://geminicli.com/docs/resources/tos-privacy/)。完整讀取；明確禁止第三方 OAuth piggyback，並指向 AI Studio API key 或 Vertex AI。
- 🟡 社群案例：[Pi discussion about subscription safety](https://github.com/earendil-works/pi/discussions/1510)。只作風險訊號，不用來推斷移除原因。

所有網頁與 GitHub 內容只透過 Groundlane 抓取。
