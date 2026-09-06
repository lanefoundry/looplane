# 檔案與圖片輸入機制：調研 + 設計 + 實作規格

> 調研日期：2026-09-06
> 目的：為 Looplane TUI 加入檔案/圖片附件功能，全四管道

---

## 一、現狀分析

### 已完成（模型層）

`models.py:347-478` 已完整處理 attachment → provider content block 的轉換：

- `_NATIVE_IMAGE_MEDIA_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}`
- `_NATIVE_FILE_MEDIA_TYPES = {"application/pdf", "text/plain", "text/markdown"}`
- 四個 provider 各有對應函式：`_openai_user_content`、`_responses_user_content`、`_anthropic_user_content`、`_gemini_user_parts`
- attachment schema（存在 `message.provider_metadata["attachments"]`）：
  ```python
  {"name": str, "media_type": str, "data_base64"?: str, "uri"?: str, "content"?: str}
  ```

### 缺失（TUI 輸入層）

| 元件 | 現狀 | 需要改動 |
|------|------|---------|
| `terminal/composer.py` (86 行) | 純文字 TextArea，無附件概念 | 加附件列 UI、paste 攔截 |
| `terminal/app.py:_submit_current_task` | `instruction` 是純字串 | 需帶 attachments 到 `_run_agent` |
| `terminal/types.py:TuiRunRequest` | 無 attachments 欄位 | 加 `attachments` tuple |
| `slash_commands.py` | 無 `/add` / `/remove` | 新增兩個指令 |
| `terminal/clipboard.py` | 只做文字 copy | 加 image paste 讀取 |

---

## 二、主流 Coding Agent 比較

### 2.1 輸入管道總覽

| 工具 | 剪貼簿貼上 | 拖放 | 路徑偵測 | 指令/Flag | `@` mention |
|------|:----------:|:----:|:--------:|:---------:|:-----------:|
| Claude Code | ✅ Ctrl+V | ✅ | ✅ | — | — |
| Codex CLI | ✅ Ctrl+V | ✅ | — | ✅ `--image` | — |
| Aider | ✅ `/paste` | — | ✅ 啟動時帶路徑 | ✅ `/add` | — |
| Cursor | ✅ Cmd+V | ✅ | — | — | ✅ `@file` |
| Cline | — | ✅ (Shift+拖) | — | ✅ `+` 按鈕 | ✅ `@file` |
| OpenCode | — | ✅ | — | — | — |

### 2.2 關鍵觀察

1. **三大管道是共識**：剪貼簿貼上、拖放、路徑/指令參照 — 每家至少做兩種
2. **CLI/TUI 全都靠「終端拖放→貼路徑字串」**，沒有任何一家實作 Kitty/Sixel/iTerm2 inline protocol 來「接收」圖片
3. **剪貼簿截圖**是最高頻入口（截圖 → Ctrl+V）— Claude Code 用 `chat:imagePaste` keybinding
4. **非圖片檔案**：IDE 類用 `@` mention；CLI 類用 `/add` 指令或 agent 自己 read_file
5. **UI 回饋**至關重要 — 用戶需要看到 chip/pill 確認附件已掛上

### 2.3 各工具細節

#### Claude Code
- `chat:imagePaste` keybinding（Ctrl+V / Alt+V），讀 clipboard raw PNG bytes → base64
- 拖放：終端模擬器拖放 → 貼路徑字串 → 偵測路徑並讀取
- UI 回饋：`[Image #1]` chip
- 非圖片：agent 用 Read tool 讀取，無前端附件機制

#### Codex CLI（OpenAI）
- CLI flag：`--image path.png`、多圖 `--image a.png,b.png` 或重複 flag
- Ctrl+V 貼上、終端拖放

#### Aider
- `/add image.png` 加入 context、`/paste` 讀剪貼簿圖片
- `/add file.csv`、`/read-only file.pdf`；URL 用 `/web`
- 啟動時：`aider img.png file.csv` 帶檔案路徑

#### Cursor / Cline
- IDE webview 環境：拖放、Cmd+V、`@file` / `@folder` mention
- 縮圖 + pill UI

#### OpenCode
- Go TUI (Bubble Tea)：拖放進 TUI 終端
- 依賴 Ghostty/WezTerm 等現代終端的拖放支援

### 2.4 終端協議

所有 CLI/TUI 工具**都沒有**實作 Kitty Graphics Protocol、Sixel、或 iTerm2 Inline Image Protocol 來接收輸入。

拖放原理：現代終端模擬器（iTerm2、Ghostty、WezTerm、Kitty）在使用者拖放檔案時，自動將絕對路徑以文字形式貼入 stdin。工具端只需偵測路徑字串。

---

## 三、設計規格

### 3.1 四個輸入管道

#### 管道 1：路徑偵測（prompt 中的檔案路徑自動掛附件）

```
> 看看這張截圖 ~/Desktop/screenshot.png 有什麼問題
     ↓ 送出時偵測到路徑 → 讀取 → 塞 attachment
     ↓ prompt 文字中路徑保留（讓模型知道檔名）
```

偵測策略：
- 送出時（`_submit_current_task`）掃描 prompt 中的 token
- 以空白分隔，匹配 `Path(token).expanduser()` 是否 `.is_file()`
- **副檔名白名單**過濾（見 3.2），避免誤判 `./src` 等目錄或常見詞
- 支援 `~` 展開、相對路徑（相對於 `self.repository`）、絕對路徑
- 引號包裹的路徑也處理（拖放常見：`'/path/to/file with spaces.png'`）
- 跳脫：路徑前加 `\` 不觸發（`\path.png`）

#### 管道 2：`/add` 指令（明確加入附件到當前訊息或 session context）

```
/add assets/logo.png                → 加到下一則訊息
/add screenshots/*.png              → glob 展開
/add --context data/schema.sql      → 加到 session context（每則訊息都帶）
/remove logo.png                    → 移除已加的附件
/remove --all                       → 清空所有附件
```

- `/add` 無 `--context` flag 時：附件只跟下一則訊息
- `/add --context`：附件持續存在直到 `/remove` 或 `/new`
- 加入後 composer 底部顯示附件列

#### 管道 3：剪貼簿圖片貼上（Ctrl+V）

- Keybinding：Ctrl+V（覆寫 Textual 預設 paste 行為）
- 偵測順序：
  1. 剪貼簿有圖片資料？→ 讀取 → 塞 attachment
  2. 剪貼簿只有文字？→ 維持原本 paste 行為
- macOS 讀取：`osascript -e 'the clipboard as «class PNGf»'` → hex → bytes → base64
- Linux 讀取：`xclip -selection clipboard -t image/png -o` → bytes → base64
- 備選 macOS：`pngpaste -` （如果安裝了）
- 圖片命名：`clipboard-{timestamp}.png`

#### 管道 4：拖放

- 終端模擬器拖放自動貼路徑字串 → Textual 的 `on_paste` 事件
- Composer 的 `on_paste` 偵測貼入文字是否為有效檔案路徑
- 如果是 → 走 AttachmentManager，不插入文字到編輯區
- 如果不是 → 維持原本 paste 行為
- 注意事項：
  - 部分終端包引號：`'/path/to/file'` 或 `"/path/to/file"`
  - 多檔拖放：以換行分隔的多個路徑
  - 路徑中有空格的情況

### 3.2 支援的檔案類型

| 類別 | 副檔名 | media_type | 處理方式 |
|------|--------|-----------|---------|
| 原生圖片 | `.png` `.jpg` `.jpeg` `.gif` `.webp` | `image/*` | base64 → `data_base64` |
| SVG | `.svg` | `image/svg+xml` | 讀文字 → `content` |
| PDF | `.pdf` | `application/pdf` | base64 → `data_base64` |
| 結構化資料 | `.csv` `.json` `.yaml` `.yml` `.toml` `.xml` | `text/*` / `application/*` | 讀文字 → `content` |
| 文件 | `.md` `.txt` `.rst` `.log` | `text/*` | 讀文字 → `content` |
| 程式碼 | `.py` `.js` `.ts` `.tsx` `.jsx` `.go` `.rs` `.rb` `.java` `.c` `.cpp` `.h` `.hpp` `.cs` `.swift` `.kt` `.sh` `.bash` `.zsh` `.sql` `.html` `.css` `.scss` | `text/*` | 讀文字 → `content` |
| 其他二進位 | `.zip` `.tar` `.gz` 等 | — | **拒絕**，提示不支援 |

偵測方式：先用副檔名判斷，fallback 用 `mimetypes.guess_type()`。

### 3.3 大小限制

| 項目 | 上限 | 理由 |
|------|------|------|
| 單檔（二進位/圖片） | 20 MB | Anthropic API 限制 |
| 單檔（文字） | 512 KB | 避免塞爆 context window |
| 附件總數 | 10 個 / 訊息 | 合理上限 |
| Session context 附件 | 5 個 | 每則訊息都帶，不能太多 |

超過限制時在 status bar 顯示錯誤訊息，不靜默忽略。

### 3.4 附件資料流（完整架構）

```
                ┌──────────────────────────────────┐
                │  輸入管道                          │
                │  ┌─────────┐  ┌──────┐  ┌──────┐ │
                │  │路徑偵測  │  │/add  │  │Ctrl+V│ │
                │  │(送出時)  │  │指令  │  │paste │ │
                │  └────┬────┘  └──┬───┘  └──┬───┘ │
                │       │          │          │      │
                │       ▼          ▼          ▼      │
                │  ┌──────────────────────────────┐ │
                │  │  AttachmentManager (新模組)    │ │
                │  │  - resolve_path()             │ │
                │  │  - read_file() → Attachment   │ │
                │  │  - read_clipboard_image()     │ │
                │  │  - validate_size()            │ │
                │  │  - detect_media_type()        │ │
                │  └──────────────┬───────────────┘ │
                └─────────────────┼─────────────────┘
                                  │
                                  ▼
                ┌──────────────────────────────────┐
                │  MessageComposer (附件狀態)        │
                │  - _pending_attachments: list     │
                │  - _session_attachments: list     │
                │  - 附件列 UI (composer 底部)       │
                └──────────────┬───────────────────┘
                               │ Submitted 事件
                               ▼
                ┌──────────────────────────────────┐
                │  app.py:_submit_current_task()    │
                │  - 合併 pending + session 附件     │
                │  - 清空 pending（保留 session）     │
                │  - 傳入 _run_agent()              │
                └──────────────┬───────────────────┘
                               │
                               ▼
                ┌──────────────────────────────────┐
                │  TuiRunRequest                    │
                │  + attachments: tuple[dict,...]   │
                └──────────────┬───────────────────┘
                               │
                               ▼
                ┌──────────────────────────────────┐
                │  runner → message.provider_metadata│
                │  {"attachments": [...]}           │
                └──────────────┬───────────────────┘
                               │
                               ▼
                ┌──────────────────────────────────┐
                │  models.py (已完成)                │
                │  _openai_user_content()           │
                │  _anthropic_user_content()        │
                │  _gemini_user_parts()             │
                └──────────────────────────────────┘
```

### 3.5 UI 回饋

#### Composer 附件列

在 composer `#task` TextArea 的下方（`#composer-actions` 上方）插入附件列容器：

```
┌─────────────────────────────────────────────┐
│ > 這張截圖的按鈕位置不對                        │
│                                             │
├─────────────────────────────────────────────┤
│ 📎 screenshot.png (245KB)  [x]              │
│ 📎 data.csv (12KB)         [x]              │
├─────────────────────────────────────────────┤
│ Enter send · Shift+Enter newline · ...      │
└─────────────────────────────────────────────┘
```

- 附件列只在有附件時顯示（`display: none` toggle）
- 每個附件一行：圖示 + 檔名 + 大小 + 移除按鈕
- Session context 附件標記 `(ctx)` 區分
- 移除快捷鍵：點擊 `[x]` 或用 `/remove`

#### Status bar 回饋

- 成功加入：`"Added screenshot.png (245KB)"`
- 檔案不存在：`"File not found: path/to/file.png"`
- 太大：`"File too large: 25MB (limit 20MB)"`
- 不支援的類型：`"Unsupported file type: .zip"`
- 附件已滿：`"Attachment limit reached (10)"`

#### Transcript 中的附件顯示

送出後在 `You` / `Task` turn 中顯示附件：

```
You
 看看這張截圖有什麼問題
 📎 screenshot.png · 📎 data.csv
```

---

## 四、實作步驟

### Step 1：新增 `attachment_manager.py`

位置：`src/looplane/attachment_manager.py`（非 terminal 模組，runner 也可能用）

```python
@dataclass(frozen=True)
class Attachment:
    name: str
    media_type: str
    data_base64: str | None = None   # 二進位檔
    content: str | None = None       # 文字檔
    size_bytes: int = 0

    def to_provider_dict(self) -> dict[str, str]: ...

def resolve_and_read(path: str | Path, *, base: Path) -> Attachment: ...
def read_clipboard_image() -> Attachment | None: ...
def detect_paths_in_text(text: str, *, base: Path) -> list[Path]: ...
```

### Step 2：擴展 `TuiRunRequest`

```python
@dataclass(frozen=True)
class TuiRunRequest:
    ...
    attachments: tuple[dict[str, str], ...] = ()
```

### Step 3：擴展 `MessageComposer`

- 新增 `_pending_attachments: list[Attachment]`
- 新增 `add_attachment()` / `remove_attachment()` / `clear_attachments()`
- 覆寫 `on_paste` — 偵測圖片剪貼簿 / 路徑字串
- `Submitted` 事件帶上 `attachments`
- 新增附件列 widget（`Static` 或 `RichLog`）

### Step 4：擴展 `slash_commands.py`

```python
class SlashCommand(StrEnum):
    ...
    ADD = "add"
    REMOVE = "remove"
```

### Step 5：`app.py` 串接

- `_submit_current_task`：送出前走 `detect_paths_in_text()`
- 合併 pending + session 附件 → `TuiRunRequest.attachments`
- 附件傳遞到 runner 層 → `provider_metadata["attachments"]`
- `_dispatch_command` 處理 `/add` 和 `/remove`

### Step 6：Runner 層串接

- `runner.py` / `model_calls.py` 接收 `TuiRunRequest.attachments`
- 塞入 `message.provider_metadata["attachments"]`
- `models.py` 已有的邏輯自動處理

### Step 7：Paste handler（Ctrl+V 圖片）

- `terminal/clipboard.py` 新增 `read_clipboard_image() -> bytes | None`
- macOS：`osascript` 讀 `PNGf` class
- Linux：`xclip -selection clipboard -t image/png -o`
- Composer 覆寫 `_on_key` 的 Ctrl+V → 先檢查剪貼簿有無圖片

### Step 8：拖放偵測

- Composer 的 `on_paste` 已經被 Textual 用來處理終端模擬器的拖放文字
- 在 paste handler 中：如果貼入文字像檔案路徑 → 走 AttachmentManager
- 不像路徑 → 維持原本 paste 行為

---

## 五、測試計畫

### 單元測試（`tests/test_attachment_manager.py`）

- `resolve_and_read`：各類型檔案讀取、base64 正確性
- `detect_paths_in_text`：路徑偵測的正面/反面案例
- 大小限制檢查
- 不支援類型拒絕
- 引號路徑解析
- `~` 展開

### 整合測試

- Composer 加附件 → Submitted 事件包含附件
- `/add` 指令 → 附件列更新
- `/remove` → 正確移除
- `TuiRunRequest` 帶附件到 runner

### 手動測試

- Ctrl+V 截圖貼上（macOS / Linux）
- 拖放檔案到終端
- 大檔案拒絕
- 多附件同時

---

## 六、參考資料

- Claude Code：`chat:imagePaste` keybinding，`[Image #1]` chip
- Codex CLI：`--image` flag，multi-image 支援
- Aider：`/add`、`/paste`、`/read-only` 指令系統
- Cursor：`@file` mention + 拖放 + Cmd+V
- Cline：`@` mention + `+` 按鈕 + Shift 拖放
- OpenCode：TUI 拖放，依賴現代終端
