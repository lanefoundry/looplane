# Approval Scope 診斷報告：每則訊息都跳「Run this command?」核准對話框

日期：2026-08-25 · 範圍：`src/looplane/`（TUI 對話、looplane-agent runtime）
驗證方式：程式碼追蹤 + 執行 `TextualApprovalPolicy._grant_scope` / `decide_permission` 實測腳本（結果見各節「實測」）。

---

## TL;DR

| # | 問題 | 結論 |
|---|------|------|
| 1 | 「Allow for this session」的 scope 是什麼？存在哪裡？ | TUI 層：process 內 `looplaneApp._approval_session_grants`（不落盤）；Runner 層：單一 run 的 `SessionManifest.granted_effects` |
| 2 | 同一對話下一則訊息（新 run）還有效嗎？ | **驗證指令（final verification）：有效**（scope 穩定）。**模型呼叫的 `run_check`：無效——這是 bug**，grant 存在一次性 `action:<tool_call_id>` scope 下，永不匹配 |
| 3 | 為什麼每則訊息都跑 `check-1`？ | 兩條路徑：harness 在**每個 run 結束前強制** `_verify_all`（設計如此）；加上 system prompt 鼓勵模型自行呼叫 `run_check`（模型選擇） |
| 4 | `git diff --check` 有 allowlist 嗎？verification 核准路徑與一般 tool 相同嗎？ | 沒有 argv 內容判斷（唯讀指令也歸類 EXECUTE）；核准路徑相同（都走 `_approval`），只差 reason 與 scope 穩定性 |

---

## 1. 核准機制實作與 session grant 的存放位置

核准有**兩層**：

### 第一層：AgentRunner 內（per-run）
- `src/looplane/loop.py:259-338` `_approval()`：
  - `loop.py:278-293`：若 `self._manifest.granted_effects` 已含該 effect → 直接回 `ALLOW_ONCE`（事件 `approval.reused`），不再問。
  - `loop.py:310`：否則呼叫 `self.approvals.decide(request)`（即 TUI 傳入的 policy）。
  - `loop.py:313-314`：決策為 `ALLOW_SESSION` 時把 **effect** 加入 `manifest.granted_effects`。
- manifest 是 per-run 的：`AgentRunner.__init__`（`loop.py:74-133`）每次建新 runner 都有新的 run_id / manifest。`resume()`（`loop.py:157+`）才會還原舊 grant。

### 第二層：TUI process 層（跨 run）
- `src/looplane/tui.py:1892-1894`：`self._approval_session_grants: set[ProcessLocalGrant]`，註解明言「ALLOW_SESSION lasts until this full-screen looplane process exits, including subsequent bounded tasks. It is never persisted to disk.」
- `src/looplane/tui.py:404-438` `TextualApprovalPolicy.decide()`：
  - 先用 `decide_permission(mode, effect, scope, grants)` 查 process 層 grant（`tui.py:425-430`）；
  - `tui.py:436-437`：使用者選 ALLOW_SESSION → `self._session_grants.add(ProcessLocalGrant(effect, scope))`。
- Grant 的 scope 計算：`tui.py:409-421` `_grant_scope()`：
  - tool_call 有 `grant_scope` 參數 → 用它（external runtime 提供，見 `conversation_controller.py:63`）;
  - `external_agent` tool → `external_agent:<backend>`;
  - **有 `command` → `"command:" + "\u0000".join(argv)`（驗證指令路徑，穩定）**;
  - 其他 → `None`，由 `tui.py:424` fallback 成 **`f"action:{request.action_id}"`**。
- 匹配邏輯：`src/looplane/runtime_semantics.py:173-203` `decide_permission()`——grant 需 effect 與 scope **完全相等** 才 ALLOW。

### TUI 對話流程
每則使用者訊息 → `_run_agent`（`tui.py:3187`）→ `runner_factory`（`tui.py:3241-3245`，每則訊息 new 一個 `TextualApprovalPolicy`，但共用同一個 `_approval_session_grants` set）→ `cli.py:615 make_runner` → `cli.py:689` **new 一個 `AgentRunner`**（新 run、新 manifest）。預設 runtime 是 `looplane-agent`（`tui.py:2016-2019`）。TaskContract 的 verification 固定帶 `check-1 = git diff --check`（`cli.py:244-249 _commands`，`cli.py:649`）。

## 2. 「Allow for this session」在下一則訊息是否有效？

**結論：分兩條路徑，一條有效、一條是 bug。**

### 路徑 A：harness final verification —— 有效 ✅
- 呼叫點：`loop.py:1016` → `_verify_all`（`loop.py:539-604`）→ `_approval(..., reason=FINAL_VERIFICATION, command=command)`（`loop.py:545-551`）。
- 因為帶了 `command`，`_grant_scope` 回傳穩定的 `"command:\0git\0diff\0--check"`（`tui.py:419-420`），跨 run 完全一致。
- 實測：msg1 選 Allow for this session 後，msg2 同指令 `decide_permission` 回 **allow**，不再彈窗。

### 路徑 B：模型自行呼叫 `run_check` tool —— 失效，每次都問 🐛
- 呼叫點：`loop.py:920-926`：`_approval(action_id=call.tool_call_id, ..., tool_call=call)`——**沒有傳 `command`**，且 `action_id` 就是 provider 給的 `tool_call_id`（每次呼叫都不同）。
- `_grant_scope`：tool_call 無 `grant_scope` 參數、名字不是 `external_agent`、`command` 是 None → 回 None → fallback `action:<tool_call_id>`（`tui.py:424`）。
- 使用者選「Allow for this session」存下的是 `ProcessLocalGrant(EXECUTE, "action:call_aaa111")`；下一則訊息的新請求是 `action:call_bbb222` → **永不匹配** → 再彈窗。
- 實測輸出：
  ```
  msg1 stored scope: action:call_aaa111
  msg2 needed scope: action:call_bbb222
  msg2 decision: ask        ← 「Allow for this session」實際上等於 allow-once
  ```
- 同一 run 內不會重複問，是因為第一層 `manifest.granted_effects`（`loop.py:278`）把整個 EXECUTE effect 都記住了；但新 run 換了 manifest 就失效，而第二層又因 scope bug 救不回來。
- 附帶：使用者看到的標題「Run this command?」正是 MODEL_TOOL + EXECUTE 的文案（`tui.py:697-702` InlineApprovalBlock / `tui.py:523-528` ApprovalModal；final verification 會顯示「Run final verification?」）。所以「一直問」的主體就是這條 bug 路徑。

**根因判斷（Q2）：bug**——`TextualApprovalPolicy._grant_scope` 對內建 tool（尤其 `run_check`）沒有穩定 scope，fallback 到一次性的 `action_id`，使 ALLOW_SESSION 語意退化成 ALLOW_ONCE。

## 3. 為什麼每則訊息都要跑 `check-1`

兩個來源疊加：

1. **harness 強制的 final verification（設計如此）**：每個 AgentRunner run 結束前無條件執行 `_verify_all`（`loop.py:1016`），跑完 `task.verification` 全部指令（至少一條，`contracts.py:58 Field(min_length=1)`；預設即 `check-1 = git diff --check`，`cli.py:245`）。system prompt 也明言「A final answer is accepted only after the harness reruns every check」（`prompts.py:10-11`）。即使使用者只說「hi」，這個 run 結尾仍要過一次 check-1。若使用者上次選的是「Allow once」，每則訊息就會再看到「Run final verification?」。
   - 注意：此路徑的核准只要選過一次「Allow for this session」，同一 TUI process 內後續訊息就靜默放行（見第 2 節路徑 A）——所以這條本身不需要改 code，是使用者操作方式 + 文案不夠清楚的組合。
2. **模型自己選擇呼叫 `run_check`**：`run_check` 是提供給模型的 tool（`tools.py:195-205`，enum 限定 allowlisted 名稱），system prompt 要求「Run declared checks after changes」（`prompts.py:9`）。模型在每個 turn 開頭自行呼叫 → 走到第 2 節的 bug 路徑 → 每次都被問。

## 4. `git diff --check` 的 allowlist / verification 核准路徑

- **沒有唯讀判斷**：`TOOL_EFFECTS`（`approvals.py:135-143`）只按 tool 名稱分類；`git_diff` tool 是 READ，但同一個 `git diff --check` 經 `run_check`/verification 執行時一律歸 `ToolEffect.EXECUTE`，不看 argv 內容。也沒有 verification 指令的預先核准清單。
- **核准路徑相同**：verification（`loop.py:545`）與 model tool（`loop.py:920`）都經 `_approval()` → 同一個 approval policy → 同一個 modal；差別只在 `reason`（影響標題文案）與 scope 穩定性。
- 既有的降問機制只有三種：per-process session grant（本報告主題）、permission mode（`ACCEPT_EDITS` 只自動放行 MODIFY，`runtime_semantics.py:201-202`）、headless 的 `allow_execute` flag（`approvals.py:83-93`）與 CLI `--unsafe-local-exec`（`cli.py:542-548`）。

---

## 最小修復建議（未實作）

針對唯一確認的 bug——`run_check`（以及同類內建 tool）的 session grant scope 不穩定：

**方案（推薦，一行語意修正）**：在 `TextualApprovalPolicy._grant_scope`（`tui.py:409-421`）為 `run_check` 加穩定 scope：

```python
if request.tool_call.name == "run_check":
    name = request.tool_call.arguments.get("name")
    if isinstance(name, str) and name.strip():
        return f"run_check:{name.strip()}"[:4_096]
```

效果：對同一 allowlisted 指令（`run_check:check-1`）選一次「Allow for this session」，之後所有訊息的 model 呼叫與 final verification（可再統一為 `command:` scope 使兩者共用 grant）都不再詢問。

**備選**：在 `loop.py:920-926` 當 `call.name == "run_check"` 時從 `self._executor.verification_commands[name]` 解析出 `VerificationCommand` 一併傳入 `command=`，讓 model tool 與 final verification 共用完全相同的 `command:\0argv` scope（語意最精確：grant 對象是指令而非工具名）。

**非必要但相關的後續討論（非本次修復範圍）**：
- Ask/read-only 對話模式是否應省略 final verification 或改為 READ-only check 子集（目前連「hi」都會觸發 `git diff --check` 執行需求）。
- 「Allow for this session」文案可補充 scope（例如「允許 check-1 直到離開」），避免使用者以為是全域放行。
