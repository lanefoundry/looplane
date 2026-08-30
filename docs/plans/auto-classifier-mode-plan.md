# Auto（AI Classifier）Approval Mode 設計草案

> 日期：2026-08-26　狀態：討論中，未實作
> 前置：`.research/dangerous-mode-and-deny.md`（五家 dangerous mode 研究）、`docs/research/approval-mode-comparison.md`（完整 mode 光譜比較）
> 參考實作：Claude Code `auto` mode（`claude-code-source/src/utils/permissions/permissions.ts:518-660`，ant-only、`TRANSCRIPT_CLASSIFIER` feature flag 鎖住）

## 概念

在 deny-first pipeline 之上加一層 LLM classifier，逐案判斷每個操作該 allow / deny / escalate，
取代人工 prompt。這是「safe dangerous」：比 `--dangerous` 的全面自動批准保守，
比純規則更有彈性。

權限光譜：

```
plan → acceptEdits → auto (AI 判斷) → bypassPermissions / --dangerous (全自動)
```

## Pipeline 位置

```
1. critical floor        → DENY   （不變，硬編碼 regex，classifier 無權翻案）
2. 使用者 deny rules     → DENY   （不變，Tool(content) 三態）
3. 靜態升級規則（新）     → 強制 ASK（deterministic escalation，見下）
4. classifier            → ALLOW / DENY / ESCALATE
5. fallback              → 原 policy（TTY prompt / headless tier gate）
```

原則（沿用 Claude Code）：
- Classifier 只**取代 ask 分支**——deny 層與 critical floor 照走，它沒資格放行被禁止的操作
- 判不準就降級回人工（ESCALATE），寧可多問不可錯放
- Claude Code 連自家版本都用 feature flag 鎖住——此功能上線時同樣應掛實驗旗標

## 待決問題

### 1. Classifier 用哪個模型？
- (a) 同 session 模型：零額外設定；但每次工具呼叫多一次 LLM 往返，延遲直接疊加
- (b) 另指定便宜快的模型（haiku 類）：延遲可控，多一個設定項
- **現階段傾向 (b)**：`--classifier-model` 可選；未給则用 session 模型

### 2. 失敗語義（fail-closed 的「close」是什麼）？
Classifier 逾時／輸出解析失敗／API 掛掉：
- TTY 情境：降級到 ask 合理
- headless 沒人可問：只能 deny，但會讓 CI 變脆
- 建議提供明示選項 `--on-classifier-error=ask|deny|allow`，預設 deny

### 3. 第 3 層靜態升級規則要不要先做？（建議：先做這個）
不需要 LLM 就能擋的高風險訊號，直接跳過 classifier 強制人工：
- 單次 patch 超過 N 行
- 刪除超過 M 個檔案
- 觸碰 lockfile / CI 設定 / `.github/` / `.git*`
- run_check 以外的任何 EXECUTE

便宜、可預測、可測試，解決大部分「--dangerous 太肥」的問題。
**建議路徑：先做靜態層 + `ApprovalMode.AUTO` 骨架（classifier 先永遠 ESCALATE），再接真 classifier。**

### 4. 輸入邊界與隱私
Classifier 會看到 patch 內容＝把程式碼送給另一個模型。
用 session 模型沒差；指定外部便宜模型時是隱私考量，文件必須講明。

### 5. Audit 軌跡
Classifier 的 decision + reason 進 `approval.resolved` 事件與 manifest
`approval_history`，與人工決策同一軌跡（新增 decision source 欄位：
`human | classifier | rule | critical_floor`）。

## 實作落點（對應現有程式碼）

- `src/looplane/permissions.py`：`ApprovalMode` 加 `AUTO`；`PermissionGuard.pre_decision`
  在 deny 層之後插入靜態升級檢查與 classifier hook
- `src/looplane/approvals.py`：`ApprovalDecision` 可能需加 `ESCALATE`（或沿用回 None 落到 fallback）
- `src/looplane/cli.py`：`--classifier-model`、`--on-classifier-error` 旗標
- 靜態升級規則的輸入來源：`ApprovalRequest.preview` 已有 bounded patch 文本；
  刪除檔案數需從 apply_patch / replace_text 參數統計

## 相關上游證據

| 專案 | 證據 |
|---|---|
| Claude Code auto mode | `types/permissions.ts:28-38`（InternalPermissionMode 含 `auto`）、`permissions.ts:518-660`（classifier 決策邏輯） |
| codex not-so-yolo | `codex-rs/utils/cli/src/shared_options.rs:43-50`（`--approve-for-me` alias `not-so-yolo`） |
| omp write tier | `oh-my-pi/packages/coding-agent/src/cli/args.ts:279-280`（`--approval-mode write` = tier 上限到 write） |
