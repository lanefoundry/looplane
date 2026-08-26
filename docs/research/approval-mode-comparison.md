# Coding Agent Permission/Approval Mode 完整比較

> 日期：2026-08-26　範圍：Claude Code、oh-my-pi(omp)、opencode、codex、pi
> 相關：`.research/dangerous-mode-and-deny.md`（deny 機制）、`docs/plans/auto-classifier-mode-plan.md`（auto mode 草案）
> 證據路徑前綴：claude-code-source = `~/Projects/coding-agent-reference/claude-code-source/src/`

## 核心框架：mode 是「自動批准的天花板」，不是開關

所有專案的 mode 都回答同一個問題：**哪些 effect tier 可以不通過人工就執行？**
差別只在表達方式（enum mode / tier 上限 / rule 引擎 / 二維矩陣）。

另一條正交軸是 **非互動情境的 fail 行為**：問不了人時要 deny 還是全放行。

## 一、各家 Mode 全景

### Claude Code — enum mode，Shift+Tab 即時循環

| mode | 自動批准範圍 | 證據 |
|---|---|---|
| `default` | 無；全部走 prompt | `utils/permissions/permissions.ts:1299-1310` |
| `acceptEdits` | working dir 內檔案寫入（Edit/Write/Notebook）+ Bash 白名單子集（mkdir/touch/rm/rmdir/mv/cp/sed）；dir 外 fall through 到 ask | `filesystem.ts:1360-1375,1395-1411`、`tools/BashTool/modeValidation.ts:7-50` |
| `plan` | 唯讀：寫入不自動放行 + 系統提醒層強制「MUST NOT edit」；plan file 本身可寫；bypass session 進 plan 保留 bypass | `pathValidation.ts:199-211`、`messages.ts:3227`、`filesystem.ts:244-248`、`permissions.ts:1268-1271` |
| `dontAsk` | ask → deny（只有預先 allowlist 能過）；保證永不阻塞等人工；不在 UI 循環，SDK/settings 專用 | `permissions.ts:503-517`、`coreSchemas.ts:346` |
| `bypassPermissions` | pipeline step 2a 自動代答 allow；deny/ask/safety 層仍生效 | `permissions.ts:1262-1281` |
| `auto`（ant-only） | LLM classifier 取代 prompt | `permissions.ts:518-660` |

Mode 切換 UX：
- 循環順序 `default → acceptEdits → plan → (bypass) → default`（`getNextPermissionMode.ts:38-78`），shift+tab 觸發（`keybindings/defaultBindings.ts:30`）
- **session 中可即時切換**；進 plan 會 stash 原 mode 到 `prePlanMode`（`permissionSetup.ts:1458-1489`）
- 模型也能主動請求進 plan（需用戶批准對話框，`EnterPlanModePermissionRequest.tsx:28`）

Plan mode 退出協議（值得抄的互動設計）：
- 模型寫完 plan 呼叫 `ExitPlanMode` tool（可附 `allowedPrompts` 語義權限請求如「run tests」）（`ExitPlanModeV2Tool.ts:64-89`）
- 用戶批准時直接選目標 mode：acceptEdits / default / bypass（`ExitPlanModePermissionRequest.tsx:430-471`）；拒絕＝留在 plan 並附 feedback

### oh-my-pi — tier 上限式 mode

| mode | tier 天花板 | 證據 |
|---|---|---|
| `always-ask` | read | `tools/approval.ts:37-41` |
| `write` | write | 同上 |
| `yolo` | exec（但 user deny rules 仍權威） | 同上 + `approval.ts:136-154` |

- CLI：`--approval-mode always-ask|write|yolo`、別名 `--auto-approve`/`--yolo`（`cli/args.ts:279-280`）
- 變體 `--plan-yolo`：先唯讀 plan、自動批准 proposal、切目標模型執行（`session/prewalk.ts:84-90`）——omp 版的 plan→execute 協議
- Subagent 強制 yolo 但保留使用者政策（parent task 批准就是授權邊界）（`task/executor.ts:899-901`）

### opencode — 沒有 mode enum，用「agent + rules」組合表達

| 概念 | 實現方式 | 證據 |
|---|---|---|
| plan | 內建 `plan` agent：edit 全 deny、僅 plans/*.md allow、禁 general 子agent | `packages/opencode/src/agent/agent.ts:156-181` |
| read-only | 內建 `explore` subagent：全 deny 再白名單 grep/glob/list/read/bash/webfetch | `agent.ts:196-212` |
| 一般 | build agent：預設 `"*": "allow"` 但 external_directory=ask、.env read=ask | `agent.ts:119-136` |
| 進出 plan | `plan_enter`/`plan_exit` 本身是 permission action（預設 deny，build 開 enter、plan 開 exit）；退出時問「switch to build?」注入 synthetic message | `agent.ts:127-128,164`、`tool/plan.ts:30-75` |
| yolo | run/TUI 的 `--auto` ≡ `--yolo` ≡ `--dangerously-skip-permissions`：監聽 asked 事件代答 "once"，引擎照常 evaluation | `cli/cmd/run.ts:242-256,798-822` |
| 非互動 | 注入 rules 把 question/plan_enter/plan_exit 一律 deny；無 --auto 時 permission 自動 reject | `run.ts:430-448,810-820` |

啟示：opencode 證明「mode」可以被 rules+agent 定義完全取代——同一套 rule engine 表達 plan/read-only/yolo，不需要特製 enum 分支。

### codex — 二維矩陣：approval policy × sandbox capability

唯一把「問不問人」和「能做什麼」拆成兩個獨立維度的專案：

| AskForApproval | 語義 | 證據 |
|---|---|---|
| `unless-trusted` | 非信任專案的指令除非 exec policy 明確允許否則 prompt | `codex-rs/protocol/src/protocol.rs:929` |
| `on-request`（預設） | 模型自行決定何時求助；沙箱會擋的操作才升級 prompt | `protocol.rs:932-934`、`core/src/exec_policy.rs:753-814` |
| `granular` | 五個子開關（sandbox_approval/rules/skill_approval/request_permissions/mcp_elicitations），false = 自動拒絕而非顯示 | `protocol.rs:941-964` |
| `never` | 永不問；失敗直接回給模型，**靠沙箱兜底** | `protocol.rs:946`、`exec_policy.rs:770-773` |

Sandbox 維度：`ReadOnly / WorkspaceWrite / ExternalSandbox / DangerFullAccess`（`protocol.rs:1010-1058`）。
`--yolo` ＝ 兩維一起拉滿（DangerFullAccess + Never）。企業管控可限制可用 policy 集（`config_requirements.rs:170,917`）。
變體 `--approve-for-me`（alias `not-so-yolo`）：auto review 的中間檔（`shared_options.rs:43-50`）。

### pi — 沒有 mode；信任邊界在載入時

- 工具永遠直接執行；read-only 靠工具集組合：SDK 匯出 `createReadOnlyTools`（read/grep/find/ls，`core/tools/index.ts:204-211`），CLI 用 `--tools read,grep,find,ls` 白名單達成（`args.ts:297,378`）
- 唯一的 mode-like 保護是 project trust（headless/取消一律 fail-closed 不信任，`core/project-trust.ts:86-95`）
- 注意：`createReadOnlyToolDefinitions` 是 dead export，沒接 CLI flag

## 二、跨專案歸納

### Mode 階梯（等效映射）

```
唯讀規劃          檔案編輯自動        高度自動            全放行
─────────────────────────────────────────────────────────────
CC  plan    →    acceptEdits    →   auto(classifier)  →  bypassPermissions
CC  dontAsk：特殊位置——「永不問人」而非「更自動」，ask 直接轉 deny
omp  always-ask  →   write          →                     yolo
oc   explore     →   build agent    →                     --yolo
cx   unless-trusted/on-request       →   not-so-yolo      →  yolo(policy=never+sandbox=full)
```

### 共同鐵律（與 deny 研究一致）

1. Mode 只影響「要不要問」，**永不影響 deny 層**
2. 非互動情境有明確策略：dontAsk（deny）/ auto-reject / headless fail-closed——沒有一家在問不了人時默默放行
3. Plan mode 都是「提示層 + 權限層雙重」：光靠 prompt 約束不夠，寫入路徑實際被擋
4. Session 中可切換 mode，且 plan 退出都有正式協議（ExitPlanMode / plan_exit tool），不是靠模型自律
5. 危險端入口全部有閘門（root 拒絕、條款確認、org kill-switch）

## 三、rivumi 對照

現況：
- 無 mode enum；`HeadlessApprovalPolicy` 的 tier 開關 ≈ omp 的天花板概念（`approvals.py:80-93`）
- `--unsafe-local-exec` ≈ execute tier 的閘門
- `--dangerous`（本次新增）≈ acceptEdits+bypass 之間：read/modify 自動、execute 保留閘
- 無 plan / read-only / dontAsk 對應物

建議 ladder（若要補齊）：

| rivumi mode | 對標 | 語義 |
|---|---|---|
| （現有 default） | CC default | 全部走 policy |
| `--dangerous`（已做） | CC acceptEdits+ / omp write | read+modify 自動，execute 過閘 |
| classifier `AUTO`（草案） | CC auto / cx granular | classifier 取代 prompt |
| 真 yolo | CC bypass / cx never | 連 execute 都自動（需併 `--unsafe-local-exec` 語義並加進入閘門） |
| plan / read-only | CC plan / oc explore agent | 唯讀工具集 + 提示層約束 |
| headless dontAsk | CC dontAsk / oc auto-reject | 問不了的場合 deny 而非掛住 |

最小改動順序建議：dontAsk 語義最便宜（headless 已接近，只差命名與文件）→ plan/read-only（工具集過濾，rivumi 的 TOOL_EFFECTS 已有分類基礎）→ classifier AUTO（見草案）→ 真 yolo。
