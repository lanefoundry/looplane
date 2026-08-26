# docs/ 整合建議（只分析，未搬未改）

產出日期：2026-08-25 · 分析範圍：`docs/` 全部 134 個 `.md`
方法：全檔清單（大小/日期）→ 逐檔抽讀開頭判定主題 → 追蹤交叉引用（`grep`）確認沒有外部依賴後才列入歸檔/合併候選。

---

## 一、建議合併（同主題多份 → 單一文件）

### M1. source-filesystem-changed 事件鏈（5 → 1）
| 檔案 | 行數 | 角色 |
|---|---|---|
| `diagnoses/source-filesystem-changed-investigation.md` | 40 | 同一事件的初步診斷 |
| `diagnoses/run-fail-diagnosis.md` | 65 | 同一根因的深入診斷（重複列舉同一批 drift 檔案與時間戳） |
| `diagnoses/source-invariant-fix.md` | 23 | 該事件的修復清單 |
| `diagnoses/screenshot-failure-analysis.md` | 55 | 同一次失敗 turn 的截圖證據分析 |
| `diagnoses/claude-code-source-invariant-research.md` | 24 | 純指標文件，內文只有一句「See research/2026-08-22-claude-code-file-conflict-architecture.md」 |

- **合併目標**：`diagnoses/source-invariant-incident.md`（症狀 → 根因 → Claude Code 對照 → 修復 → 截圖證據，按時間序分節）。
- **理由**：五份文件描述**同一事件**（2026-08-22 native conversation `source filesystem changed` 連環失敗），investigation 與 run-fail-diagnosis 的證據段落幾乎逐字重複；cc-research 是零內容轉址。
- **合併後動作**：新文件保留指向 `research/2026-08-22-claude-code-file-conflict-architecture.md`（該份仍獨立保留，被本事件引用且含 Claude Code 架構結論）；刪除原 5 檔。

### M2. nvidia-nim 事件（2 → 1）
| 檔案 | 行數 |
|---|---|
| `diagnoses/nim-500-diagnosis.md` | 81 |
| `diagnoses/tui-live-smoke-report.md` | 94 |

- **合併目標**：`diagnoses/nvidia-nim-retry-hardening.md`。
- **理由**：同一天（08-24）、同一 provider、同一修復線：nim-500 診斷 5xx 無 retry 根因，smoke report 正是該 retry 修復的 live 驗收報告（文中明言「注入 flaky httpx MockTransport 驗證 retry」）。progress.md 已有「Provider retry hardening (nvidia-nim)」摘要，兩檔合併不會遺失脈絡。
- **合併後動作**：smoke report 作為「驗證證據」章節併入；刪除原 2 檔。

### M3. Otter spinner / 動畫（3 → 1，跨目錄）
| 檔案 | 位置 | 行數 |
|---|---|---|
| `otter-animation-cadence.md` | diagnoses/ | 26 |
| `patchotter-spinner-plan.md` | plans/ | 7 |
| `2026-08-22-otter-terminal-animation-references.md` | research/ | 52 |

- **合併目標**：`diagnoses/otter-spinner-animation.md`（以 otter-animation-cadence 為底）。
- **理由**：spinner 實作計畫（已完成）、cadence 調整結果、動畫參考調研是同一個小功能的三個碎片；plan 與 reference 各自 <60 行。
- **合併後動作**：三合一；刪除原 3 檔。

### M4. Capability audit 計畫併入其成果
| 檔案 | 行數 |
|---|---|
| `plans/capability-current-state-audit-plan.md` | 10 |

- **合併目標**：`research/2026-08-22-capability-current-state-audit.md` 開頭加一段「Plan (completed)」。
- **理由**：plan 只是 5 條全勾選的 checklist，audit 成果文件完整涵蓋相同範圍。
- **合併後動作**：併入後刪除 plan 檔。

### M5. Milestone 排程碎片併入 progress.md（2 → 0）
| 檔案 | 行數 |
|---|---|
| `diagnoses/summary.md` | 68 |
| `diagnoses/milestone-reschedule.md` | 73 |

- **合併目標**：`progress.md`（M12/M13 章節已存在且更完整）。
- **合併後動作**：核對 unique 內容（如 CI gate 決策細節）無缺漏後刪除 2 檔；README.md 目錄同步更新。


### M6. Naming 計畫碎片（4 plans → 隨 A1/A2 歸檔）
`clean-brand-name-plan.md`(14)、`short-euphonic-name-plan.md`(15)、`ottie-otti-clearance-plan.md`(7) 三份全是已完成 checklist，對應的調研成果都在 research/ naming 系列。
- **建議**：直接歸檔（見 A1），不必另存合併檔；唯一值得保留正文的是 `rivumi-project-rename-plan.md`（記錄相容性邊界決策，rename 已執行）。

### M7.（附帶發現）`stages/README.md` 目錄落後
`stages/` 實有 13 檔，但 README 的 Records 只列到 M9、Pending 只列 M10/M11——**`m12-onboarding-credential-verification.md` 未被收錄**。整合時應一併補上（M12 已 closed）。

---

## 二、建議歸檔／刪除

> 建議統一搬到 `docs/archive/`（保留 git 歷史，不必真刪）。以下均已 grep 確認無活躍文件引用。

### A1. Naming/branding 調研系列 → `docs/archive/naming/`（29 檔）
全部為 2026-08-21~22 的一次性命名調研；專案已定名 **Rivumi** 並完成 rename（`rivumi-project-rename-plan.md` 全勾選），整條探索線已被最終決定取代：

```
research/2026-08-22-animal-project-package-names.md    research/name-check-packages.md
research/name-check-projects.md                        research/otter-explicit-name-check.md
research/2026-08-22-clean-otter-brand-shortlist.md     research/coined-otter-name-ideas.md
research/coined-finalists-language-comparison.md       research/coined-finalists-project-screen.md
research/coined-finalists-trademarks-global.md         research/coined-finalists-trademarks-taiwan.md
research/euphonic-otter-name-screen.md                 research/euphonic-lutuno-project-screen.md
research/euphonic-lutuno-trademark-global.md           research/euphonic-lutuno-trademark-tw.md
research/short-name-brand-ideas.md                     research/short-name-language-ideas.md
research/short-name-namespace-scout.md                 research/ultrashort-name-brand-ideas.md
research/ultrashort-name-language-ideas.md             research/five-letter-name-project-screen.md
research/rivumi-nuvimi-language-screen.md              research/rivumi-nuvimi-trademark-global.md
research/rivumi-nuvimi-trademark-tw.md                 research/otter-name-language-screen.md
research/2026-08-22-short-euphonic-otter-names.md      research/2026-08-22-ottie-otti-clearance.md
research/ottie-otti-packages-projects.md               research/ottie-otti-trademarks-global.md
research/ottie-otti-trademarks-taiwan.md
```
（另加 plans/ 的 3 份 naming plan，見 M6/A2）
- **理由**：純決策過程紀錄；結論只剩「Rivumi 通過 screen」一條有長期價值，可在 archive 放一份 10 行 INDEX.md 註記最終結論。
- **注意**：這批互相引用但無外部引用者（grep 已驗證）；`five-letter-name-project-screen.md` 引用 short-name-* 兩檔，同批一起搬即不破鏈。

### A2. 已完成的一次性 plans → `docs/archive/plans/`（8 檔）
全部 checklist 全勾、成果已由 stages/ 或 progress.md 收錄：
```
plans/ask-agent-mode-plan.md      plans/loading-copy-plan.md
plans/uv-tool-sync-plan.md        plans/approval-context-plan.md
plans/claude-code-ui-research-plan.md          （+A1 的 3 份 naming plan）
```

### A3. 已完成的里程碑 plans → `docs/archive/plans/`（3 檔）
```
plans/m11-conversation-tui-plan.md   plans/m11-claude-tui-design.md   plans/runtime-onboarding-plan.md
```
- **理由**：M11 已 closed（stages/m11 + progress.md 完整收錄）；design 文件的核心方向也已反映在 stage record。`runtime-onboarding-plan.md` 對應 M8/M10 已完成的上手流程。

### A4. 被取代／封閉的研究與診斷 → `docs/archive/superseded/`（8 檔）
| 檔案 | 理由 |
|---|---|
| `research/subscription-bridges.md` | 文件自我標註 "Superseded design note… Follow-up: provider-bridge-comparison.md"，明確被取代 |
| `research/codex-oauth-implementation.md` | 描述舊 `src/coding_agent/` 時代的實作筆記；現行架構見 stages/m4、m5 |
| `research/interactive-cli-audit.md` | 2026-08-21 對舊 staged worktree 的唯讀 audit，目標早已落地（M2/M9），一次性 |
| `research/onboarding-runtime-model-ux.md` | 決策已落進 runtime-onboarding-plan 與 stages/m8/m10；原文是一次性 UX 判讀 |
| `diagnoses/cloudflare-rivumi-resource-migration.md` | 自標 "Status: closed"；描述的是已刪除的誤建資源 |
| `diagnoses/warp-ui-investigation.md` | 自標 "implemented and verified" 的一次性設定調查 |
| `diagnoses/textual-ime-placeholder-investigation.md` | 針對 Textual 8.2.8 版本的一次性源碼調查，結論已付諸實作 |
| `diagnoses/model-switch-conversation-diagnosis.md` | 自標 "implemented and verified"；行為決策已寫入 conversation.py 與 stage records |

### A5. 直接刪除（1 檔）
| 檔案 | 理由 |
|---|---|
| `diagnoses/claude-code-source-invariant-research.md` | 全文 24 行中實質內容只有一行連結指向 `research/2026-08-22-claude-code-file-conflict-architecture.md`（併入 M1 新文件後即無資訊損失） |

---

## 三、建議保留不動

| 範圍 | 為何保留 |
|---|---|
| `README.md`、`progress.md` | 目錄與唯一 rolling 進度真相來源（歸檔後需同步更新 README 的目錄段） |
| `agent-diff-report.md` + `diagnoses/agent-diff/`（6 檔） | 2026-08-25 最新產出，root report 明言六份 detail 是其支撑；近期仍在用 |
| `opencode-zen-protocol-mismatch.md`、`startup-performance-playbook.md` | 活躍的 root-level 參考（playbook 被 M12 CI gate 引用） |
| `stages/` 全部（m1–m12 + README） | 專案指定的 milestone 唯一工程紀錄格式，互相引用 release review |
| `research/m2–m11-release-review.md`（9 檔） | 獨立審查證據，被對應 stages/*.md 明文引用，不可拆離 |
| `research/m3/m4/m5/m6-live-evidence.md` | 含 hash 的可重驗證證據紀錄，README 明言 raw bundle 在 `.research/evidence/`，此為索引 |
| `research/m3-edit-tool-options、m3-provider-e2e-audit、m4-api-url-implementation、m4-claude-subscription-boundary、m4-codex-live-readiness、m4-provider-completion-audit、m5-claude-coding-backend-design、m5-codex-cli-backend、m5-goal-gap-audit、m6-cloudflare-sandbox-design` | 各 milestone 的設計/邊界研究，stage records 引用（如 m6 design 被 stages/m6:127 引用）；subscription boundary 仍是現行政策依據 |
| `research/provider-bridge-comparison.md`、`2026-08-22-ccswitch-architecture.md`、`2026-08-22-pi-google-subscription.md`、`2026-08-22-coding-cli-landscape.md` | M13 外部 runtime 選型的現行依據（M13 仍在進行中） |
| `research/m11-claude-code-tui-reference.md`、`2026-08-22-claude-code-file-conflict-architecture.md` | TUI parity 與 edit 衝突架構的持續參照（gap analysis 與 source-invariant 事件均引用） |
| `research/2026-08-22-capability-current-state-audit.md` | 最新全局能力盤點（M4 建議將 plan 併入此檔而非刪除它） |
| `plans/m12-startup-performance-plan.md`、`plans/m13-external-coding-cli-adapters-plan.md` | M12 剛關閉（playbook 配套）、M13 仍在進行（Slice 2 open） |
| `plans/runtime-parity-plan.md`、`plans/tui-parity-implementation-plan.md` | 未標記完成的持續性目標；tui-parity 與 gap analysis 構成現行 TUI 改善路線 |
| `diagnoses/approval-scope-diagnosis.md`、`conversational-turn-redesign.md` | 08-25 最新診斷；後者明文引用前者，屬活躍修復線 |
| `diagnoses/m13-stage-report.md` | M13 進行中的 live capture 紀錄，normalizer 實作依據 |
| 其餘 diagnoses fix notes（`terminal-cancel-exit-scrollback-fixes`、`terminal-failure-repair`、`inline-selector-controls`、`runtime-reported-model-fix`、`contextual-command-menu-fix`、`composer-bottom-fix`、`groundlane-codex-child-env-fix`、`codex-subagent-activity-fix`、`tui-vs-claude-code-gap-analysis`、`claude-rewind-behavior-audit`） | 各自對應獨立的已交付修復/分析，無互相重疊；體積小但各有契約內容 |
| `product/onboarding/新增 Provider API Key 流程優化 PRD.md` | product/ 唯一文件，PRD 性質不同於工程紀錄 |

---

## 四、影響評估

| 項目 | 現況 | 異動 | 異動後 |
|---|---|---|---|
| research/ | 64 | −34（29 naming 歸檔 + 4 被取代歸檔 + 1 animation-refs 併入 M3） | 30 |
| plans/ | 18 | −13（8 一次性歸檔 + 3 里程碑歸檔 + 2 併入 M3/M4） | 5 |
| diagnoses/ | 27（不含 agent-diff/） | −11（M1 五合一 −4、M2 二合一 −1、M5 刪 2、A4 歸檔 4） | 16 |
| **合計** | **134** | **−58** | **約 76** |

\* research/ 30 = 64 − 29(naming) − 1(animation refs 併入) − 4(superseded 歸檔)。若把 `m6-cloudflare-sandbox-design` 一併歸檔則再 −1，但因 stages/m6 明文引用，建議保留。

- **淨縮減：活躍文件 134 → 約 76 檔（−43%）**。實體檔面：約 48 檔移入 `docs/archive/`（仍在 repo、git 可尋回）、11 檔內容併入他檔後刪除、新增 3 份合併檔（incident / nvidia-retry / spinner，其中 2 份為原檔改寫）——總實體檔數 134 → 約 124，但活躍工作面只剩 76。
- **必做的配套**：`docs/README.md` 的 plans/diagnoses/research 三段目錄需改寫（提及的檔名近半異動）；archive/ 需附 INDEX.md 說明各批歸檔原因與最終結論（naming → Rivumi）。
- **風險**：低。所有歸檔候選經 grep 確認無 stages/、plans/、根文件的活躍引用（唯一例外 `subscription-bridges.md` 曾被 provider-bridge-comparison 反向標註 superseded，屬單向說明，不改語義）。
