# Wave 0 compatibility exit contract (draft)

## Scope

- Keep public import compatibility for:
  - `looplane.tui`
  - `looplane.loop`
  - `looplane.tools`
  - `looplane.codex_app_server`
- No remove/rename behavior in these modules during Wave 0.

## Allowed compatibility strategy

- 以「兼容 facade」保留目前可被外部引用的主要入口函式、類別名稱。
- 新模組可以在 `src/looplane/*` 新 package 中成為 canonical owner，但舊模組必須 delegate 到新實作。
- 任何 facade 行為保留不含語意變更（至少保持可 import 與主要公開呼叫可成立）。

## Temporary limits

- 不處理跨版本外部生態的去除條款，本輪不做廣義棄用公告。
- 兼容 facade 可在未來 Wave 3 以分層計畫逐步收斂，但不能在本輪直接刪除。

## Immediate constraints for Wave 0

- 不能在未先更新本文件並在 PR 描述列明時改動上述入口的呼叫簽名。
- 對外契約變更需先建立「兼容清單」與「替代路徑」。
