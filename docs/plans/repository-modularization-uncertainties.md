# Repository modularization: 不確定與執行澄清文件

本文用於補齊 [repository-modularization-plan.md](repository-modularization-plan.md) 的「可直接開始」條件，降低執行中的反覆停滯。

## 0) 先行結論

- 計畫可以啟動，但**不該直接進入 Wave 2 以上**。
- 建議先把以下 6 項不確定性收斂到「是/否」決策，再開始實作切片。
- 專案目前仍有跨模組循環、進度與行為邊界未完全對齊，最安全的做法是先走 **Wave 0 → Wave 1 少量切片**。

## 1) 目前不確定的地方（優先順序）

1. **前置狀態是否足夠**
   - 未先完成「可信 baseline」會把 refactor 噪音和既有缺陷混在一起。
   - 需先鎖定：`ruff`、`pytest`、`build`/startup 的現況值，並確認可回滾差異可追蹤。
   - 這一點關係到所有 Wave 的「可否進入」判斷，不解清就不能做進一步模組化承諾。

2. **`loop.py` 與 `subagents.py` 的循環 import 仍在**
   - 現況仍存在 `loop.py` 對 `subagents.py` 的 import（延後式）及 `subagents.py` 對 `AgentRunner` 的 import。
   - 在 Wave 2 計畫中明確要求拆除，這是高優先級的阻塞條件，未清除前不能宣告 Wave 2 對齊目標。

3. **模組邊界規則是否已定義到可執行**
   - 計畫有 target package shape，但尚缺「每個模組可 import 哪個方向」的硬規則清單到測試層級。
   - 目前多點仍是 `loop.py`/`cli.py`/`sdk.py` 透過重複延後式 import 走相似職責，容易在切片中造成 hidden coupling。

4. **TUI 行為穩定性未形成可回歸清單**
   - `tui.py` 仍是單體與高耦合核心，focus/input/routing / async writer / generation fence 風險在文檔中被列為高風險。
   - 目前未有一份「重構前行為契約」(focus 鍵、esc/enter/方向鍵、取消邏輯、訊息可見順序等) 的最小清單。

5. **M12 / M13 交錯造成的決策衝突**
   - `progress.md` 顯示 M12、M13 仍有未完成項；M13 runtime 分支與模組化重構有重疊點。
   - 若不先訂定「模組化不改 runtime 接口」的邊界，會在兩邊同時改同一 contract。

6. **對外相容出口與升級路徑不夠明確**
   - 目前兼容出口（例如 `looplane.tui` / `looplane.loop` / `looplane.tools`）需要清楚定義是否保留 full compatibility、限縮 compatibility、或逐步淘汰。
   - 未定義時，重構可能只在內部通過、但外部呼叫中斷。

## 2) 各不確定性的「澄清輸出」格式（建議）

- 為每一項補上三件事：
  - `判斷`：可否在本次切片前一次完成（是/否）
  - `可接受邏輯`：採取哪個實作版本才算通過
  - `失敗條件`：若發生哪些症狀就撤回（例如：focus 事件順序變動、啟動回歸、對話無法 resume）

## 3) 建議的最小啟動文檔（先做）

- [ ] 建立 `modularization-launch-decision.md`（共用）
  - baseline 指標
  - import graph 目標/非目標
  - M12/M13 變更邊界
  - compatibility contract 簽核
- [ ] 先完成 Wave 0 的 0.1 + 0.2 + 0.3
  - 不做行為新增，只做可驗收的結構約束與 smoke
  - 將 `loop.py`/`subagents.py` 循環移除規劃進 0.3 的第一個驗收項
- [ ] 進入 Wave 1 前確認
  - 每 slice 前一律有「行為不變清單」
  - 每 slice 後執行 startup lazy-import、關鍵流程 smoke

## 4) 一個可以直接複用的執行判斷規則

- 如果同一個切片在兩個 slice 邊界都沒有明確 owner，就暫停並先補充澄清。
- 若任何切片無法保證「無行為變更」條件，該 slice 改為待定，先轉回 Wave 0 的約束條目中。
- 沒有可觀測驗證的風險都視為不確定，不得當作可完成事項。

## 5) 下一步（你要不要我直接接著做）

我可以直接接著把這份文件展開成「執行版清單版」：
1. 將每一項不確定性拆成 `owner / due / acceptance criteria / rollback trigger`
2. 對應到 `repository-modularization-plan.md` 的每個 Wave 切片
3. 直接產生一份可貼到 PR description 的「開始前核對表」

## 6) 可直接執行版（立即用於開工）

### 6.1 開始條件（Wave 0 入口）

- [ ] `W0-01` baseline 鎖定：`ruff`、`pytest`、`startup`、`build` 四類指標產生 baseline 檔
  - owner: 平台工程
  - acceptance: docs/plans/runs 內有可追溯時間戳、指標值與對照說明
  - rollback trigger: 基線缺欄位或不可重跑

- [ ] `W0-02` import graph 鎖定
  - owner: 架構
  - acceptance: 明列 SCC，明確標出 `loop.py` ↔ `subagents.py` 為 must-fix
  - rollback trigger: 無法穩定重建 import graph

- [ ] `W0-03` compatibility 出口約束定稿
  - owner: 產品與工程共識
  - acceptance: `looplane.tui/loop/tools` 的兼容策略有版本與遷移期限
  - rollback trigger: 對外契約不完整

- [ ] `W0-04` M12/M13 工作邊界上鎖
  - owner: 架構
  - acceptance: 本輪重構不改 runtime 行為協定，明列允許改動清單
  - rollback trigger: 出現 runtime 行為修改混入

### 6.2 開工 Gate

- [x] `W0-05` `loop.py` ↔ `subagents.py` 循環 import 被納入第一輪 ticket（非旁路）
  - owner: 核心工程
  - acceptance: 逐步拆除後確認不依賴反轉；實測 `looplane.loop` ↔ `looplane.subagents` 不再形成 SCC
  - rollback trigger: 無法以非行為外掛方式移除

- [x] `W0-06` 載入邊界 smoke 測試就緒
  - owner: 平台工程
  - acceptance: 主要入口可測到重型 SDK 未被不當 eager-load
  - rollback trigger: CLI/TUI 啟動時間退化超過 baseline 允許值

### 6.3 Wave 1/2 開工前

- [ ] `POST-W0` 列出 TUI 不變行為清單（focus、輸入、取消、訊息順序、編輯/只讀切換）
  - owner: TUI owner
  - acceptance: 每項可回歸驗證
  - rollback trigger: 缺少一項關鍵輸入契約

- [ ] `POST-W0` 每一 slice 綁定「行為不變清單 + 驗證命令」才能關閉
  - owner: 專案管理
  - acceptance: 任何 slice 未掛上清單不得合併
  - rollback trigger: 缺項無法補齊

### 6.4 我可以幫你直接接的下一步

- 直接幫你把這 6 項整理成：
  - `docs/plans/repository-modularization-launch-checklist.md`
  - 每項對應到 `repository-modularization-plan.md` 的 0.1、0.2、0.3、1.1~1.5、2.x slice
  - 含 `owner/ETA/acceptance` 欄位

如果你回「開始」，我就按這個版本直接產出第二版。

## 6.5 Wave 0 實作結果（2026-09-05）

- Wave 0 的基線與約束文件已完成，對齊到 `docs/plans/runs/wave0-gate-2026-09-05.md`。
- 目前狀態：**基線已建立、邊界已定義，但仍有既有技術債需在 Wave 0.2/0.3 清理**（特別是 `loop.py` ↔ `subagents.py`）。

已完成對應項：
- [x] `W0-01` baseline 鎖定（ruff/pytest/startup/build）
- [x] `W0-02` import graph 鎖定
- [x] `W0-03` compatibility 約束草案
- [x] `W0-04` M12/M13 邊界草案
- [x] `W0-05` 循環 import 修正（`loop.py` ↔ `subagents.py`）
- [x] `W0-06` 載入邊界 smoke（`tests/test_lazy_imports.py` + `docs/plans/runs/wave0-lazy-load-smoke-2026-09-05.txt`）
