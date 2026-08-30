# LSP 整合：把編譯器診斷推進 agent context（系列 #36 取證）

日期：2026-08-26。證據格式 `repo/path/file.ext#symbolName`，本地 clone 於 `~/Projects/coding-agent-reference/`。

## omp（can1357/oh-my-pi）

注意：routing table 寫「crates 的 lsp、dap」，實際 grep 後 **Rust crates 內沒有獨立 lsp/dap crate**（`oh-my-pi/crates/` 只有 pi-ast/pi-builtins/pi-iso/pi-natives/pi-shell/pi-voice/pi-walker/vendor）。LSP 整合在 TypeScript 側：

- `oh-my-pi/packages/coding-agent/src/lsp/client.ts#LSPClient` — stdio JSON-RPC client
- `oh-my-pi/packages/coding-agent/src/lsp/writethrough.ts#createLspWritethrough` — 檔案寫入必經 LSP writethrough：didOpen/didChange/didSave 後等診斷
- `oh-my-pi/packages/coding-agent/src/lsp/diagnostics.ts` — timeout 常數：SINGLE_DIAGNOSTICS_WAIT_TIMEOUT_MS=3000、BATCH=400、INLINE=500、DEFERRED=12_000、PIPELINE_GRACE=10_000；MAX_GLOB_DIAGNOSTIC_TARGETS=20、WORKSPACE_SYMBOL_LIMIT=200
- `oh-my-pi/packages/coding-agent/src/lsp/deferred-diagnostics.ts#DeferredDiagnostics` — 工具結果已回傳後才到的「遲到診斷」；用 per-file mutationVersion 判 stale（`isStale()`）
- `oh-my-pi/packages/coding-agent/src/lsp/diagnostics-ledger.ts#DiagnosticsLedger.reduce` — 跨 turn 去重：diagnosticIdentity 把 location prefix 剝掉後比對
- `oh-my-pi/packages/coding-agent/src/sdk.ts` — `queueDeferredDiagnostics` → `yieldQueue.enqueue(LSP_LATE_DIAGNOSTIC_MESSAGE_TYPE, entry)`，註冊在 sdk.ts#3758 附近（buildLateDiagnosticsBatchMessage），遲到診斷以 custom message 批次注入下一個 turn 的 context
- `oh-my-pi/packages/coding-agent/src/lsp/tool.ts#LspTool` — 主動查詢工具，actions：status / diagnostics / definition / symbols / request / reload / rename_file / capabilities；read-only session 只放行 LSP_READONLY_ACTIONS
- `oh-my-pi/packages/coding-agent/src/lsp/workspace-diagnostics.ts#runWorkspaceDiagnostics`、`detectProjectTypes`
- `oh-my-pi/packages/coding-agent/src/lsp/defaults.json` — 60+ 內建 server 預設（rust-analyzer `checkOnSave:false`、pyright openFilesOnly 等）
- 測試見 `test/tools/lsp-diagnostics-freshness.test.ts`、`lsp-batching.test.ts`、`lsp-diagnostics-dedup.test.ts`；bench `bench/edit-lsp-writethrough.bench.ts`

## claude-code（decompiled v2.1.88）

- `claude-code-source/src/services/lsp/passiveFeedback.ts#registerLSPNotificationHandlers` — 對所有 server 訂閱 `textDocument/publishDiagnostics`，錯誤逐 server 隔離，連續失敗 ≥3 次警告
- `claude-code-source/src/services/lsp/LSPDiagnosticRegistry.ts#registerPendingLSPDiagnostic` / `#checkForLSPDiagnostics` — pending registry → attachment。量控：MAX_DIAGNOSTICS_PER_FILE=10、MAX_TOTAL_DIAGNOSTICS=30，severity 排序優先 Error；去重 key=message+severity+range+source+code，跨 turn 用 LRUCache(max 500 files)；`clearDeliveredDiagnosticsForFile` 在檔案被編輯時清掉已投遞紀錄
- `claude-code-source/src/utils/attachments.ts#getLSPDiagnosticAttachments`（約 L2883）— 每次 query 前檢查 registry，轉成 attachment 自動送進對話；註解明言「LSP diagnostics are only useful if the agent has the Bash tool」
- `claude-code-source/src/services/lsp/config.ts#getAllLspServers` — server 只能由 plugin 提供，不開放 user/project settings

## opencode（sst/opencode）

- `opencode/packages/opencode/src/lsp/client.ts` L160 — `connection.onNotification("textDocument/publishDiagnostics")` 推入 push store；L564 特意不在 didChange 清空診斷（clangd 只在內容真的變了才重發）
- `opencode/packages/opencode/src/lsp/diagnostic.ts#report` — 只取 severity===1（error）、每檔上限 20，render 成 `<diagnostics file="...">` 區塊
- 注入點：`opencode/packages/opencode/src/tool/edit.ts`（約 L200）、`tool/write.ts#MAX_PROJECT_DIAGNOSTICS_FILES`、`tool/apply_patch.ts` — 直接附加在編輯工具的 tool result 後面
- `opencode/packages/core/src/v1/config/lsp.ts#builtinServerIds` — 約 40 個 builtin server

## pi（badlogic/pi-mono）：負向發現

`grep -rn "\blsp\b" pi-mono/packages --include="*.ts"` 核心程式碼無命中（只有 examples/incidental）。上游 pi 沒有 LSP 整合——這是 omp fork 後加的，屬於「fork 加值」的演進證據。

## codex（openai/codex）：負向發現

`grep -rln -i "\blsp\b" codex/codex-rs --include="*.rs"` 無命中。codex 沒有 LSP 整合，靠 shell 執行測試/build 取得回饋。

## looplane 現況

- `src/looplane/tools.py#run_check` — 「Run one exact argv verification command selected by its allowlisted name」（L197 description）；verification_commands 由外部傳入 exact non-empty argv（tools.py L80-83）。無任何 LSP / 被動診斷迴路。
- 外部執行基建：`src/looplane/external_runner.py#ExternalCodingRunner`。

## looplane 設計草案（寫進文章）

1. Phase 1 pull-on-edit：編輯工具成功後對該檔做一次 bounded LSP 診斷查詢（學 opencode `<diagnostics>` block，error-only、per-file cap）
2. Phase 2 長駐 server 子程序（掛 external_runner 同級的生命週期管理）＋ deferred diagnostics 注入下一 turn（學 omp mutationVersion staleness + ledger 去重）
3. run_check 仍是唯一驗證閘門；LSP 是快速顧問訊號，不改變 success 判定
