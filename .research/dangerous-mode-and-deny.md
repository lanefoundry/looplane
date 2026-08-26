# Dangerous Mode 與禁止操作（Deny）機制研究

> 日期：2026-08-25　主題：rivumi 增加 dangerous 模式時如何保留「禁止操作」能力
> 後續：已實作（`src/rivumi/permissions.py`）；AI classifier「auto」模式設計見 `docs/plans/auto-classifier-mode-plan.md`；完整 mode 光譜比較見 `docs/research/approval-mode-comparison.md`

## 四個參考專案的做法

### 1. pi（上游）— 沒有 yolo，信任邊界在載入時
- 工具永遠直接執行，無任何 approval gate（grep `yolo|dangerous|skipPermissions` 為 0 命中）
- 唯一防線是 project trust：headless / 使用者取消一律 fail-closed 回 false（`pi-mono/packages/coding-agent/src/core/project-trust.ts:86-95`）
- 唯一 denylist 是精確名稱的 `--exclude-tools`（Set 比對，`core/sdk.ts:258-263`）
- 啟示：**不採用**——rivumi 需要 per-call 防護

### 2. oh-my-pi（pi fork）— 三層 approval + deny 壓過 yolo ⭐ 主要參考
- 旗標：`--auto-approve` / `--yolo`、`--approval-mode always-ask|write|yolo`（`cli/args.ts:279-280`、`commands/launch-help.ts:100-106`）
- Tier 分類：`read(0) < write(1) < exec(2)`；mode 對應可自動批准的最高 tier（`tools/approval.ts:31-41`）；工具未宣告 tier 一律落 exec（fail-closed，`approval.ts:77`）
- **解析順序（`resolveApproval`, approval.ts:104-125）**：
  1. 工具自身 `approval(args)` 決策
  2. 使用者 per-tool 政策 `tools.approval.<tool>`（allow/prompt/deny）
  3. mode 的 tier 比較
- **deny 在所有分支之前返回，包括 yolo**（`approval.ts:136-154`）→「dangerous 模式不能覆蓋明確 deny」
- 硬編碼 `CRITICAL_BASH_PATTERNS` regex 底線（`tools/bash.ts:172-217`）：rm -rf /、fork bomb、mkfs、curl|sh、shutdown 等；命中強制 prompt，連使用者 allow 都壓不過
- 使用者自訂 `bash.patterns`（glob 僅支援 `*`），**複合命令先 tokenizer 切段再逐段比對**——`cd x && rm -rf /` 也逃不掉（`bash.ts:264-282`）；非對稱語義：allow 須 match 全命令且含控制語法即拒保；deny/prompt 任一段命中即觸發（`bash.ts:284-294`）
- Anti-TOCTOU：extension 改寫 input 後對 effectiveParams 再 resolve 一次（`extensibility/extensions/wrapper.ts:202-246`）
- Provider safety checks 不受 yolo 影響且無 UI 即 throw（`wrapper.ts:305-322`）

### 3. opencode — bypass 只是「自動代答」，引擎永不跳過
- `--auto` ≡ `--yolo` ≡ `--dangerously-skip-permissions`（`packages/opencode/src/cli/cmd/run.ts:274`）
- 實作：監聽 `permission.asked` 事件自動回 `"once"`（`run.ts:798-822`）——**引擎內每個請求仍走完整 evaluation**
- Rule schema：`{ action, resource, effect }`，effect ∈ allow/deny/ask（`packages/schema/src/permission.ts:L54-64`）
- 匹配：自製 wildcard（`*`→`.*`、錨定全串，`core/src/util/wildcard.ts:L3-14`）；resource 對 bash 是 command 字串本身、對檔案工具是 path（`core/src/tool/bash.ts:L142-149`）
- 順序：findLast 最後一條匹配規則勝出；無匹配 fallback `ask`；聚合優先序 deny > ask > allow；**saved allow 排在規則集之後，翻轉不了明確 deny**（`core/src/permission.ts:L76-86,155-162`）
- 無 permission 設定的 agent 預設全拒 `[{action:"*",resource:"*",effect:"deny"}]`（`core/src/permission.ts:L15`）

### 4. codex — yolo 只關 approval+sandbox，deny 層獨立存在
- `--dangerously-bypass-approvals-and-sandbox`（alias `--yolo`）→ `DangerFullAccess` + approval Never（`utils/cli/src/shared_options.rs:52-59`、`tui/src/startup_orchestration.rs:15-17`）
- 但三套 deny 仍然有效：
  1. execpolicy prefix_rule：`Forbidden` 連 approval prompt 都不出現，直接擋（`execpolicy/src/rule.rs:110-115`、`core/src/exec_policy.rs:375-440`）
  2. network domain deny：「explicitly denied by policy and cannot be approved from this prompt」（`core/src/network_policy_decision.rs:46-72`）
  3. filesystem per-path 權限 + writable root 的 read_only_subpaths / protected_metadata_names（防改 `.git/hooks` 提權）（`protocol/src/protocol.rs:1060-1111`）

### 5. Claude Code — bypass 是 pipeline 中的「自動代答 allow」，deny/ask/safety 全部排在它之前 ⭐ pipeline 最完整
- 旗標：`--dangerously-skip-permissions`（`main.tsx:976`），轉成 `bypassPermissions` mode（`utils/permissions/permissionSetup.ts:725-727`）
- **進入前安全閘門**（`setup.ts:395-442`）：
  - root/sudo 直接 exit(1)；豁免：`IS_SANDBOX=1` 或 Bubblewrap env（L406-407）、Windows 整段跳過（L403）
  - 首次進入強制條款對話框；接受紀錄只讀 user/local/flag/policy settings 的 `skipDangerousModePermissionPrompt`，**刻意排除 projectSettings**——「a malicious project could otherwise auto-bypass the dialog (RCE risk)」（`utils/settings/settings.ts:878-889`）
  - 組織 kill-switch：settings `permissions.disableBypassPermissionsMode === 'disable'` 可停用（`permissionSetup.ts:698-711`）；session 中途也不准切進 bypass（`cli/print.ts:4574-4595`）
- Mode enum：`default | acceptEdits | plan | dontAsk | bypassPermissions`（+內部 `auto|bubble`）（`types/permissions.ts:16-38`）
- **規則語法**：`Tool(content)` 格式，如 `Bash(rm -rf:*)`（prefix `:*` / wildcard `*` / exact 三態，`utils/permissions/shellRuleMatching.ts:159-184`；wildcard→dotAll regex，L90-154）；路徑用 gitignore 風格比對（`filesystem.ts:955-1025`），`~/`、`//`、`./` 前綴有明確解析規則（L853-917）
- **Pipeline（`utils/permissions/permissions.ts:1158-1319` `hasPermissionsToUseToolInner()`）**：
  ```
  1a 整工具 deny rule → deny early-return   (L1169-1181)
  1b 整工具 ask rule → ask                  (L1183-1206)
  1c 工具自身 checkPermissions()            (L1208-1223)
  1d 工具回 deny → deny early-return        (L1225-1228)
  1e requiresUserInteraction → 即使 bypass 也問 (L1230-1236)
  1f 內容級 ask rule（Bash(npm publish:*)）→ 優先於 bypass (L1238-1250)
  1g 安全檢查（.git/ .claude/ shell configs）→ bypass-immune (L1252-1260)
  2a bypassPermissions → 自動 allow         (L1262-1281)
  2b allow rule → allow                     (L1283-1297)
  3  passthrough → ask                      (L1299-1310)
  ```
  其中 1a–1g 抽成 `checkRuleBasedPermissions()`，docstring 直稱是「the subset that bypassPermissions mode respects」（L1060-1156）
- **bypass 下 deny 必然生效**：控制流上 deny early-return 在 bypass 分支之前；註解明言 safety checks 「must prompt even in bypassPermissions mode」與「respected even in bypass mode, just as deny rules are respected at step 1d」（L1242-1253）
- PreToolUse hook 可 allow/deny/ask 且執行於 permission check 之前（`services/tools/toolExecution.ts:800-862`）；但 hook 回 allow **不能繞過** settings deny/ask rules——仍會跑 `checkRuleBasedPermissions()`（`toolHooks.ts:372-405`）
- `--allowedTools`/`--disallowedTools` 不是獨立系統，只是被注入成 `cliArg` source 的 allow/deny 規則（`permissionSetup.ts:978-991`），與 settings 各 source 合併評估



1. **deny 是權威層，壓過 dangerous mode**：omp 的 deny 先於 yolo 返回、codex 的 Forbidden/網域 deny 不可從 prompt 批准、opencode 的 saved allow 翻不了 deny、**Claude Code 的 pipeline deny（1a/1d）early-return 於 bypass 分支（2a）之前**——五家一致
2. **dangerous mode 是「擴大自動批准範圍」而非「移除檢查」**：omp 擴 tier 上限；opencode 自動代答 once；引擎流程不變
3. **fail-closed**：未知工具/tier、無 UI、解析失敗一律取最嚴格結果
4. **bash 比對必須先切複合命令**，且 allow 語義要比 deny 嚴格
5. **硬編碼 critical pattern 當最後底線**，連使用者設定都蓋不掉

## rivumi 現況對照

已有：
- `ToolEffect` READ/MODIFY/EXECUTE 三層 tier（`src/rivumi/approvals.py:16-21`）≈ omp 的 tier
- `effect_for_tool` 未分類即 raise（fail-closed，`approvals.py:146-152`）
- `HeadlessApprovalPolicy` 按 tier 決定 allow（`approvals.py:80-93`）——已經是 proto-yolo
- `TTYApprovalPolicy` session-scoped grants（`approvals.py:96-132`）
- `SafePathPolicy` 硬擋 .git / traversal / symlink escape（`src/rivumi/policy.py:55-56,101-105`）——路徑維度的硬禁令已存在
- 弱點：prompt 層面的操作禁令（`prompts.py:9`），僅靠模型自律

缺什麼：
- 沒有 ApprovalMode / dangerous 概念（目前只有 per-policy tier 開關）
- **沒有 deny 層**：現行 policy 只有「allow by tier」與互動 prompt，無法表達「即使 dangerous 模式也禁止 X」
- 沒有指令內容比對（run_check 的命令字串完全沒有 pattern 檢查）
- 沒有 deny > ask > allow 的評估順序概念

## 建議設計（omp 模式為骨架 + codex 的「deny 不可批准」語義）

```
決策順序（每個 tool call）：
1. 硬編碼 CRITICAL_PATTERNS（regex，對 run_check 命令切段後逐段比對）→ 命中即 DENY（不可配置、不可繞過）
2. 使用者 deny 規則（per-tool 名稱 + 指令/path glob pattern）→ DENY
3. 工具自身 effect 宣告（現行 TOOL_EFFECTS）
4. mode tier 比較：default=read / dangerous=read+modify(+execute 視旗標)
5. 落到第 4 步之外 → 交給現行 ApprovalPolicy（TTY prompt / headless DENY）
```

- 新增 `ApprovalMode` enum（如 `default | dangerous`），dangerous = 提高 auto-allow tier 上限，**不改變第 1、2 步的評估**
- deny 規則建議格式沿用 opencode 的 `{tool, pattern, action}` wildcard 思路：resource 對 read/edit 工具是相對 path（可直接複用 SafePathPolicy 的 `_match_path_glob`）、對 run_check 是完整命令字串
- CLI flag 命名慣例：`--dangerous` 主 flag（對齊 opencode 的等價別名做法），文件中明確標註 EXTREMELY DANGEROUS（對齊 codex）；**進入前加安全閘門**（Claude Code 做法）：root/sudo 拒絕、首次使用強制接受條款且紀錄不可被專案層設定偽造（防 RCE）
- 規則語法建議直接採 Claude Code 的 `Tool(content)` 三態（prefix `:*` / wildcard `*` / exact）——已成業界事實標準，使用者遷移成本最低
- 保持 fail-closed：未知工具名稱在 dangerous 模式下同樣走完整評估（不因模式而跳過）
