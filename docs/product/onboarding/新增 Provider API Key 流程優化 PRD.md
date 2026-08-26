# 優化新增 Provider API Key 流程

## 1. 使用者意圖

不是「填 API key」這個動作本身，而是「**用我信任/熟悉的 provider，確認它能正確接上**」——使用者已經決定要用哪個 provider（例如已在用 Anthropic API 或自架 Ollama），重點是快速、可靠地把它接通，而不是被系統晾在一旁、送出任務才發現接不通。

## 2. 現況苦工

現況（`src/rivumi/tui.py`、`src/rivumi/native_credentials.py`、`src/rivumi/cli_config.py`）：

- **存完 key 不驗證，要跑完任務才發現錯**：`native_credentials.py:154-156` 只檢查欄位非空白、無 NUL byte，完全不打 API 做連線測試；`_is_ready()`（`tui.py:1688`）也只檢查 provider/model 是否非空，不檢查 credential 是否存在。使用者要等到真正送出任務、第一次呼叫 provider API 時，才會看到 401 或連線錯誤。
- **選 provider 與輸入 key 是兩個分開的彈窗，流程斷裂**：`OnboardingModal`（`tui.py:1050`）選完 Runtime/Connection/Model 按 Save & Continue 後直接關閉，不會馬上要求輸入 API key；要等到之後手動切 `/runtime` 或某個欄位缺失時，才會另外彈出 `ApiKeyModal`（`tui.py:1295`）。
- **Model 欄位是自由輸入，不知道該填什麼**：除了 Ollama 是下拉選單（讀本機 `ollama list`），其餘所有 provider 的 Model 欄位都是自由輸入文字框（`tui.py:1146-1150`），不驗證是否為該 provider 真實存在的 model，容易拼錯。
- **不知道自己已經設過哪些 provider、哪個有效**：沒有任何一個畫面能一眼看到「目前有效 credential 的 provider 清單」，每次都要重新摸索或翻 `~/.local/state/rivumi/auth/` 底下的檔案。

## 3. 參考案例

Rivumi 本身會 shell out 呼叫的三個外部 CLI（`opencode_backend.py`、`pi_backend.py`、`omp_backend.py`），剛好各自示範了這四個苦工點的解法：

- **`opencode auth list`**：一進去就看到現況 —— provider 名稱 + 憑證類型（`● OpenCode Zen api` / `● Nvidia api`），不用猜。
- **`opencode auth login`**：選 provider → 貼 key／走 OAuth，是同一個連續流程，不是兩個斷開的畫面。
- **`opencode models [provider] --refresh`**：向 provider 動態抓真實可用的 model 清單，而非自由輸入。
- **`pi auth check --provider X --model Y [--json] [--no-refresh]`**：明確的 readiness check 指令，可在存 key 當下立刻驗證是否能打通。

## 4. Agent 介入設計（模式 C：流程中即時驗證 + 自動建議）

### UI 流程（Textual TUI，`OnboardingModal` 改版）

1. **第一畫面：Provider 狀態總覽**（新增，對應 `opencode auth list`）
   - 開啟 modal 時，先列出目前已設定的 provider，每項標示狀態圖示：
     - `✓` 已驗證可用（distinguishes from `native_credentials.py` 有存值）
     - `⚠` 已存 key 但尚未驗證 / 上次驗證失敗
     - 未列出 = 尚未設定
   - 底部提供「新增 Provider」按鈕，進入第 2 步。
   - 選擇一個已存在的 provider 列項 = 直接進入該 provider 的第 3 步（重新驗證 / 更新 key）。

2. **第二畫面：Runtime / Connection 選擇**（沿用現有 Runtime → Connection 兩個下拉，邏輯不變）

3. **第三畫面：Credential 輸入 + 即時驗證**（合併現有 `ApiKeyModal`，取消獨立彈窗）
   - 依 `NATIVE_CREDENTIAL_FIELDS` 顯示對應欄位（多數是單一 `api_key`，`workers-ai` 是 `account_id` + `api_token`）。
   - 欄位填完（或欄位失焦）時，立即背景呼叫一次輕量 API 做連線測試（例如 list-models 或最小 completion 呼叫），畫面顯示 spinner → `✓ 已連線，偵測到帳號可用` 或 `✗ 連線失敗：<原因>`。
   - **驗證失敗時擋下「Save & Continue」**，按鈕反灰、顯示錯誤原因；但保留一顆次要按鈕「略過驗證直接存」，供離線/暫時性驗證端點掛掉時仍可強制寫入，避免把使用者鎖死。
   - Ollama（本機、免 key）維持現況免驗證直接可用。

4. **第四畫面：Model 選擇（動態清單，取代自由輸入）**
   - 驗證成功後，自動向該 provider 拉取可用 model 清單（例如 OpenAI-compatible 打 `/models`、Anthropic 用已知清單、Ollama 沿用現有 `ollama list` 邏輯），改成下拉選單而非文字框。
   - 拉取失敗（provider 不支援 list-models API）時 fallback 回自由輸入文字框，不阻斷流程。

5. **Save & Continue / Use once / Cancel**：語意不變，但只有在通過第 3 步驗證（或使用者主動選擇略過）後才能按下。

### 後端調整

- `native_credentials.py`：新增 `verify_native_credential(provider, fields) -> VerificationResult`，對每個 provider 定義最小驗證呼叫方式（部分 provider 可共用 OpenAI-compatible `/models` 端點）。
- `native_credentials.py` 或新檔 `provider_models.py`：新增 `list_provider_models(provider) -> list[str]`，供 Model 下拉選單使用，失敗時回傳空清單觸發 UI fallback。
- `tui.py`：`OnboardingModal` 拆成多步驟 wizard（狀態總覽 → runtime/connection → credential+驗證 → model），移除獨立的 `ApiKeyModal` 呼叫路徑，改為同一流程內的一步。
- `cli.py`：CLI 端的 `rivumi auth set-key <provider>` 存檔後，同步呼叫驗證並印出 `✓`/`✗` 結果（而非現況只印靜態提示文字）；`rivumi auth list`（新指令，對應 `opencode auth list`）列出各 provider 驗證狀態。

### 順手修復（與此流程強相關的既有 bug）

- `cli_config.py:33` 的 `SUPPORTED_RUNTIMES` 未包含 `opencode`/`pi`/`omp`，但 `runtime_registry.py:181-186` 已允許它們出現在 Runtime 下拉選單。目前選了會在下次啟動時讓 `CliConfig.model_validate()` 拋錯，導致 CLI/TUI 完全無法啟動。應在此次改版中一併把三者加入 `SUPPORTED_RUNTIMES`。

## 5. 可控性檢查

- [x] 每個 agent 動作（連線驗證、model 清單拉取）前後都有使用者可介入點：驗證結果顯示給使用者看，使用者可重試、略過或改填。
- [x] 結果落在使用者預期內：驗證只回報「通/不通」與原因，不做任何自動選擇 provider 或自動送出任務的行為。
- [x] 可「重生 / 微調單格 / 退回上一步」：狀態總覽畫面可重新驗證單一 provider；credential 輸入失敗可原地重填不必整個流程重來。
- [x] 不是右邊一個 chat 視窗，是融進既有的三步驟 wizard 流程裡。

## 6. 相關既有程式與文件

- `src/rivumi/tui.py:1050`（`OnboardingModal`）、`tui.py:1295`（`ApiKeyModal`，將被合併）、`tui.py:1688`（`_is_ready`）、`tui.py:1741`（`_run_configuration`）
- `src/rivumi/native_credentials.py`（credential store，需新增驗證與 model 清單函式）
- `src/rivumi/cli_config.py:33`（`SUPPORTED_RUNTIMES`，需修 bug）
- `src/rivumi/cli.py:1473`（`rivumi auth set-key`）、`cli.py:412-455`（`_credential_hint`）
- `src/rivumi/runtime_registry.py:72-187`（runtime/provider 定義）
- 參考對象：本機已安裝的 `opencode`（`/opt/homebrew/bin/opencode`，`auth list` / `auth login` / `models --refresh`）、`pi`（`/opt/homebrew/bin/pi`，`auth check`）
- 文件慣例：本專案用 `docs/stages/m<N>-<slug>.md` 記錄「已完成並驗證」的里程碑（見 `docs/stages/m8-first-run-onboarding.md`），此規格是實作前的設計文件，故不放在 `docs/stages/`，實作完成後應依慣例補一篇新的 `docs/stages/m12-onboarding-credential-verification.md`（含測試通過紀錄、commit）。

## 7. 下一步

- [ ] 依此規格拆分實作任務（建議先修 `SUPPORTED_RUNTIMES` bug，再做即時驗證，最後做動態 model 清單，三者可分批上線）
- [ ] 實作完成後依專案慣例補 `docs/stages/m12-...md`
