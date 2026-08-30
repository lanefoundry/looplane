# Event sink redelivery kills the TUI on `live_event_projection.event sequence is not contiguous`

日期：2026-08-30 · 範圍：`src/looplane/console.py`、`src/looplane/tui.py`、TUI 啟動路徑
驗證方式：reproduce — 在 `/Users/xiaoxu/Projects/looplane` 跑 `looplane`（無參數）後立即觸發 `ValueError`，traceback 全文見第 1 節。

---

## TL;DR

| # | 問題 | 結論 |
|---|------|------|
| 1 | 為什麼 TUI 剛啟動就 crash？ | `LiveEventProjection.apply` 收到一個 `sequence` **不大於** `last_sequence + 1` 的 event → raise `ValueError`；Textual `@on(RunEventMessage)` 沒 try/except，event loop 死，整個 TUI 退出。 |
| 2 | 為什麼「重送」會發生？ | Producer（`AgentRunner._event`）單線程分配 sequence，理論上不會重送；但 (a) `ConsoleEventSink.emit` 透過 `asyncio.Queue` 把 `RunEventMessage` 灌進 Textual worker，**訊息佇列可能在 Textual 重組 widget 後 replay**；(b) Resume 路徑從 `events.jsonl` reload，把 history replay 進 active `LiveEventProjection`。兩者都會讓同一個 `event_id` 出現第二次。 |
| 3 | 為什麼是「expected 21, got 20」？ | `last_sequence` 已經推進到 20 → 剛處理完 `tool.started sequence=20`；下一秒又收到同一筆 sequence=20 → 這是 redelivery，不是 gap。 |
| 4 | 為什麼 TUI 不能 recover？ | `tui.py:4242` 直接 `self._projection.apply(message.event)`，例外一路冒到 Textual 主迴圈，Textual 把 App 結束。對照 `CompositeEventSink` 自己已經有「secondary_errors 收集例外」契約（`console.py:38-46`）— TUI 沒沿用同一條路。 |
| 5 | 鄰居怎麼做？ | OMP（`oh-my-pi`）沒有 sequence 校驗層，靠 in-process bus 與 `toolCallId` actionMap 做顯示層 dedupe（`packages/tui/src/components`）；Claude Code `sinks.ts` 也只在 analytics 路徑去重；沒有人會因為 sequence 跳號就把整個 UI 殺掉。 |

---

## 1. 重現與 traceback

指令：

```bash
cd /Users/xiaoxu/Projects/looplane
scripts/install-dev-cli
looplane
```

安裝成功、`looplane --help` 正常，但**bare `looplane` 一進 TUI 就死**：

```
ValueError: event sequence is not contiguous: expected 21, got 20
```

完整 traceback：

```
looplane/tui.py:4242 in event_received
  projected = self._projection.apply(message.event)
  message = RunEventMessage()
  self = looplaneApp(title='looplane', classes={'-dark-mode', '-theme-looplane'}, ...)

looplane/console.py:69 in apply
  raise ValueError(
    f"event sequence is not contiguous: expected {self.last_sequence + 1}, "
    f"got {event.sequence}"
  )
  event = RunEvent(
    event_type='tool.started',
    run_id='d8ed56107663440ebd3d57a2705b8a43',
    task_id='6faea698f028443c9d63e5ce7936d374',
    sequence=20,
    data={'tool_call_id': 'call_01a052e4ba087a418f843fbf', 'name': 'read_file', 'effect': 'read'},
    event_id='20db89cf45044ceab3d53549a94a77d5',
    created_at=datetime.datetime(2026, 8, 30, 13, 38, 37, 135015, tzinfo=datetime.timezone.utc),
  )
  self = <looplane.console.LiveEventProjection object at 0x10f980510>
```

`expected 21, got 20`：上一次成功的 `apply` 已經把 `last_sequence` 從 19 推到 20（`console.py:73`），下一筆卻仍是 20。

---

## 2. 根因拆解

### 2.1 `LiveEventProjection.apply` 對 redelivery 零容忍

`src/looplane/console.py:63-73`：

```python
def apply(self, event: RunEvent) -> tuple[str, ...]:
    if self.run_id is None:
        self.run_id = event.run_id
    if event.run_id != self.run_id:
        raise ValueError("event stream changed run_id")
    if event.sequence != self.last_sequence + 1:
        raise ValueError(
            f"event sequence is not contiguous: expected {self.last_sequence + 1}, "
            f"got {event.sequence}"
        )
    self.last_sequence = event.sequence
    ...
```

`RunEvent` 模型本身帶 `event_id: uuid4()`（`events.py:29`）— 本來就設計成可識別「同一筆 event」— 但 `LiveEventProjection` 完全沒用這個欄位，而是只看 monotonic sequence。三種合理情況會撞死：

| 情況 | 表現 |
|---|---|
| Textual worker queue 在 widget 重組後 replay 最後 N 則訊息 | 收到第二份 `sequence=20` 的 `tool.started`，crash |
| Resume session：先 replay `events.jsonl` 灌進 projection，再 resume live | projection 已經走到 20，live producer 從 21 開始推；若 resume path 沒把 live 跳過 replayed range → 同一個 `event_id` 又灌一次 |
| 將來 subagent fan-out 多個 conversation 共享同一個 sink 時 cross-stream sequence | 不同 task_id 各自從 0 開始，混在同一個 projection → 永不等於 +1 |

### 2.2 TUI handler 沒 try/except

`src/looplane/tui.py:4238-4244`：

```python
@on(RunEventMessage)
def event_received(self, message: RunEventMessage) -> None:
    if message.generation != self._generation or not self.query("#activity"):
        return
    projected = self._projection.apply(message.event)   # ← raise 直接死
    for line in projected:
        self.query_one("#activity", RichLog).write(line)
```

對照 `CompositeEventSink.emit`（`console.py:40-46`）已有同樣契約：

```python
async def emit(self, event: RunEvent) -> None:
    await self.sinks[0].emit(event)
    for sink in self.sinks[1:]:
        try:
            await sink.emit(event)
        except Exception as exc:  # display/telemetry must not corrupt durable state
            self.secondary_errors.append(exc)
```

「secondary 失敗不能腐蝕 durable state」是已寫下的 invariant — 但 TUI 沒沿用同一條路。

### 2.3 鄰居設計對照

| Agent | sequence 校驗 | dedupe key | TUI 例外隔離 |
|---|---|---|---|
| OMP（oh-my-pi） | 沒有 in-memory bus 校驗 | `toolCallId` actionMap（顯示層） | component 內各自 try/except |
| Claude Code（claude-code-source） | analytics sink `streamJsonStdoutGuard.ts` 計 byte | sink 級 dedupe | `utils/sinks.ts` 內部吞例外 |
| Codex CLI | `codex_app_server.py:194 _next_sequence` 單 writer | 無（單 stream） | 同 framework |
| **looplane** | **strict monotonic in `LiveEventProjection`** | **無** | **無 — 直接死** |

---

## 3. 最小修復方案

兩個獨立改動，互相補強。

### 3.1 `LiveEventProjection` 用 `event_id` 做 dedupe，容忍 out-of-order

`src/looplane/console.py`：

```python
class LiveEventProjection:
    """Validate event order and turn audit events into short human-readable lines.

    Display layer: tolerant of redelivery (same event_id) and out-of-order arrival.
    The authoritative sequence is enforced by EventWriter + JsonlEventSink; this
    projection only projects events into short TUI lines and must never kill the
    UI on a benign redelivery from Textual's message queue or a session resume
    replay.
    """

    def __init__(
        self,
        *,
        max_preview_bytes: int = 2_000,
        run_id: str | None = None,
        last_sequence: int = -1,
        seen_event_ids: set[str] | None = None,
    ) -> None:
        self.max_preview_bytes = max_preview_bytes
        self.run_id = run_id
        self.last_sequence = last_sequence
        self.seen_event_ids: set[str] = seen_event_ids or set()
        self.dropped_duplicates = 0
        self.out_of_order = 0

    def apply(self, event: RunEvent) -> tuple[str, ...]:
        if self.run_id is None:
            self.run_id = event.run_id
        if event.run_id != self.run_id:
            raise ValueError("event stream changed run_id")
        # Redelivery: same event_id already projected → silent skip.
        if event.event_id in self.seen_event_ids:
            self.dropped_duplicates += 1
            return ()
        self.seen_event_ids.add(event.event_id)
        # Out-of-order / replay from a lower sequence: log + skip the projection.
        if event.sequence <= self.last_sequence:
            self.out_of_order += 1
            return ()
        self.last_sequence = event.sequence
        ...
```

要點：

- `seen_event_ids` 是新 state，O(1) lookup；沒上限就 O(N)，但 TUI life 是 process-bound，沒問題。
- `event.sequence <= self.last_sequence` 涵蓋「redelivery 同 sequence」與「resume replay 倒灌」兩種。
- **不** raise — `event run_id mismatch` 仍 raise（那是真實 stream 換 run，必須聲音）。
- 加 `dropped_duplicates` / `out_of_order` 計數，方便之後做 `looplane status` 或 diagnose 報告。

### 3.2 TUI `event_received` 補 try/except

`src/looplane/tui.py:4238-4244`：

```python
@on(RunEventMessage)
def event_received(self, message: RunEventMessage) -> None:
    if message.generation != self._generation or not self.query("#activity"):
        return
    try:
        projected = self._projection.apply(message.event)
    except ValueError as exc:
        # Display layer is best-effort: never let a malformed event end the UI.
        self._projection_errors += 1
        log = self.query_one("#activity", RichLog)
        log.write(f"[projection error: {type(exc).__name__}: {exc}]")
        return
    for line in projected:
        self.query_one("#activity", RichLog).write(line)
    ...
```

要點：

- 只接 `ValueError`（projection 自己會 raise 的）；其他 exception 仍要讓 framework 看到。
- 把計數寫進 `self._projection_errors`，搭配 console 端的 `dropped_duplicates` / `out_of_order` — 之後要 diagnose 一起呈現。
- 不 swallow `event run_id mismatch` — 真換 run 是 critical，應該繼續讓它 raise 或在 TUI 端做更顯眼的 UI（不在這次範圍）。

---

## 4. Acceptance

| 測試 | 預期 |
|---|---|
| 同一 `event_id` 灌兩次進 `LiveEventProjection` | 第二次回 `()`、`dropped_duplicates == 1` |
| `sequence` 從大到小倒灌 | 第二次回 `()`、`out_of_order == 1`、`last_sequence` 不變 |
| Resume replay 把 `events.jsonl` 從頭灌一次 | 既有 live events 不再 raise |
| TUI 收到會 raise 的 event（run_id mismatch） | 例外寫進 activity log、TUI 還活著、計數 +1 |
| `scripts/install-dev-cli && looplane` bare 啟動 | 不再出現 `ValueError: event sequence is not contiguous` |

## 5. 非本次範圍

- `run_id mismatch` 的 TUI-side recovery（應該要 clear projection 換新 run，而不是 silent log）。
- Projection 改為 `CompositeEventSink` 內部 secondary sink（讓 sink 自己負責 try/except，TUI 退化成單純 subscriber）— 比較大的 refactor，留待後續 RFC。
- Textual `RunEventMessage` 在 widget 重組時的 replay 行為本身 — 屬於 framework 行為，目前用 dedupe 就夠。

## 6. Prior art

- OMP `packages/tui/src/components`：以 `toolCallId` 為 actionMap key，重畫不重排。
- Claude Code `src/utils/sinks.ts`：sink 級 try/except + drop counter。
- Postgres logical replication consumer：以 `xid` 做 dedupe、容忍 out-of-order；同樣的「display never blocks durable」原則。