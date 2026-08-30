# 啟動效能 Playbook：從 Codex 0.148.0 學到的

> 目標讀者：looplane 開發者。目的：把 OpenAI 收購 Astral 後重做 Codex CLI life cycle 的經驗，轉成本專案可執行的工程實務。

## 1. 案例背景

OpenAI 於 2026/03 收購 Astral（uv / ruff / ty 背後公司），團隊併入 Codex。Codex CLI 0.148.0 是第一個可見成果：社群回報啟動快約 25 倍，近乎即開即用。

關鍵事實：

- Codex CLI 本來就是 Rust，慢的**不是語言**，是啟動流程設計。
- 官方 changelog 的優化全是流程層級：MCP OAuth 憑證讀取加速、plugin discovery 快取重用、skill/plugin 並行探索。
- 單一 PR（openai/codex#26469）就把 TUI 中位數啟動從 **833ms → 504ms**，並附上 paired benchmark 數據。

## 2. looplane 現況基準（2026-08-22 實測）

```
$ time .venv/bin/python -c "import looplane.cli"
real    0.701s   ← TUI 還沒出現就燒掉 ~700ms

$ python -X importtime -c "import looplane.cli"  # 最重路徑
398ms   looplane.cli
└─ 354ms  looplane.codex_oauth
   └─ 247ms  openai SDK（連 grader types 都載入）
```

診斷：`src/looplane/cli.py` 頂層 eager import 所有 backends 與 OAuth 模組，
導致「只想跑 `--help` 或非 Codex 流程」的使用者也付出整包 OpenAI SDK 的代價。

## 3. 五原則與對應做法

### 原則一：量測使用者感受的那一瞬間

Codex 的指標不是「import 時間」，而是 **median time to first editable composer**。

**looplane 做法**：

- 定義北極星指標：`looplane` 啟動 → composer 可輸入（TUI ready）。
- 在 TUI 初始化完成處打點，寫入 startup telemetry（可用環境變數 `LOOPLANE_STARTUP_LOG` 控制輸出）。
- 所有 perf PR 必須附 before/after paired benchmark（見第 4 節方法論）。

### 原則二：Lazy import —— 最低垂的果實

把「不會立刻用到」的重模組延後載入。

**looplane 做法**（`cli.py`）：

```python
# Before（頂層）
from looplane.claude_backend import ClaudeCodeBackend
from looplane.codex_oauth import ...

# After：函式內延遲載入
def _load_codex_backend():
    from looplane.codex_backend import CodexBackend
    return CodexBackend

@cache
def _get_openai_client():
    from openai import AsyncOpenAI
    return AsyncOpenAI()
```

規則：

- 所有非當前啟動路徑必要的重模組一律延遲載入，尤其是 `openai`、OAuth、vendor
  backend、conversation adapter、gateway 與 `uvicorn`；`textual` 只在實際進入 TUI 時載入。
- 型別註解用 `TYPE_CHECKING` guard。
- 各 backend（claude/codex/gateway）只在對應子命令被呼叫時 import。

預期效益：光此項應可砍掉 300–400ms。

### 原則三：並行化獨立的啟動路徑

Codex 把 hook discovery 與 account/model bootstrap 改為同時跑。

**looplane 做法**：盤點啟動步驟，找出無依賴者用 `asyncio.gather`：

| 步驟 | 是否獨立 |
|---|---|
| 讀取 config | ✅ 可與其他並行 |
| workspace 建立/清理 | ✅ |
| backend auth 檢查（OAuth token refresh） | ✅ |
| MCP / tool server 連線 | ⚠️ 應延遲到首次使用 |

### 原則四：快取 + single-flight 探索結果

任何「掃描」類工作不得在每次啟動重做。

**looplane 做法**：

- backend capability、workspace 狀態掃描結果以「相關 config 的 hash」為 key 快取到磁碟（參考 Codex：key 只含 plugin 相關設定，避免無關變更使快取失效）。
- 並行請求同一資源時 single-flight（只放行第一個，其餘共用結果），防止 stampede 與 race。
- 失效的載入結果不得回填快取。

### 原則五：驗證迴路速度 = 產品本身

looplane 的賣點是 verified patches；agent 每秒能迭代幾次，取決於 linter/tester 多快。

- 已選 ruff ✅，繼續把「單次迭代毫秒數」當核心 KPI。
- evals 與 sandbox 驗證流程同樣適用原則二～四（延遲載入、並行、快取）。

## 4. 量測方法論（照 Codex PR #26469 的打法）

1. **工具**：`hyperfine --warmup 3 --min-runs 10`。
2. **paired benchmark**：交替執行 before/after 各 10 次，取中位數，避免機器雜訊。
3. **場景要真實**：帶著現有 `~/.looplane` 設定與 workspace 跑，不要用乾淨環境自欺。
4. **importtime 找元兇**：`python -X importtime -c "import looplane.cli" | sort -t'|' -k2 -rn | head`。

建議新增 `scripts/bench_startup.sh` 固化上述流程，perf PR 一律貼數字。

## 5. 行動清單

正式執行規劃：`docs/plans/m12-startup-performance-plan.md`。外接 OpenCode／Pi／OMP 的 M13 必須建立
在這個 lazy discovery 與 benchmark contract 上，避免新增 adapter 擴大共同啟動成本。

- [ ] 新增 `scripts/bench_startup.sh`（hyperfine + importtime）
- [ ] `cli.py` 移除頂層重模組 import，改 lazy loading
- [ ] TUI 加 startup 打點，輸出 time-to-composer
- [ ] 盤點啟動路徑依賴圖，並行化獨立步驟
- [ ] workspace/backend 探索加磁碟快取 + single-flight
- [ ] CI 加 startup regression gate（同 runner paired median；先建立噪音基線，再以 >10%
      退步擋 merge並保留 raw benchmark artifact）

## 6. 心法總結

> 慢的从来不是語言，是 life cycle 設計。
> 工程品味最好的展示方式，是公開的 before/after 數字。
