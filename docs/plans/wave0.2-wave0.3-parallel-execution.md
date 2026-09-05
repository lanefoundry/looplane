# Wave 0.2 / 0.3 並行起始卡

## 已啟動事項

- `W0-05 loop.py ↔ subagents.py` 硬循環：改成動態/延遲引用，維持行為不變且不再形成 import SCC。
- `W0-06` 載入邊界 smoke：沿用既有 `tests/test_lazy_imports.py`，需在每輪變更後補跑。

## 並行切片（可同時進行）

### Task A（核心） — 解除 loop/subagents 循環
- owner: platform core
- files:
  - `src/looplane/loop.py`
  - `src/looplane/subagents.py`
- deliverable:
  - `_run_dispatch_subagents` 與 `run_subagent_task` 使用 `importlib.import_module("looplane.subagents")` / `importlib.import_module("looplane.loop")` 的延遲載入。
  - `from looplane.loop import AgentRunner` 不再出現在 `subagents.py`。
  - 結果預期：import graph 不再報 `looplane.loop` ↔ `looplane.subagents`。
- acceptance:
  - 以既有 import graph 腳本重跑後不再有兩節點 SCC（loop ↔ subagents）。
  - `pytest -q tests/test_lazy_imports.py` 仍通過（不要求本次即時執行）。

### Task B（驗證） — W0 Gate 更新
- owner: 平台工程
- files:
  - `docs/plans/repository-modularization-uncertainties.md`
  - `docs/plans/runs/wave0-gate-2026-09-05.md`
  - `docs/plans/runs/import-graph-m0.md`
- deliverable:
  - W0-05 更新為「done」條件與驗收痕跡。
  - `import-graph-m0.md` 記錄最新 cycle 結果與時間戳。
- acceptance:
  - W0-05 可由文檔直接證明已完成，且附對應 artifact 檔名。

### Task C（觀測） — 行為回歸快照
- owner: QA
- files:
  - `docs/plans/runs/ruff-baseline-wave0-2026-09-05.json`
  - `docs/plans/runs/pytest-baseline-wave0-2026-09-05.txt`
  - `docs/plans/runs/wave0-lazy-load-smoke-2026-09-05.txt`
  - `docs/plans/runs/startup-baseline/*`
- deliverable:
  - 新增一個 `Wave 0.2` 輕量回歸記錄（可沿用既有檔名時加上 `wave0.2` 後綴）。
- acceptance:
  - 三類觀測（lint / startup / lazy-load）皆有明確「前/後」對比條目。

## 同步節點（每個任務完成即匯流）

1. A 完成後先重跑 import graph。
2. B/C 在 A 結果穩定後補齊紀錄，避免互相阻塞。
3. 所有完成欄位具備 artifact 後，更新 Wave 0 gate 進入下一階段。
