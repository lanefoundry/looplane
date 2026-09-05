# Wave 0 M12/M13 boundary lock

## Principle

- Modularization can run in parallel with M13 planning docs, but **this Wave 0 只封裝邊界，不改動 runtime 行為約定**。
- Runtime 行為（協定、工具執行時序、provider/API 流程、session 生命周期）不得在本輪重排中變更。

## Allowed scope in Wave 0

- 解析 import graph。
- 產生 baseline 測量（ruff/pytest/startup/build）。
- 安全抽離兼容 façade/compat 契約。
- 供 `loop.py` / `subagents.py` 解除循環的拆解規劃（不改行為）。
- 加入/更新 import-boundary 測試（若已有對應測試骨架）。

## Prohibited in Wave 0

- 不接入/切換 M13 runtime provider 實作。
- 不更改 runtime schema、事件協定、tool scheduling、approval 機制核心邏輯。
- 不改變 `M12` 已建立的 startup lazy-loading 成本前提。

## Exit condition for handoff

- 在 W0 完成後，可安全進入 Wave 1 時，應有明確承諾：Wave 1 僅處理 UI/CLI/架構邊界拆解，仍不改 runtime 行為語意。
