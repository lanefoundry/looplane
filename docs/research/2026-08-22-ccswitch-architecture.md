# CCSwitch 如何切換 coding CLI、API provider 與訂閱 OAuth

研究日期：2026-08-22

## 研究問題

1. 使用者所說的 CCSwitch 是哪一個專案？
2. 它是在切設定檔、代理 API，還是自己實作 agent harness？
3. Claude、Codex 的 credentials 與 OAuth token 分別由誰持有？
4. 它如何做到跨協定、快速切 provider 與 failover？
5. 哪些設計適合移植到 Rivumi 的「Coding CLI」與「Model API」兩種模式？

## 結論

這裡的 CCSwitch 是 [`farion1231/cc-switch`](https://github.com/farion1231/cc-switch)。它不是 Pi、Codex CLI 或 Claude Code 那種 agent harness，而是一個位於既有 coding CLI 周邊的管理與傳輸層：

- **設定切換器**：更新 Claude Code、Codex、Gemini 等官方或第三方 CLI 的設定檔。
- **本機路由器**：把 CLI 的 base URL 改到 `127.0.0.1:15721`，再依目前選取的 provider 轉送、轉換協定、記錄用量與 failover。
- **Auth Center**：保存部分 provider 的 API key/OAuth 帳號；對 Codex ChatGPT OAuth 可取得、刷新 token，並在需要時把完整 token bundle 寫回 `~/.codex/auth.json`。
- **不是 agent loop**：工具呼叫、上下文、shell/file 操作、重試與任務流程仍由 Claude Code、Codex CLI 等上游 client 負責。

因此，CCSwitch 最接近 Rivumi 的 **Coding CLI 模式加上一個可選 transport router**，並不能取代 Rivumi 的 **Model API 模式／自有 harness**。

## 架構

```text
使用者
  │
  ▼
Claude Code / Codex CLI / Gemini CLI       ← agent loop、tools、context、UI
  │
  ├─ 直接模式：CLI config ───────────────→ 選定 provider API
  │
  └─ Routing 模式：base URL → 127.0.0.1:15721
                                  │
                                  ▼
                         CCSwitch local proxy
                    route / protocol transform / auth
                    log / usage / circuit breaker
                                  │
                    ┌─────────────┼─────────────┐
                    ▼             ▼             ▼
              Anthropic API  OpenAI-compatible  ChatGPT Codex backend
                                               via Codex OAuth
```

## 三條實際路徑

### 1. 純設定切換

CCSwitch 修改既有 CLI 的 config。文件明確寫到切換後通常要重啟 CLI；選擇「Official Login」會恢復官方 endpoint/login。這條路徑沒有代理，也沒有 CCSwitch agent loop。

### 2. Local proxy takeover

開啟 proxy/routing 後，CCSwitch 會備份原設定，再把各 CLI 的 endpoint 指到 loopback：

- Claude：`ANTHROPIC_BASE_URL=http://127.0.0.1:15721`
- Codex：`base_url=http://127.0.0.1:15721/v1`
- Gemini：對應的本機 gateway

本機 server 實作 Anthropic Messages、OpenAI Chat Completions、OpenAI Responses 與 Gemini routes。Routing mode 可在不中斷 CLI 的情況下換 upstream provider；不同 app 有獨立 routing/failover queue。

這是 **API transport interception**，不是 harness replacement。以「Claude Code 使用 ChatGPT 訂閱」為例：Claude Code 照常產生 Anthropic Messages 請求，CCSwitch 把它轉成 Codex Responses，注入 ChatGPT OAuth，送往 `https://chatgpt.com/backend-api/codex`，再把串流 response/tool event 轉回 Claude Code 能理解的形狀。

### 3. Auth Center / managed OAuth

Codex OAuth manager 實作 OpenAI device-code flow、每帳號 refresh lock、access token cache，以及 refresh token 持久化。新的多帳號路徑還會：

- 將綁定帳號的完整 access/id/refresh token bundle 寫入 `~/.codex/auth.json`；
- 在覆寫前讀回 Codex CLI 可能已輪換的 refresh token；
- 無法安全判斷 token 世代時取消操作，避免破壞有效登入；
- 官方 ChatGPT provider 不自動 failover，避免意外切到另一個計費來源。

「Official auth preservation」是另一條較保守的路徑：保留官方 `~/.codex/auth.json`，把第三方 endpoint/key 放在 `~/.codex/config.toml`。此時官方 OAuth 只維持 Codex 身分／官方能力，第三方模型仍由第三方依其規則計費。

### Codex 與 Claude 訂閱不是對稱能力

- **Codex**：CCSwitch 自己實作 ChatGPT Plus/Pro device OAuth、refresh、多帳號與 token injection；可讓 Codex CLI 傳入 bearer，也可由 proxy 對 ChatGPT Codex backend 動態注入。
- **Claude**：CCSwitch 的 managed Auth Center 沒有 Anthropic/Claude OAuth provider。它只讀 Claude Code 已建立的 macOS Keychain 或 `~/.claude/.credentials.json`，用該 access token查詢 subscription usage；不負責 Claude 登入或 refresh，也沒有把 Claude Pro/Max 暴露成獨立 model transport。

所以 CCSwitch 並沒有解決「第三方自有 harness 使用 Claude Pro/Max 內含額度」。Claude 官方訂閱路徑仍是 **Claude Code 持有 OAuth 與 agent runtime**；CCSwitch 只做設定管理或額度顯示。

## 對 Rivumi 的含義

Rivumi 應把兩個選項拆成兩個互相正交的軸，而不只是單一 toggle：

| 軸 | 選項 | 誰擁有 agent loop | 誰呼叫模型 |
|---|---|---|---|
| Execution mode | Coding CLI | Codex CLI / Claude Code | CLI 直接或經 Rivumi router |
| Execution mode | Model API | Rivumi `AgentRunner` / harness | Rivumi `ModelProvider` |
| Transport mode | Direct | 不改變 | execution owner 直連 upstream |
| Transport mode | Local router | 不改變 | 先到 Rivumi loopback，再 route/convert |

建議的邊界：

```text
CodingCliRunner
  ├─ CodexCliAdapter
  └─ ClaudeCodeAdapter
        └─ optional LocalModelRouter

ApiAgentRunner
  └─ ModelProvider
       ├─ OpenAI API key
       ├─ Anthropic API key
       ├─ OpenRouter / compatible APIs
       └─ Codex subscription transport（若產品與條款風險可接受）
```

可直接借用的 CCSwitch 模式：

- config ownership marker，不以「proxy process 還活著嗎」判斷是否可 restore；
- 原設定 atomic backup/restore；
- 每個 app/provider switch 加鎖與 revision conflict check；
- official login、third-party API key、managed OAuth 分開呈現；
- route、protocol conversion、billing source、agent-loop owner 分開顯示；
- 官方訂閱 route 禁止隱性 failover 到付費 API key；
- proxy log 明確記錄 app、provider、model、endpoint、status，但遮蔽 secret。

不建議直接照搬的部分：

- 把跨產品的 OAuth token 搬進另一個 CLI 當作正常、穩定的公開 API；
- 把「可以技術上轉送」描述成「官方支援」；
- 把 ChatGPT/Claude 訂閱、API billing、extra usage 混成同一種額度；
- 讓 local proxy 與自有 harness 共用同一個模糊的 `Provider` 抽象，導致工具迴圈 ownership 不清楚。

CCSwitch 自己也在 release note 對 Codex OAuth reverse proxy 標示條款與帳號限制風險。因此若 Rivumi 提供這條路，應是清楚標示的 experimental transport；而「呼叫官方 Codex CLI」則是較穩定的 subscription-backed Coding CLI mode。

## 事實交叉表

| 事實 | 主要證據 | 交叉證據 | 判定 |
|---|---|---|---|
| CCSwitch 修改 CLI config，通常需重啟 | [Quick Start](https://github.com/farion1231/cc-switch/blob/main/docs/user-manual/en/1-getting-started/1.4-quickstart.md) | [App Routing](https://github.com/farion1231/cc-switch/blob/main/docs/user-manual/en/4-proxy/4.2-routing.md) | 已確認 |
| Proxy takeover 將 base URL 指到 loopback | [Proxy Service](https://github.com/farion1231/cc-switch/blob/main/docs/user-manual/en/4-proxy/4.1-service.md) | [server.rs](https://github.com/farion1231/cc-switch/blob/main/src-tauri/src/proxy/server.rs) | 已確認 |
| 支援 Messages、Chat Completions、Responses 轉換 | [Proxy Service](https://github.com/farion1231/cc-switch/blob/main/docs/user-manual/en/4-proxy/4.1-service.md) | [providers/mod.rs](https://github.com/farion1231/cc-switch/blob/main/src-tauri/src/proxy/providers/mod.rs) | 已確認 |
| Codex OAuth 直達 ChatGPT Codex backend | [providers/mod.rs](https://github.com/farion1231/cc-switch/blob/main/src-tauri/src/proxy/providers/mod.rs) | [codex_oauth_auth.rs](https://github.com/farion1231/cc-switch/blob/main/src-tauri/src/proxy/providers/codex_oauth_auth.rs) | 已確認 |
| CCSwitch 管理 Codex OAuth token、多帳號與 refresh | [codex_oauth_auth.rs](https://github.com/farion1231/cc-switch/blob/main/src-tauri/src/proxy/providers/codex_oauth_auth.rs) | [CHANGELOG](https://github.com/farion1231/cc-switch/blob/main/CHANGELOG.md) | 已確認 |
| 它不是 coding-agent harness | [Proxy Service](https://github.com/farion1231/cc-switch/blob/main/docs/user-manual/en/4-proxy/4.1-service.md) | server routes、provider adapters 與 config architecture；未發現獨立 tool loop | 高信心推論 |
| Codex OAuth reverse proxy 有條款風險 | [v3.13.0 release note](https://github.com/farion1231/cc-switch/blob/main/docs/release-notes/v3.13.0-en.md) | [v3.16.1 release note](https://github.com/farion1231/cc-switch/blob/main/docs/release-notes/v3.16.1-en.md) | 專案明示 |
| Claude subscription auth 由 Claude Code 持有，CCSwitch 只讀取查額度 | [subscription.rs](https://github.com/farion1231/cc-switch/blob/main/src-tauri/src/services/subscription.rs) | [auth commands](https://github.com/farion1231/cc-switch/blob/main/src-tauri/src/commands/auth.rs) | 已確認 |

## 來源閱讀清單

- ✅ [Quick Start](https://github.com/farion1231/cc-switch/blob/main/docs/user-manual/en/1-getting-started/1.4-quickstart.md)：直接 config switching 與 official-login 行為。
- ✅ [Proxy Service](https://github.com/farion1231/cc-switch/blob/main/docs/user-manual/en/4-proxy/4.1-service.md)：loopback takeover、協定與用途。
- ✅ [App Routing](https://github.com/farion1231/cc-switch/blob/main/docs/user-manual/en/4-proxy/4.2-routing.md)：不中斷切換、每 app routing、backup/restore。
- ✅ [Codex official auth preservation guide](https://github.com/farion1231/cc-switch/blob/main/docs/guides/codex-official-auth-preservation-guide-en.md)：`auth.json` 與第三方 provider credential 分離。
- ✅ [v3.13.0 release note](https://github.com/farion1231/cc-switch/blob/main/docs/release-notes/v3.13.0-en.md)：ChatGPT subscription through Claude Code 與風險聲明。
- ✅ [v3.16.1 release note](https://github.com/farion1231/cc-switch/blob/main/docs/release-notes/v3.16.1-en.md)：ownership marker、鎖、協定轉換與風險。
- ✅ [CHANGELOG](https://github.com/farion1231/cc-switch/blob/main/CHANGELOG.md)：目前 Pi 管理邊界、Codex 多帳號、refresh-token adoption 與 failover 限制。
- ✅ [server.rs](https://github.com/farion1231/cc-switch/blob/main/src-tauri/src/proxy/server.rs)：實際 local HTTP routes。
- ✅ [providers/mod.rs](https://github.com/farion1231/cc-switch/blob/main/src-tauri/src/proxy/providers/mod.rs)：provider type、Codex backend、adapter/transform 邊界。
- ✅ [providers/auth.rs](https://github.com/farion1231/cc-switch/blob/main/src-tauri/src/proxy/providers/auth.rs)：各種上游 header/auth strategy。
- ✅ [codex_oauth_auth.rs](https://github.com/farion1231/cc-switch/blob/main/src-tauri/src/proxy/providers/codex_oauth_auth.rs)：OAuth device flow、token storage/refresh 與 concurrency safeguards。
- ✅ [provider_router.rs](https://github.com/farion1231/cc-switch/blob/main/src-tauri/src/proxy/provider_router.rs)：provider selection、circuit breaker 與 official-provider failover 限制。
- ✅ [switch_lock.rs](https://github.com/farion1231/cc-switch/blob/main/src-tauri/src/proxy/switch_lock.rs)：per-app serialization。
- ✅ [subscription.rs](https://github.com/farion1231/cc-switch/blob/main/src-tauri/src/services/subscription.rs)：Claude Code credential read-only 與 quota query。
- ✅ [auth commands](https://github.com/farion1231/cc-switch/blob/main/src-tauri/src/commands/auth.rs)：managed OAuth provider 範圍不含 Claude。

所有網頁與 GitHub 原始碼均透過 Groundlane 完整抓取；超長檔案只抽取與研究問題直接相關的區段分析。
